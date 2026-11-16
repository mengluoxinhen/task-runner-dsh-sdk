import uuid
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from doc_audit.config import load_config
from .db import init_db, create_task, get_task, list_tasks
from .worker import Worker


SETTINGS = load_config()
PATHS = SETTINGS["paths"]
LIMITS = SETTINGS["limits"]
ALLOWED_EXTS = set(LIMITS["allowed_exts"])
WORKSPACE_ROOT = PATHS["workspace_root"]
DSH_HOME = PATHS["dsh_home"]
MODES = list(SETTINGS["prompt_templates"].keys())

app = FastAPI(title="Doc Audit Service")
worker = Worker(SETTINGS, Path.cwd())


@app.on_event("startup")
def startup():
    init_db(PATHS["db_file"])
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    worker.start()


@app.post("/api/tasks")
async def create_audit_task(
    mode: str = Form(...),
    files: list[UploadFile] = File(...),
):
    if mode not in MODES:
        raise HTTPException(400, f"mode 必须为 {' 或 '.join(MODES)}")
    if mode == "single" and len(files) != 1:
        raise HTTPException(400, "单文档审核只接受 1 个文件")
    if mode == "multi" and not (2 <= len(files) <= LIMITS["multi_max_files"]):
        raise HTTPException(400, f"多文档比对接受 2–{LIMITS['multi_max_files']} 个文件")

    task_id = uuid.uuid4().hex[:12]
    ws = WORKSPACE_ROOT / task_id
    ws.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise HTTPException(400, f"不支持的格式: {f.filename}")
        dest = ws / Path(f.filename).name
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dest.name)

    create_task(task_id, mode, saved)
    worker.submit(task_id, mode, ws, DSH_HOME)
    return {"task_id": task_id, "status": "pending", "mode": mode, "files": saved}


@app.get("/api/tasks/{task_id}")
def query_task(task_id: str):
    t = get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.get("/api/tasks")
def query_tasks(limit: int = 50, offset: int = 0):
    return {"items": list_tasks(limit, offset)}


@app.get("/api/tasks/{task_id}/report")
def download_report(task_id: str):
    t = get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "completed":
        raise HTTPException(409, f"任务尚未完成，当前状态: {t['status']}")
    report = WORKSPACE_ROOT / task_id / LIMITS["report_filename"]
    if not report.exists():
        raise HTTPException(404, "报告不存在")
    return FileResponse(report, filename=f"audit_{task_id}.json",
                        media_type="application/json")


@app.get("/api/tasks/{task_id}/files/{filename}")
def download_workspace_file(task_id: str, filename: str):
    """下载任务 workspace 中的文件（如 Agent 自动修改后的文档）。"""
    t = get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "completed":
        raise HTTPException(409, f"任务尚未完成，当前状态: {t['status']}")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = WORKSPACE_ROOT / task_id / filename
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=filename)
