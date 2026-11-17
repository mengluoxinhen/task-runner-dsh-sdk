"""无 LLM 的接口测试：用 FakeRunner 替代 harness，覆盖校验/结果/交付路径。

运行：python tests/test_api.py
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from task_engine.config import load_config  # noqa: E402
from task_engine.runner import RunOutcome, atomic_write_json  # noqa: E402
from web.app import create_app  # noqa: E402
from web.db import init_db  # noqa: E402

TASKS_YAML = """
defaults:
  delivery: async
  agent_timeout_seconds: 30
  sync_timeout_seconds: 5
  max_file_bytes: 1048576
  output:
    kind: json
    file: result.json

tasks:
  ppt_extract:
    name: PPT 内容解析
    accept: [".pptx"]
    min_files: 1
    max_files: 1
    prompt: ppt.md
    delivery: sync
    sync_timeout_seconds: 5
  doc_to_markdown:
    name: 文档转 Markdown
    accept: [".docx", ".pdf"]
    min_files: 1
    max_files: 3
    prompt: md.md
    output:
      kind: files
      dir: output
      allowed_exts: [".md"]
  slow_json:
    name: 慢任务
    accept: [".txt"]
    min_files: 1
    max_files: 1
    prompt: slow.md
    delivery: sync
    sync_timeout_seconds: 1
  fail_json:
    name: 失败任务
    accept: [".txt"]
    min_files: 1
    max_files: 1
    prompt: fail.md
  small:
    name: 小上限
    accept: [".txt"]
    min_files: 1
    max_files: 1
    prompt: small.md
    max_file_bytes: 10

  concurrent_json:
    name: 并发任务
    accept: [".txt"]
    min_files: 1
    max_files: 1
    prompt: concurrent.md
