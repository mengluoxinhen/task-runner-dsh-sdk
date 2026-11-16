import os
from contextlib import contextmanager

from deepseek_harness import DeepSeekHarness

from . import config as cfg


@contextmanager
def build_harness(settings: dict):
    """全部参数来自 settings（config/settings.yaml 加载+渲染后），代码零硬编码。

    第三方网关走 OpenAI 兼容协议，SDK 内置 deepseek-official 路由不匹配，
    故 provider 指向 gateway.patch 中注册的自定义路由（见 config.py 渲染）。
    settings["workspace"]/["dsh_home"] 为每任务覆盖项。
    """
    if "gateway_patch_file" not in settings:
        settings = cfg.finalize(settings)
    api_key = settings.get("api_key") or os.environ.get(settings["api_key_env"], "")
    with DeepSeekHarness(
        dsh_home=str(settings.get("dsh_home") or settings["paths"]["dsh_home"]),
        cwd=str(settings.get("workspace") or settings["paths"]["workspace_root"]),
        profile=settings["profile"],
        provider=settings["provider"],
        model=settings["model"],
        reasoning_effort=settings["reasoning_effort"],
        max_tokens=settings["max_tokens"],
        initialize_timeout_seconds=settings["initialize_timeout_seconds"],
        patches=(settings["gateway_patch_file"],),
        env={settings["api_key_env"]: api_key},
    ) as h:
        yield h


class SessionManager:
    """集中管理 session 命名。每任务独立 prefix，避免任务间污染。"""

    def __init__(self, harness, prefix="audit"):
        self.h = harness
        self.prefix = prefix
        self._doc_sessions = {}
        self._global = f"{prefix}-global"

    def for_doc(self, doc_name: str) -> str:
        if doc_name not in self._doc_sessions:
            safe = "".join(c if c.isalnum() else "-" for c in doc_name)[:40]
            self._doc_sessions[doc_name] = f"{self.prefix}-doc-{safe}"
        return self._doc_sessions[doc_name]

    def global_(self) -> str:
        return self._global

    def run(self, prompt: str, session: str) -> str:
        return self.h.run(prompt, session_id=session).final_response
