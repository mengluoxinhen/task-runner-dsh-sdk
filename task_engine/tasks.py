"""任务注册表：config/tasks.yaml → TaskSpec 列表。

新增任务 = 在 tasks.yaml 加一个条目 + 在 prompt_dir 放一个 .md，
不需要改 Python 代码。启动时做完整校验，配置有错直接拒绝启动。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath

import yaml

TASK_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PROMPT_VAR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DELIVERY_KINDS = {"async", "sync"}
OUTPUT_KINDS = {"json", "files"}
RESERVED_DIR_PARTS = {"_artifacts", ".dsh", "__pycache__"}
BUILTIN_PROMPT_VARS = {
    "workspace", "task_name", "file_types", "input_files", "output_dir", "constraint",
}


class TaskConfigError(ValueError):
    """tasks.yaml 校验失败。消息里聚合所有问题。"""


@dataclass(frozen=True)
class OutputSpec:
    kind: str                       # json | files
    file: str = "result.json"       # Agent 最终 JSON 文件名（相对 workspace）
    dir: str = "output"             # files 任务的产物目录（相对 workspace）
    allowed_exts: tuple[str, ...] = ()
    min_files: int = 1
    max_files: int = 20
    max_total_bytes: int = 209715200  # files 产物总大小上限 200MB


@dataclass(frozen=True)
class TaskSpec:
    id: str
    name: str
    description: str
    accept: tuple[str, ...]
    min_files: int
    max_files: int
    prompt_file: str
    prompt_text: str
    delivery: str                   # async | sync
    sync_timeout_seconds: float
    agent_timeout_seconds: float
    max_file_bytes: int
    output: OutputSpec
    prompt_vars: dict[str, str] = field(default_factory=dict)

    def public_metadata(self) -> dict:
        out = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "accept": list(self.accept),
            "min_files": self.min_files,
            "max_files": self.max_files,
            "delivery": self.delivery,
            "max_file_bytes": self.max_file_bytes,
            "output": {"kind": self.output.kind},
        }
        if self.output.kind == "json":
            out["output"]["file"] = self.output.file
        else:
            out["output"].update({
                "dir": self.output.dir,
                "allowed_exts": list(self.output.allowed_exts),
                "min_files": self.output.min_files,
                "max_files": self.output.max_files,
            })
        return out


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _as_int(value, field: str, errors: list[str], minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} 必须是整数")
        return minimum
    if value < minimum:
        errors.append(f"{field} 必须 ≥ {minimum}")
        return minimum
    return value


def _as_float(value, field: str, errors: list[str], minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field} 必须是数字")
        return minimum
    if value < minimum:
        errors.append(f"{field} 必须 ≥ {minimum}")
        return minimum
    return float(value)


def _ext_list(value, field: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        errors.append(f"{field} 必须是非空列表")
        return ()
    exts: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.startswith(".") or len(item) < 2:
            errors.append(f"{field} 含非法扩展名: {item!r}（应以 . 开头）")
            continue
        exts.append(item.lower())
    return tuple(dict.fromkeys(exts))


def _safe_relative_dir(value, field: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} 必须是非空相对路径")
        return "output"
    p = PurePath(value.strip())
    if p.is_absolute() or p.drive:
        errors.append(f"{field} 不能是绝对路径/盘符路径: {value!r}")
        return "output"
    parts = p.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        errors.append(f"{field} 不能包含 . / .. / 空路径段: {value!r}")
        return "output"
    if any(part in RESERVED_DIR_PARTS for part in parts):
        errors.append(f"{field} 不能占用保留目录 {sorted(RESERVED_DIR_PARTS)}: {value!r}")
        return "output"
    return str(p).replace("\\", "/")


def load_tasks(settings: dict) -> dict[str, TaskSpec]:
    """读取并校验任务注册表。失败抛 TaskConfigError（聚合全部问题）。"""
    tasks_file = Path(settings["paths"]["tasks_file"])
    prompt_dir = Path(settings["paths"]["prompt_dir"])
    if not tasks_file.is_file():
        raise TaskConfigError(f"任务注册表不存在: {tasks_file}")

    raw = yaml.safe_load(tasks_file.read_text("utf-8")) or {}
    if not isinstance(raw, dict):
        raise TaskConfigError("tasks.yaml 顶层必须是映射")

    task_entries = raw.get("tasks")
    if not isinstance(task_entries, dict) or not task_entries:
        raise TaskConfigError("tasks.yaml 缺少非空的 tasks 映射")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise TaskConfigError("tasks.yaml 的 defaults 必须是映射")

    settings_limits = settings.get("limits", {})
    settings_agent = settings.get("agent", {})

    errors: list[str] = []
    specs: dict[str, TaskSpec] = {}

    for task_id, entry in task_entries.items():
        prefix = f"tasks.{task_id}"
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"{prefix}: id 必须匹配 [a-z0-9_]{{1,64}}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: 必须是映射")
            continue

        conf = _deep_merge(defaults, entry)

        name = conf.get("name") or task_id
        description = conf.get("description") or ""
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name 不能为空")
        if not isinstance(description, str):
            errors.append(f"{prefix}.description 必须是字符串")

        accept = _ext_list(conf.get("accept"), f"{prefix}.accept", errors)
        min_files = _as_int(conf.get("min_files", 1), f"{prefix}.min_files", errors, 1)
        max_files = _as_int(conf.get("max_files", 1), f"{prefix}.max_files", errors, 1)
        if min_files > max_files:
            errors.append(f"{prefix}: min_files 不能大于 max_files")

        delivery = conf.get("delivery", "async")
        if delivery not in DELIVERY_KINDS:
            errors.append(f"{prefix}.delivery 必须是 {'/'.join(sorted(DELIVERY_KINDS))}")
            delivery = "async"

        sync_timeout = _as_float(
            conf.get("sync_timeout_seconds", 300),
            f"{prefix}.sync_timeout_seconds", errors, 1)
        agent_timeout = _as_float(
            conf.get("agent_timeout_seconds", settings_agent.get("timeout_seconds", 900)),
            f"{prefix}.agent_timeout_seconds", errors, 1)
        max_file_bytes = _as_int(
            conf.get("max_file_bytes", settings_limits.get("max_file_bytes", 52428800)),
            f"{prefix}.max_file_bytes", errors, 1)

        prompt_file = conf.get("prompt")
        if not isinstance(prompt_file, str) or not prompt_file.strip():
            errors.append(f"{prefix}.prompt 必须是非空文件名")
            prompt_file = ""
            prompt_text = ""
        else:
            prompt_path = prompt_dir / prompt_file
            if not prompt_path.is_file():
                errors.append(f"{prefix}.prompt 文件不存在: {prompt_path}")
                prompt_text = ""
            else:
                prompt_text = prompt_path.read_text("utf-8")
                if not prompt_text.strip():
                    errors.append(f"{prefix}.prompt 文件为空: {prompt_path}")

        raw_vars = conf.get("prompt_vars") or {}
        if not isinstance(raw_vars, dict):
            errors.append(f"{prefix}.prompt_vars 必须是映射")
            raw_vars = {}
        prompt_vars: dict[str, str] = {}
        for var_name, var_value in raw_vars.items():
            if not isinstance(var_name, str) or not PROMPT_VAR_RE.fullmatch(var_name):
                errors.append(
                    f"{prefix}.prompt_vars 键必须是 [a-z][a-z0-9_]*: {var_name!r}")
                continue
            if var_name in BUILTIN_PROMPT_VARS:
                errors.append(
                    f"{prefix}.prompt_vars 不能覆盖内置占位符: {var_name}")
                continue
            if isinstance(var_value, bool) or not isinstance(
                    var_value, (str, int, float)):
                errors.append(
                    f"{prefix}.prompt_vars.{var_name} 必须是字符串或数字")
                continue
            prompt_vars[var_name] = str(var_value)

        raw_output = conf.get("output") or {}
        if not isinstance(raw_output, dict):
            errors.append(f"{prefix}.output 必须是映射")
            raw_output = {}
        kind = raw_output.get("kind", "json")
        if kind not in OUTPUT_KINDS:
            errors.append(f"{prefix}.output.kind 必须是 {'/'.join(sorted(OUTPUT_KINDS))}")
            kind = "json"

        result_file = raw_output.get("file", "result.json")
        if not isinstance(result_file, str) or not result_file.strip():
            errors.append(f"{prefix}.output.file 必须是非空文件名")
            result_file = "result.json"
        elif PurePath(result_file).name != result_file or PurePath(result_file).drive:
            errors.append(f"{prefix}.output.file 只能是文件名，不能含路径: {result_file!r}")
            result_file = "result.json"
        elif not result_file.lower().endswith(".json"):
            errors.append(f"{prefix}.output.file 必须以 .json 结尾: {result_file!r}")

        out_dir = _safe_relative_dir(
            raw_output.get("dir", "output"), f"{prefix}.output.dir", errors)
        out_exts = _ext_list(
            raw_output.get("allowed_exts", [".md"]),
            f"{prefix}.output.allowed_exts", errors) if kind == "files" else ()
        out_min = _as_int(
            raw_output.get("min_files", 1), f"{prefix}.output.min_files", errors, 1)
        out_max = _as_int(
            raw_output.get("max_files", 20), f"{prefix}.output.max_files", errors, 1)
        if out_min > out_max:
            errors.append(f"{prefix}: output.min_files 不能大于 output.max_files")
        out_total = _as_int(
            raw_output.get("max_total_bytes", 209715200),
            f"{prefix}.output.max_total_bytes", errors, 1)

        output = OutputSpec(
            kind=kind,
            file=result_file,
            dir=out_dir,
            allowed_exts=out_exts,
            min_files=out_min,
            max_files=out_max,
            max_total_bytes=out_total,
        )
        specs[task_id] = TaskSpec(
            id=task_id,
            name=name.strip() if isinstance(name, str) else task_id,
            description=description.strip() if isinstance(description, str) else "",
            accept=accept,
            min_files=min_files,
            max_files=max_files,
            prompt_file=prompt_file,
            prompt_text=prompt_text,
            delivery=delivery,
            sync_timeout_seconds=sync_timeout,
            agent_timeout_seconds=agent_timeout,
            max_file_bytes=max_file_bytes,
            output=output,
            prompt_vars=prompt_vars,
        )

    if errors:
        raise TaskConfigError("tasks.yaml 校验失败:\n- " + "\n- ".join(errors))
    return specs
