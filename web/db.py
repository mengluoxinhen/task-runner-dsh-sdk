import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/tasks.db")   # 由 init_db(path=...) 注入配置值


def init_db(path=None):
    global DB_PATH
    if path is not None:
        DB_PATH = Path(path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                files TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error TEXT
            )
        """)


def create_task(task_id, mode, files):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO tasks (id, mode, status, files, created_at) VALUES (?,?,?,?,?)",
            (task_id, mode, "pending", json.dumps(files), datetime.utcnow().isoformat()),
        )


def update_task_status(task_id, status, **kw):
    sets = ["status=?"]
    vals = [status]
    for k, v in kw.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(task_id)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)


def get_task(task_id):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(limit=50, offset=0):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
