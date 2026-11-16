import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/tasks.db")   # 由 init_db(path=...) 注入配置值
SCHEMA_VERSION = 1

_ALLOWED_UPDATE_COLUMNS = {
    "status", "started_at", "finished_at", "error", "output_kind",
    "output_files", "result_rel_path", "duration_ms", "session_id",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    delivery TEXT,
    input_files TEXT NOT NULL,
    output_kind TEXT,
    output_files TEXT,
    result_rel_path TEXT,
    error TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_type_created ON tasks(task_type, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at DESC)",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {r["name"] for r in rows}


def init_db(path=None):
    global DB_PATH
    if path is not None:
        DB_PATH = Path(path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with closing(_connect()) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tasks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone() is not None

        if version < SCHEMA_VERSION and has_tasks:
            columns = _table_columns(conn, "tasks")
            if "task_type" not in columns:
                legacy = "tasks_legacy"
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (legacy,),
                ).fetchone():
                    legacy = f"tasks_legacy_{int(time.time())}"
                conn.execute(f'ALTER TABLE tasks RENAME TO "{legacy}"')
                print(f"[db] 旧任务表已迁移为 {legacy}（只读保留）", flush=True)

        conn.execute(_SCHEMA)
        for sql in _INDEXES:
            conn.execute(sql)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()


def create_task(task_id, task_type, delivery, input_files, output_kind,
                session_id=None):
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, task_type, status, delivery, input_files, output_kind,
                session_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                task_id,
                task_type,
                "pending",
                delivery,
                json.dumps(list(input_files), ensure_ascii=False),
                output_kind,
                session_id or f"task-{task_id}",
                now_iso(),
            ),
        )
        conn.commit()


def update_task_status(task_id, status, **kw):
    sets = ["status=?"]
    vals: list = [status]
    for key, value in kw.items():
        if key not in _ALLOWED_UPDATE_COLUMNS:
            raise ValueError(f"不允许更新列: {key}")
        sets.append(f"{key}=?")
        vals.append(value)
    vals.append(task_id)
    with closing(_connect()) as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("input_files", "output_files"):
        value = data.get(key)
        if not value:
            data[key] = []
            continue
        try:
            data[key] = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            data[key] = []
    return data


def get_task(task_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_tasks(limit=50, offset=0, task_type=None, status=None):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where = []
    params: list = []
    if task_type:
        where.append("task_type=?")
        params.append(task_type)
    if status:
        where.append("status=?")
        params.append(status)
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
