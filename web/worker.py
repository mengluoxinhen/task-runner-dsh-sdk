import itertools
import json
import queue
import threading
import time
import traceback
from pathlib import Path

from task_engine.runner import Runner
from task_engine.tasks import TaskSpec

from .db import now_iso, update_task_status
from .waiter import TaskWaiters


class Worker:
    """固定并发数的任务执行池。

    - 多个 worker 线程共享同一优先队列；sync 任务优先级更高（0），async 为 1，
      同优先级按提交顺序。并发数由 agent.max_concurrency 控制。
    - 每个任务独立 workspace + 独立 session（task-{id}）。
    - 完成/失败后触发 waiter，sync 请求据此返回。
    """

    def __init__(self, settings: dict, tasks: dict[str, TaskSpec], base_dir: Path,
                 runner_factory=None):
        self.settings = settings
        self.tasks = tasks
        self.base_dir = Path(base_dir)
        self.runner_factory = runner_factory or Runner
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.waiters = TaskWaiters()
        self._seq = itertools.count()
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        raw = (settings.get("agent") or {}).get("max_concurrency", 1)
        try:
            self.concurrency = max(1, int(raw))
        except (TypeError, ValueError):
            self.concurrency = 1

    def _next_seq(self) -> int:
        with self._seq_lock:
            return next(self._seq)

    def start(self):
        if any(t.is_alive() for t in self._threads):
            return
        self._threads = [
            threading.Thread(target=self._loop, daemon=True,
                             name=f"task-worker-{i}")
            for i in range(self.concurrency)
        ]
        print(f"[worker] concurrency={self.concurrency}", flush=True)
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        for _ in range(self.concurrency):
            self.q.put((999, self._next_seq(), None))

    def submit(self, task_id: str, spec: TaskSpec, workspace: Path,
               dsh_home: Path, input_files: list[str]) -> threading.Event:
        event = self.waiters.register(task_id)
        priority = 0 if spec.delivery == "sync" else 1
        self.q.put((
            priority,
            self._next_seq(),
            (task_id, spec, Path(workspace), Path(dsh_home), list(input_files)),
        ))
        return event

    def _loop(self):
        while not self._stop.is_set():
            try:
                _, _, item = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self.q.task_done()
                break

            task_id, spec, workspace, dsh_home, input_files = item
            started = time.monotonic()
            try:
                update_task_status(task_id, "running", started_at=now_iso())
                settings_for_task = dict(self.settings)
                settings_for_task["session_prefix"] = f"task-{task_id}"
                runner = self.runner_factory(
                    settings_for_task, spec, workspace, dsh_home, input_files)
                outcome = runner.run()
                duration_ms = int((time.monotonic() - started) * 1000)
                update_task_status(
                    task_id,
                    "completed",
                    finished_at=now_iso(),
                    duration_ms=duration_ms,
                    error=None,
                    output_kind=spec.output.kind,
                    output_files=json.dumps(outcome.outputs, ensure_ascii=False),
                    result_rel_path=outcome.result_rel_path,
                )
            except Exception as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                detail = f"{exc}\n{traceback.format_exc()}"
                update_task_status(
                    task_id,
                    "failed",
                    finished_at=now_iso(),
                    duration_ms=duration_ms,
                    error=detail[:12000],
                )
            finally:
                self.waiters.finish(task_id)
                self.q.task_done()
