"""配置加载：settings.yaml 是全局参数唯一来源。

职责：
1. 注入项目根 .env（若存在）到环境变量（简易解析，不引第三方依赖）。
2. 展开 ${VAR} / ${VAR:default} 引用。
3. paths.* 解析为绝对路径。
4. 渲染 config/gateway.patch.yml 模板 → config/_generated/gateway.patch.yml，
   使网关参数（base_url/model/协议等）只在 settings.yaml 维护一份。

任务类型与提示词不在这里加载：见 task_engine/tasks.py。
"""
import os
import re
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _load_dotenv() -> None:
    f = BASE_DIR / ".env"
    if not f.is_file():
        return
    for line in f.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _expand_env(obj):
    if isinstance(obj, str):
        m = _ENV_REF.fullmatch(obj.strip())
        if m:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_REF.sub(lambda x: os.environ.get(x.group(1), x.group(2) or ""), obj)
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    return obj


def _resolve_paths(settings: dict) -> dict:
    for k, v in settings.get("paths", {}).items():
        p = Path(v)
        settings["paths"][k] = p if p.is_absolute() else BASE_DIR / p
    return settings


def render_gateway_patch(settings: dict) -> Path:
    """模板占位符用顶层配置项填充，生成给 SDK patches= 用的实际文件。"""
    tpl = Path(settings["paths"]["gateway_patch_template"]).read_text("utf-8")
    out = Path(settings["paths"]["gateway_patch_rendered"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tpl.format(
        provider_id=settings["provider"],
        base_url=settings["base_url"],
        api_key_env=settings["api_key_env"],
        api_style=settings["api_style"],
        model=settings["model"],
        context_window=settings["context_window"],
        max_tokens=settings["max_tokens"],
        supports_developer_role=str(settings["supports_developer_role"]).lower(),
    ), "utf-8")
    return out


def fill_template(tpl: str, **tokens) -> str:
    """<<token>> 替换：prompt 模板无需 format 转义 JSON 花括号。"""
    for k, v in tokens.items():
        tpl = tpl.replace(f"<<{k}>>", str(v))
    return tpl


def load_config() -> dict:
    _load_dotenv()
    settings = _expand_env(yaml.safe_load(
        (BASE_DIR / "config" / "settings.yaml").read_text("utf-8")))
    return finalize(settings)


def finalize(settings: dict) -> dict:
    """对（可能来自测试脚本的）原始 settings dict 补齐路径与渲染产物。"""
    settings = _resolve_paths(settings)
    settings["gateway_patch_file"] = str(render_gateway_patch(settings))
    return settings
