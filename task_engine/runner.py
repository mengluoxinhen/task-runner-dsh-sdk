"""通用 one-shot 任务执行器。

输入：TaskSpec + 任务 workspace + 输入文件列表。
输出：RunOutcome（解析后的 result JSON、result 文件相对路径、files 产物清单）。

约定：
- 所有任务把最终 JSON 写入 workspace/<output.file>（默认 result.json），
  该文件是结果的唯一权威来源；Agent 只在最终回复里重复同一 JSON。
- json 任务：result.json 即结果本体；解析失败 → TaskRunError。
- files 任务：产物写 workspace/<output.dir>/；产物校验通过即成功，
  result.json 只是摘要，可回退/合成。
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import fill_template
from .llm import make_harness
from .tasks import TaskSpec


class TaskRunError(RuntimeError):
    """任务执行/产物校验失败。消息会原样进入 tasks.error。"""


@dataclass
class RunOutcome:
    result: object
    result_rel_path: str
    outputs: list[dict] = field(default_factory=list)
    finish_reason: str | None = None


def strip_json_fences(text: str) -> str:
    raw = (text or "").strip().lstrip("\ufeff")
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    return raw


def parse_json_text(text: str, *, max_bytes: int | None = None) -> object:
    """解析 Agent 输出：整体 loads → 首 { 尾 } 回退 → 首 [ 尾 ] 回退。"""
    raw = strip_json_fences(text)
    if not raw:
        raise TaskRunError("输出为空，没有可解析的 JSON")
    if max_bytes is not None and len(raw.encode("utf-8")) > max_bytes:
        raise TaskRunError(f"JSON 输出超过上限 {max_bytes} 字节")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = raw.find(open_ch)
        end = raw.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise TaskRunError("无法从 Agent 输出中解析出合法 JSON")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json_file(path: Path, *, max_bytes: int | None = None) -> object:
    if not path.is_file():
        raise FileNotFoundError(path)
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise TaskRunError(f"结果文件超过上限 {max_bytes} 字节: {path}")
    return parse_json_text(path.read_text("utf-8"))


class _HarnessRun:
    """运行一次 harness.run，并用 watchdog 在超时后 close() 强杀。"""

    def __init__(self, settings: dict, timeout_seconds: float):
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def run(self, prompt: str, session_id: str, on_notification=None):
        harness = make_harness(self.settings)
        done = threading.Event()
        timed_out = threading.Event()
        closed = threading.Event()
        close_lock = threading.Lock()

        def close_harness() -> None:
            with close_lock:
                if closed.is_set():
                    return
                closed.set()
                try:
                    harness.close()
                except Exception:
                    pass

        def watchdog() -> None:
            if done.is_set():
                return
            timed_out.set()
            close_harness()

        timer = threading.Timer(self.timeout_seconds, watchdog)
        timer.daemon = True
        timer.start()
        try:
            result = harness.run(
                prompt, session_id=session_id, on_notification=on_notification)
        except Exception as exc:
            if timed_out.is_set():
                raise TaskRunError(
                    f"任务超时（{self.timeout_seconds:g}s），已强制终止 Agent"
                ) from exc
            raise
        finally:
            done.set()
            timer.cancel()
            close_harness()

        if timed_out.is_set():
            raise TaskRunError(f"任务超时（{self.timeout_seconds:g}s），已强制终止 Agent")
        return result


class Runner:
    def __init__(self, settings: dict, spec: TaskSpec, workspace: Path,
                 dsh_home: Path, input_files: list[str]):
        self.settings = settings
        self.spec = spec
        self.workspace = Path(workspace).resolve()
        self.dsh_home = Path(dsh_home).resolve()
        self.input_files = list(input_files)

    # ── prompt ─────────────────────────────────────────────────
    def render_prompt(self) -> str:
        return fill_template(
            self.spec.prompt_text,
            workspace=str(self.workspace),
            task_name=self.spec.name,
            file_types=" / ".join(self.spec.accept),
            input_files=", ".join(self.input_files),
            output_dir=self.spec.output.dir,
            constraint=self.settings["prompt_vars"]["final_output_constraint"],
            **self.spec.prompt_vars,
        )

    # ── 进度日志 ────────────────────────────────────────────────
    def _notification_logger(self, task_id: str):
        """把 SDK 通知打到服务日志，并将事件原样追加到 _artifacts/agent_events.jsonl。"""
        log_path = self.workspace / "_artifacts" / "agent_events.jsonl"

        def emit(message: str) -> None:
            print(f"[agent {task_id}] {message}", flush=True)

        def on_notification(notification) -> None:
            try:
                method = getattr(notification, "method", "")
                payload = getattr(notification, "payload", {}) or {}
                if method == "session.status":
                    emit(f"status={payload.get('status')}")
                    return
                if method != "session.event":
                    return
                event = payload.get("event")
                if not isinstance(event, dict):
                    return
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                except OSError:
                    pass
                event_type = str(event.get("type") or "event")
                seq = event.get("seq")
                detail = json.dumps(event.get("data"), ensure_ascii=False)
                if len(detail) > 1000:
                    detail = detail[:1000] + "…"
                prefix = f"{event_type}" + (f" seq={seq}" if seq is not None else "")
                emit(f"{prefix} {detail}")
            except Exception:
                # 日志失败不能影响任务本身
                pass

        return on_notification

    # ── main ───────────────────────────────────────────────────
    def run(self) -> RunOutcome:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.dsh_home.mkdir(parents=True, exist_ok=True)

        harness_settings = dict(self.settings)
        harness_settings["workspace"] = str(self.workspace)
        harness_settings["dsh_home"] = str(self.dsh_home)
        session_id = self.settings.get("session_prefix") or f"task-{self.spec.id}"

        prompt = self.render_prompt()
        task_id = str(self.settings.get("task_id") or self.workspace.name)
        print(f"[task] type={self.spec.id} workspace={self.workspace} session={session_id}",
              flush=True)

        result = _HarnessRun(harness_settings, self.spec.agent_timeout_seconds).run(
            prompt, session_id, on_notification=self._notification_logger(task_id))
        print(f"[agent {task_id}] finished finish_reason="
              f"{getattr(result, 'finish_reason', None)!r} "
              f"events={len(getattr(result, 'events', []) or [])}", flush=True)

        final_response = result.final_response or ""
        if self.spec.output.kind == "json":
            return self._finish_json(final_response, result)
        return self._finish_files(final_response, result)

    # ── json 任务 ──────────────────────────────────────────────
    def _finish_json(self, final_response: str, run_result) -> RunOutcome:
        result_path = self.workspace / self.spec.output.file
        max_bytes = self._result_max_bytes()
        parsed = None
        source = ""

        if result_path.is_file():
            try:
                parsed = read_json_file(result_path, max_bytes=max_bytes)
                source = "result.json"
            except (TaskRunError, json.JSONDecodeError, UnicodeDecodeError):
                parsed = None

        if parsed is None:
            if not final_response.strip():
                raise TaskRunError(
                    "Agent 没有返回内容，也没有写出合法 result.json。"
                    + self._diagnose(run_result)
                )
            try:
                parsed = parse_json_text(final_response, max_bytes=max_bytes)
                source = "final_response"
            except TaskRunError:
                self._save_raw(final_response)
                raise TaskRunError(
                    "无法从 Agent 输出中解析出合法 JSON，原始回复已保存到 "
                    f"_artifacts/agent_raw.txt。{self._diagnose(run_result)}"
                )

        atomic_write_json(result_path, parsed)
        print(f"[task] result via {source or 'result.json'} → {result_path}", flush=True)
        return RunOutcome(
            result=parsed,
            result_rel_path=self.spec.output.file,
            outputs=[],
            finish_reason=getattr(run_result, "finish_reason", None),
        )

    # ── files 任务 ─────────────────────────────────────────────
    def _finish_files(self, final_response: str, run_result) -> RunOutcome:
        out_dir = self.workspace / self.spec.output.dir
        outputs = self._collect_outputs(out_dir)
        if not outputs:
            raise TaskRunError(
                f"Agent 未在 {self.spec.output.dir}/ 产出任何 "
                f"{' / '.join(self.spec.output.allowed_exts)} 结果文件。"
                + self._diagnose(run_result)
            )

        summary = None
        result_path = self.workspace / self.spec.output.file
        max_bytes = self._result_max_bytes()
        if result_path.is_file():
            try:
                summary = read_json_file(result_path, max_bytes=max_bytes)
            except (TaskRunError, json.JSONDecodeError, UnicodeDecodeError):
                summary = None
        if summary is None and final_response.strip():
            try:
                summary = parse_json_text(final_response, max_bytes=max_bytes)
            except TaskRunError:
                summary = None
        if not isinstance(summary, dict):
            summary = {
                "task": self.spec.id,
                "files": [{"name": o["name"], "size": o["size"]} for o in outputs],
                "count": len(outputs),
                "note": "Agent 未提供合法 JSON 摘要，已按产物清单合成。",
            }

        atomic_write_json(result_path, summary)
        print(f"[task] files={len(outputs)} → {out_dir}", flush=True)
        return RunOutcome(
            result=summary,
            result_rel_path=self.spec.output.file,
            outputs=outputs,
            finish_reason=getattr(run_result, "finish_reason", None),
        )

    def _collect_outputs(self, out_dir: Path) -> list[dict]:
        if not out_dir.is_dir():
            return []
        allowed = {e.lower() for e in self.spec.output.allowed_exts}
        found: list[dict] = []
        total = 0
        for path in sorted(out_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() not in allowed:
                continue
            size = path.stat().st_size
            if size <= 0:
                continue
            total += size
            rel = path.relative_to(self.workspace).as_posix()
            found.append({
                "name": path.relative_to(out_dir).as_posix(),
                "rel_path": rel,
                "size": size,
            })
        if len(found) < self.spec.output.min_files:
            raise TaskRunError(
                f"产物数量不足：需要 ≥ {self.spec.output.min_files} 个，实际 {len(found)} 个"
            )
        if len(found) > self.spec.output.max_files:
            raise TaskRunError(
                f"产物数量超限：最多 {self.spec.output.max_files} 个，实际 {len(found)} 个"
            )
        if total > self.spec.output.max_total_bytes:
            raise TaskRunError(
                f"产物总大小超限：{total} > {self.spec.output.max_total_bytes} 字节"
            )
        return found

    # ── helpers ────────────────────────────────────────────────
    def _result_max_bytes(self) -> int:
        return int(self.settings.get("limits", {}).get("result_max_bytes", 20971520))

    def _save_raw(self, text: str) -> None:
        path = self.workspace / "_artifacts" / "agent_raw.txt"
        try:
            atomic_write_text(path, text)
        except OSError:
            pass

    @staticmethod
    def _diagnose(run_result) -> str:
        parts = [f"finish_reason={getattr(run_result, 'finish_reason', None)!r}"]
        for event in reversed(getattr(run_result, "events", []) or []):
            try:
                text = json.dumps(event, ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            if "error" in text.lower():
                parts.append("event=" + text[:800])
                break
        parts.append(
            "疑似网关 403 配额耗尽/路由失败或模型未返回内容，"
            "请检查网关状态、GATEWAY_API_KEY 与 settings.yaml 的 model/provider。"
        )
        return "；".join(parts)
