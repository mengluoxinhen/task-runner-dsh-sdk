import queue
import threading
import traceback
from pathlib import Path
from datetime import datetime

from doc_audit.runner import Runner
from .db import update_task_status


class Worker:
    """单线程串行消费任务队列。每任务新建 harness，session_prefix 带 task_id。"""

    def __init__(self, settings: dict, base_dir: Path):
        self.settings = settings
        self.base_dir = base_dir
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, task_id: str, mode: str, workspace: Path, dsh_home: Path):
        self.q.put((task_id, mode, workspace, dsh_home))

    def _loop(self):
        while True:
            task_id, mode, workspace, dsh_home = self.q.get()
            try:
                update_task_status(task_id, "running",
                                   started_at=datetime.utcnow().isoformat())
                settings_for_task = dict(self.settings)
                settings_for_task["session_prefix"] = f"task-{task_id}"
                runner = Runner(settings_for_task, mode,
                                workspace=workspace, dsh_home=dsh_home)
                runner.run()
                update_task_status(task_id, "completed",
                                   finished_at=datetime.utcnow().isoformat())
            except Exception as e:
                update_task_status(
                    task_id, "failed",
                    error=f"{e}\n{traceback.format_exc()}",
                    finished_at=datetime.utcnow().isoformat(),
                )
            finally:
                self.q.task_done()
