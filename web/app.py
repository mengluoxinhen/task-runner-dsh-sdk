import asyncio
import mimetypes
import re
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from task_engine.config import BASE_DIR, load_config
from task_engine.runner import read_json_file
from task_engine.tasks import TaskSpec, load_tasks

from .db import create_task, get_task, init_db, list_tasks
from .worker import Worker

VERSION = "2.0"
STATUSES = {"pending", "running", "completed", "failed"}


def _error(status: int, code: str, message: str, **extra):
    detail = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=status, detail=detail)


def _remove_file(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _dedupe_name(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    stem, ext = Path(name).stem, Path(name).suffix
    index = 2
    while f"{index}_{stem}{ext}" in used:
        index += 1
    return f"{index}_{stem}{ext}"


def _clean_upload_name(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    if not name or name in {".", ".."}:
        _error(400, "invalid_filename", "上传文件名为空或非法")
    if len(name) > 200:
        stem, ext = Path(name).stem, Path(name).suffix
        name = stem[: max(1, 200 - len(ext))] + ext
    return name


def _save_upload(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    _error(
                        413,
                        "file_too_large",
                        f"文件超过单文件上限 {max_bytes} 字节",
                        file=dest.name,
                        max_file_bytes=max_bytes,
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    return size


def create_app(settings: dict | None = None, runner_factory=None) -> FastAPI:
    settings = settings or load_config()
    tasks = load_tasks(settings)
    paths = settings["paths"]
    workspace_root = Path(paths["workspace_root"])
    dsh_home = Path(paths["dsh_home"])
    tmp_dir = Path(paths["tmp_dir"])
    result_max_bytes = int(settings.get("limits", {}).get("result_max_bytes", 20971520))
    sync_max_waiters = int(settings.get("server", {}).get("sync_max_waiters", 8))

    worker = Worker(settings, tasks, BASE_DIR, runner_factory=runner_factory)
    sync_pool = ThreadPoolExecutor(max_workers=max(1, sync_max_waiters),
                                   thread_name_prefix="sync-wait")
    sync_lock = threading.BoundedSemaphore(max(1, sync_max_waiters))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(paths["db_file"])
        for directory in (workspace_root, dsh_home, tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        worker.start()
        try:
            yield
        finally:
            worker.stop()
            sync_pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Task Runner Service", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.tasks = tasks
    app.state.worker = worker
    app.state.workspace_root = workspace_root
    app.state.dsh_home = dsh_home
    app.state.tmp_dir = tmp_dir

    # ── helpers ────────────────────────────────────────────────
    def _task_spec(task_type: str) -> TaskSpec:
        spec = tasks.get(task_type)
        if spec is None:
            _error(404, "unknown_task_type",
                   f"未知任务类型: {task_type}",
                   available=list(tasks))
        return spec

    def _result_url(task_id: str) -> str:
        return f"/api/tasks/{task_id}/result"

    def _file_url(task_id: str, rel_path: str) -> str:
        return f"/api/tasks/{task_id}/files/{quote(rel_path)}"

    def _accepted(task_id: str, spec: TaskSpec, status: str = "pending") -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task_id,
                "task_type": spec.id,
                "status": status,
                "delivery": spec.delivery,
                "status_url": f"/api/tasks/{task_id}",
                "result_url": _result_url(task_id),
                "retry_after_ms": 2000,
            },
            headers={
                "Location": f"/api/tasks/{task_id}",
                "Retry-After": "2",
            },
        )

    def _build_zip(task_id: str, workspace: Path, outputs: list[dict]) -> Path:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = tmp_dir / f"{task_id}-{uuid.uuid4().hex[:8]}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in outputs:
                rel_path = item.get("rel_path")
                if not rel_path:
                    continue
                path = (workspace / rel_path).resolve()
                if path.is_file() and path.is_relative_to(workspace.resolve()):
                    zf.write(path, arcname=item.get("name") or path.name)
        return zip_path

    def _files_result_response(task: dict, spec: TaskSpec) -> FileResponse:
        outputs = task.get("output_files") or []
        if not outputs:
            _error(404, "result_missing", "任务没有可下载的结果文件", task_id=task["id"])
        workspace = workspace_root / task["id"]
        headers = {"X-Task-Id": task["id"]}
        if len(outputs) == 1:
            item = outputs[0]
            path = (workspace / item["rel_path"]).resolve()
            if not path.is_file() or not path.is_relative_to(workspace.resolve()):
                _error(404, "result_missing", "结果文件不存在", task_id=task["id"])
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return FileResponse(path, media_type=media_type,
                                filename=Path(item.get("name") or path.name).name,
                                headers=headers)
        zip_path = _build_zip(task["id"], workspace, outputs)
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"{task['id']}.zip",
            headers=headers,
            background=BackgroundTask(_remove_file, zip_path),
        )

    def _json_result_response(task: dict, spec: TaskSpec, download: bool):
        rel_path = task.get("result_rel_path") or spec.output.file
        path = (workspace_root / task["id"] / rel_path).resolve()
        if not path.is_file() or not path.is_relative_to((workspace_root / task["id"]).resolve()):
            _error(404, "result_missing", "结果 JSON 文件不存在", task_id=task["id"])
        if download:
            return FileResponse(
                path,
                media_type="application/json",
                filename=f"{task['id']}.json",
                headers={"X-Task-Id": task["id"]},
            )
        try:
            data = read_json_file(path, max_bytes=result_max_bytes)
        except Exception as exc:
            _error(500, "result_invalid", f"结果 JSON 解析失败: {exc}",
                   task_id=task["id"])
        return JSONResponse(content=data, headers={"X-Task-Id": task["id"]})

    def _result_response(task: dict, spec: TaskSpec, download: bool = False):
        if task["status"] == "failed":
            _error(409, "task_failed",
                   (task.get("error") or "任务失败").splitlines()[0],
                   task_id=task["id"])
        if task["status"] != "completed":
            _error(409, "task_not_completed",
                   f"任务尚未完成，当前状态: {task['status']}",
                   task_id=task["id"], status=task["status"])
        kind = task.get("output_kind") or spec.output.kind
        if kind == "json":
            return _json_result_response(task, spec, download)
        return _files_result_response(task, spec)

    def _task_view(task: dict) -> dict:
        spec = tasks.get(task["task_type"])
        kind = task.get("output_kind") or (spec.output.kind if spec else None)
        result = {
            "kind": kind,
            "available": False,
            "url": _result_url(task["id"]),
        }
        if task["status"] == "completed":
            if kind == "json":
                rel_path = task.get("result_rel_path") or (
                    spec.output.file if spec else "result.json")
                path = (workspace_root / task["id"] / rel_path).resolve()
                if (path.is_file()
                        and path.is_relative_to((workspace_root / task["id"]).resolve())):
                    try:
                        result.update({
                            "available": True,
                            "file": rel_path,
                            "data": read_json_file(path, max_bytes=result_max_bytes),
                        })
                    except Exception as exc:
                        result["note"] = f"结果文件不可用: {exc}"
                else:
                    result["note"] = "结果 JSON 文件不存在"
            else:
                outputs = task.get("output_files") or []
                result["available"] = bool(outputs)
                result["files"] = [
                    {
                        "name": item.get("name"),
                        "rel_path": item.get("rel_path"),
                        "size": item.get("size"),
                        "url": _file_url(task["id"], item.get("rel_path", "")),
                    }
                    for item in outputs
                ]
        return {
            "id": task["id"],
            "task_type": task["task_type"],
            "task_name": spec.name if spec else task["task_type"],
            "status": task["status"],
            "delivery": task.get("delivery") or (spec.delivery if spec else None),
            "input_files": task.get("input_files") or [],
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "duration_ms": task.get("duration_ms"),
            "error": task.get("error"),
            "result": result,
        }

    # ── meta ───────────────────────────────────────────────────
    @app.get("/")
    def index():
        return {
            "service": "task-runner",
            "version": VERSION,
            "model": settings.get("model"),
            "task_types": list(tasks),
            "endpoints": {
                "task_types": "/api/task-types",
                "create": "/api/tasks/{task_type}",
                "status": "/api/tasks/{task_id}",
                "result": "/api/tasks/{task_id}/result",
                "files": "/api/tasks/{task_id}/files/{path}",
                "list": "/api/tasks",
            },
        }

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "task_types": len(tasks),
            "waiting_sync": worker.waiters.pending(),
        }

    @app.get("/api/task-types")
    def task_types():
        return {"items": [spec.public_metadata() for spec in tasks.values()]}

    # ── create ─────────────────────────────────────────────────
    @app.post("/api/tasks", status_code=410)
    def legacy_create():
        return JSONResponse(
            status_code=410,
            content={"detail": {
                "code": "legacy_endpoint",
                "message": "POST /api/tasks 已移除，请改用 POST /api/tasks/{task_type}。",
                "task_types": list(tasks),
            }},
        )

    @app.post("/api/tasks/{task_type}")
    async def create_task_endpoint(
        task_type: str,
        files: list[UploadFile] = File(...),
    ):
        spec = _task_spec(task_type)
        if not files:
            _error(400, "too_few_files", f"至少需要 {spec.min_files} 个文件")
        if len(files) < spec.min_files:
            _error(400, "too_few_files",
                   f"{spec.name} 需要至少 {spec.min_files} 个文件，实际 {len(files)} 个",
                   min_files=spec.min_files, max_files=spec.max_files)
        if len(files) > spec.max_files:
            _error(400, "too_many_files",
                   f"{spec.name} 最多接受 {spec.max_files} 个文件，实际 {len(files)} 个",
                   min_files=spec.min_files, max_files=spec.max_files)

        accept = set(spec.accept)
        reserved = {
            spec.output.file, "result.json.tmp", "_artifacts", ".dsh", "__pycache__",
            Path(spec.output.dir).parts[0],
        }
        used: set[str] = set()
        planned: list[tuple[UploadFile, str]] = []
        for upload in files:
            name = _clean_upload_name(upload.filename)
            ext = Path(name).suffix.lower()
            if ext not in accept:
                _error(400, "unsupported_ext",
                       f"{spec.name} 不支持格式: {ext or '(无扩展名)'}",
                       file=name, accepted=list(spec.accept))
            if name in reserved:
                name = "input_" + name
            name = _dedupe_name(name, used)
            used.add(name)
            planned.append((upload, name))

        task_id = uuid.uuid4().hex[:12]
        workspace = workspace_root / task_id
        try:
            workspace.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            _error(500, "task_create_failed", "任务 workspace 冲突，请重试")
        saved: list[str] = []
        try:
            for upload, name in planned:
                _save_upload(upload, workspace / name, spec.max_file_bytes)
                saved.append(name)
            create_task(task_id, spec.id, spec.delivery, saved, spec.output.kind)
            wait_event = worker.submit(task_id, spec, workspace, dsh_home, saved)
        except HTTPException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            _error(500, "task_create_failed", f"创建任务失败: {exc}")

        if spec.delivery != "sync":
            return _accepted(task_id, spec)

        if not sync_lock.acquire(blocking=False):
            return _accepted(task_id, spec)

        try:
            loop = asyncio.get_running_loop()
            try:
                finished = await asyncio.wait_for(
                    loop.run_in_executor(sync_pool, wait_event.wait,
                                         spec.sync_timeout_seconds),
                    timeout=spec.sync_timeout_seconds + 5,
                )
            except asyncio.TimeoutError:
                finished = False
            if not finished:
                return _accepted(task_id, spec)
            task = get_task(task_id)
            if task is None:
                _error(500, "task_missing", "任务记录丢失", task_id=task_id)
            if task["status"] == "failed":
                _error(409, "task_failed",
                       (task.get("error") or "任务失败").splitlines()[0],
                       task_id=task_id)
            if task["status"] == "completed":
                return _result_response(task, spec)
            return _accepted(task_id, spec, task["status"])
        finally:
            sync_lock.release()

    # ── query ──────────────────────────────────────────────────
    @app.get("/api/tasks")
    def query_tasks(task_type: str | None = None, status: str | None = None,
                    limit: int = 50, offset: int = 0):
        if status is not None and status not in STATUSES:
            _error(400, "invalid_status", f"status 必须是 {'/'.join(sorted(STATUSES))}")
        items = list_tasks(limit=limit, offset=offset,
                           task_type=task_type, status=status)
        public = []
        for task in items:
            spec = tasks.get(task["task_type"])
            public.append({
                "id": task["id"],
                "task_type": task["task_type"],
                "task_name": spec.name if spec else task["task_type"],
                "status": task["status"],
                "delivery": task.get("delivery"),
                "input_files": task.get("input_files") or [],
                "output_kind": task.get("output_kind"),
                "created_at": task.get("created_at"),
                "finished_at": task.get("finished_at"),
                "duration_ms": task.get("duration_ms"),
                "result_url": _result_url(task["id"]),
            })
        return {"items": public, "limit": limit, "offset": offset}

    @app.get("/api/tasks/{task_id}")
    def query_task(task_id: str):
        task = get_task(task_id)
        if task is None:
            _error(404, "not_found", "任务不存在", task_id=task_id)
        return _task_view(task)

    @app.get("/api/tasks/{task_id}/result")
    def download_result(task_id: str, download: int = 0):
        task = get_task(task_id)
        if task is None:
            _error(404, "not_found", "任务不存在", task_id=task_id)
        spec = tasks.get(task["task_type"])
        if spec is None:
            _error(410, "task_type_removed",
                   f"任务类型 {task['task_type']} 已不在配置中", task_id=task_id)
        return _result_response(task, spec, download=bool(download))

    @app.get("/api/tasks/{task_id}/files/{file_path:path}")
    def download_workspace_file(task_id: str, file_path: str):
        task = get_task(task_id)
        if task is None:
            _error(404, "not_found", "任务不存在", task_id=task_id)
        if task["status"] not in {"completed", "failed"}:
            _error(409, "task_not_completed",
                   f"任务尚未结束，当前状态: {task['status']}",
                   task_id=task_id, status=task["status"])

        workspace = (workspace_root / task_id).resolve()
        candidate = (workspace / file_path).resolve()
        if not candidate.is_relative_to(workspace):
            _error(403, "path_forbidden", "非法路径", path=file_path)
        if not candidate.is_file() or candidate.is_symlink():
            _error(404, "not_found", "文件不存在", path=file_path)

        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        return FileResponse(candidate, media_type=media_type,
                            filename=candidate.name,
                            headers={"X-Task-Id": task_id})

    return app


app = create_app()
