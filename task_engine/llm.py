import os

from deepseek_harness import DeepSeekHarness

from . import config as cfg


def make_harness(settings: dict) -> DeepSeekHarness:
    """构造 harness。全部参数来自 settings（config/settings.yaml 加载+渲染后）。

    第三方网关走 OpenAI 兼容协议，SDK 内置 deepseek-official 路由不匹配，
    故 provider 指向 gateway.patch 中注册的自定义路由（见 config.py 渲染）。
    settings["workspace"]/["dsh_home"] 为每任务覆盖项。
    调用方负责 close（Runner 里有 watchdog 兜底强杀）。
    """
    if "gateway_patch_file" not in settings:
        settings = cfg.finalize(settings)
    api_key = settings.get("api_key") or os.environ.get(settings["api_key_env"], "")
    env = {settings["api_key_env"]: api_key}
    permission_mode = str(
        settings.get("agent", {}).get("permission_mode") or "").strip()
    if permission_mode:
        env["DSH_PERMISSION_MODE"] = permission_mode
    return DeepSeekHarness(
        dsh_home=str(settings.get("dsh_home") or settings["paths"]["dsh_home"]),
        cwd=str(settings.get("workspace") or settings["paths"]["workspace_root"]),
        profile=settings["profile"],
        provider=settings["provider"],
        model=settings["model"],
        reasoning_effort=settings["reasoning_effort"],
        max_tokens=settings["max_tokens"],
        initialize_timeout_seconds=settings["initialize_timeout_seconds"],
        request_timeout_seconds=settings.get("request_timeout_seconds"),
        patches=(settings["gateway_patch_file"],),
        env=env,
    )
