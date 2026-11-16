"""同步任务等待事件注册表。

必须先 register 再入队，避免任务先完成、Event 后注册的竞态；
worker 在 finally 中 finish（set + pop）。
"""
import threading


class TaskWaiters:
    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def register(self, task_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._events[task_id] = event
        return event

    def finish(self, task_id: str) -> None:
        with self._lock:
            event = self._events.pop(task_id, None)
        if event is not None:
            event.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._events)