"""


class FakeRunner:
    def __init__(self, settings, spec, workspace, dsh_home, input_files):
        self.settings = settings
        self.spec = spec
        self.workspace = Path(workspace)
        self.input_files = list(input_files)

    def run(self) -> RunOutcome:
        if self.spec.id == "fail_json":
            raise RuntimeError("fake failure")
        if self.spec.id == "slow_json":
            time.sleep(2)
        if self.spec.id == "concurrent_json":
            time.sleep(1.5)

        if self.spec.output.kind == "json":
            data = {
                "fake": True,
                "task": self.spec.id,
                "input": self.input_files,
            }
            path = self.workspace / self.spec.output.file
            atomic_write_json(path, data)
            return RunOutcome(data, self.spec.output.file, [], "stop")

        outputs = []
        for name in self.input_files:
            stem = Path(name).stem
            path = self.workspace / self.spec.output.dir / f"{stem}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# fake\n\nsource: {name}\n", encoding="utf-8")
            outputs.append({
                "name": f"{stem}.md",
                "rel_path": f"{self.spec.output.dir}/{stem}.md",
                "size": path.stat().st_size,
            })
        summary = {"task": self.spec.id, "files": outputs, "count": len(outputs)}
        atomic_write_json(self.workspace / self.spec.output.file, summary)
        return RunOutcome(summary, self.spec.output.file, outputs, "stop")


def _prepare(tmp: Path):
    cfg_dir = tmp / "config"
    prompt_dir = cfg_dir / "prompts"
    prompt_dir.mkdir(parents=True)
    (cfg_dir / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    for name in ("ppt.md", "md.md", "slow.md", "fail.md", "small.md",
                 "concurrent.md"):
        (prompt_dir / name).write_text("fake prompt", encoding="utf-8")

    settings = load_config()
    settings["paths"]["tasks_file"] = cfg_dir / "tasks.yaml"
    settings["paths"]["prompt_dir"] = prompt_dir
    settings["paths"]["workspace_root"] = tmp / "workspaces"
    settings["paths"]["dsh_home"] = tmp / "dsh-home"
    settings["paths"]["db_file"] = tmp / "tasks.db"
    settings["paths"]["tmp_dir"] = tmp / "tmp"
    settings["server"] = dict(settings.get("server") or {}, sync_max_waiters=2)
    return settings


def _wait(client: TestClient, task_id: str, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] in {"completed", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 超时未结束")


def _upload(client, task_type, files):
    return client.post(
        f"/api/tasks/{task_type}",
        files=[("files", (name, data, "application/octet-stream"))
               for name, data in files],
    )


def test_migration(tmp: Path):
    db = tmp / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, mode TEXT, status TEXT, files TEXT,"
        " created_at TEXT, started_at TEXT, finished_at TEXT, error TEXT)")
    conn.execute(
        "INSERT INTO tasks VALUES ('old','single','completed','[]','2026-01-01',NULL,NULL,NULL)")
    conn.commit()
    conn.close()

    init_db(db)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tasks" in tables, tables
    legacy = [t for t in tables if t.startswith("tasks_legacy")]
    assert legacy, tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "task_type" in cols, cols
    assert conn.execute(f'SELECT COUNT(*) FROM "{legacy[0]}"').fetchone()[0] == 1
    conn.close()
    print("ok migration")


def test_api(tmp: Path):
    settings = _prepare(tmp)
    app = create_app(settings, runner_factory=FakeRunner)

    with TestClient(app) as client:
        # 元数据
        meta = client.get("/api/task-types").json()["items"]
        assert {m["id"] for m in meta} == {
            "ppt_extract", "doc_to_markdown", "slow_json", "fail_json", "small",
            "concurrent_json"}
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/").json()["task_types"]

        # 旧接口
        legacy = client.post("/api/tasks")
        assert legacy.status_code == 410
        assert legacy.json()["detail"]["code"] == "legacy_endpoint"

        # 未知任务类型
        r = _upload(client, "nope", [("a.txt", b"x")])
        assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_task_type"

        # 扩展名 / 数量 / 大小
        r = _upload(client, "ppt_extract", [("a.txt", b"x")])
        assert r.status_code == 400 and r.json()["detail"]["code"] == "unsupported_ext"
        r = _upload(client, "ppt_extract", [("a.pptx", b"x"), ("b.pptx", b"y")])
        assert r.status_code == 400 and r.json()["detail"]["code"] == "too_many_files"
        r = _upload(client, "small", [("a.txt", b"x" * 20)])
        assert r.status_code == 413 and r.json()["detail"]["code"] == "file_too_large"

        # sync：直接返回 JSON，且状态接口内联 result.data
        r = _upload(client, "ppt_extract", [("demo.pptx", b"fake")])
        assert r.status_code == 200, r.text
        task_id = r.headers["x-task-id"]
        assert r.json()["fake"] is True
        body = client.get(f"/api/tasks/{task_id}").json()
        assert body["status"] == "completed"
        assert body["result"]["available"] is True
        assert body["result"]["data"]["task"] == "ppt_extract"
        assert body["task_name"] == "PPT 内容解析"
        rr = client.get(f"/api/tasks/{task_id}/result")
        assert rr.status_code == 200 and rr.json()["fake"] is True
        rr = client.get(f"/api/tasks/{task_id}/result?download=1")
        assert rr.status_code == 200
        assert "attachment" in rr.headers.get("content-disposition", "")

        # 轮询取数：删除 result.json 后 available=False，/result 404
        result_path = settings["paths"]["workspace_root"] / task_id / "result.json"
        assert result_path.is_file()
        result_path.unlink()
        body = client.get(f"/api/tasks/{task_id}").json()
        assert body["result"]["available"] is False
        assert client.get(f"/api/tasks/{task_id}/result").status_code == 404

        # async files：多产物 zip
        r = _upload(client, "doc_to_markdown",
                    [("a.docx", b"fake"), ("b.pdf", b"fake")])
        assert r.status_code == 202, r.text
        body = _wait(client, r.json()["task_id"])
        assert body["status"] == "completed"
        assert len(body["result"]["files"]) == 2
        zr = client.get(f"/api/tasks/{body['id']}/result")
        assert zr.status_code == 200
        assert zr.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
            assert sorted(zf.namelist()) == ["a.md", "b.md"]
            assert "# fake" in zf.read("a.md").decode("utf-8")

        # 单文件直返
        r = _upload(client, "doc_to_markdown", [("c.docx", b"fake")])
        body = _wait(client, r.json()["task_id"])
        assert len(body["result"]["files"]) == 1
        one = client.get(f"/api/tasks/{body['id']}/result")
        assert one.status_code == 200
        assert one.headers["content-type"].startswith("text/markdown")
        assert "# fake" in one.text

        # 产物子路径下载 + 路径穿越
        ok = client.get(f"/api/tasks/{body['id']}/files/output/c.md")
        assert ok.status_code == 200 and "# fake" in ok.text
        bad = client.get(f"/api/tasks/{body['id']}/files/..%2F..%2F.env")
        assert bad.status_code in (403, 404)

        # 同名上传去重
        r = _upload(client, "doc_to_markdown",
                    [("same.docx", b"1"), ("same.docx", b"2")])
        body = _wait(client, r.json()["task_id"])
        assert body["input_files"] == ["same.docx", "2_same.docx"], body["input_files"]

        # sync 超时 → 202，任务继续跑
        r = _upload(client, "slow_json", [("slow.txt", b"x")])
        assert r.status_code == 202, r.text
        body = _wait(client, r.json()["task_id"])
        assert body["status"] == "completed"
        assert body["result"]["data"]["task"] == "slow_json"

        # failed 任务
        r = _upload(client, "fail_json", [("bad.txt", b"x")])
        body = _wait(client, r.json()["task_id"])
        assert body["status"] == "failed"
        assert "fake failure" in body["error"]
        fr = client.get(f"/api/tasks/{body['id']}/result")
        assert fr.status_code == 409
        assert fr.json()["detail"]["code"] == "task_failed"

        # 列表过滤
        items = client.get("/api/tasks?task_type=ppt_extract").json()["items"]
        assert items and all(i["task_type"] == "ppt_extract" for i in items)
        assert client.get("/api/tasks?status=bogus").status_code == 400

    print("ok api")


def test_concurrency(tmp: Path):
    settings = _prepare(tmp)
    settings.setdefault("agent", {})["max_concurrency"] = 2
    app = create_app(settings, runner_factory=FakeRunner)

    with TestClient(app) as client:
        r1 = _upload(client, "concurrent_json", [("a.txt", b"x")])
        r2 = _upload(client, "concurrent_json", [("b.txt", b"x")])
        assert r1.status_code == 202 and r2.status_code == 202, (r1.text, r2.text)
        ids = [r1.json()["task_id"], r2.json()["task_id"]]

        deadline = time.time() + 3
        states: list[str] = []
        while time.time() < deadline:
            states = [client.get(f"/api/tasks/{i}").json()["status"] for i in ids]
            if states.count("running") == 2:
                break
            if "completed" in states:
                break
            time.sleep(0.05)
        assert states.count("running") == 2, states

        for task_id in ids:
            assert _wait(client, task_id)["status"] == "completed"

    print("ok concurrency")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        migration_dir = root / "migration"
        migration_dir.mkdir()
        test_migration(migration_dir)
        test_api(root / "app")
        test_concurrency(root / "concurrency")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
