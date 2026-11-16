import json
import re
from pathlib import Path

from .llm import build_harness


class Runner:
    """one-shot 执行器：prompt 模板、输出合同、路径全部来自 settings（见 config/）。

    settings 兼容两种来源：load_config() 产物（Path 型 paths）与测试脚本的
    原始 yaml dict（字符串 paths、无 prompts/prompts_file）。
    """

    def __init__(self, settings: dict, mode: str, workspace: Path = None, dsh_home: Path = None):
        if "gateway_patch_file" not in settings:
            from . import config as cfg
            settings = cfg.finalize(settings)
        self.settings = settings
        if mode not in settings["prompt_templates"]:
            raise ValueError(f"未知 mode: {mode}，可用: {list(settings['prompt_templates'])}")

        self.mode = mode
        paths = settings["paths"]
        self.workspace = Path(workspace or paths["workspace_root"]).resolve()
        self.dsh_home = Path(dsh_home or paths["dsh_home"]).resolve()

    def _render_prompt(self) -> str:
        from .config import fill_template
        s = self.settings
        return fill_template(
            s["prompt_templates"][self.mode],
            workspace=str(self.workspace),
            file_types=" / ".join(s["limits"]["allowed_exts"]),
            min_confidence=s["prompt_vars"]["min_confidence"],
            constraint=s["prompt_vars"]["final_output_constraint"],
        )

    def run(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.dsh_home.mkdir(parents=True, exist_ok=True)

        harness_settings = dict(self.settings)
        harness_settings["workspace"] = str(self.workspace)
        harness_settings["dsh_home"] = str(self.dsh_home)
        session_id = self.settings.get("session_prefix", f"audit-{self.mode}")

        prompt = self._render_prompt()
        print(f"[one-shot] mode={self.mode}  workspace={self.workspace}  session={session_id}")

        with build_harness(harness_settings) as harness:
            result = harness.run(prompt, session_id=session_id)

        report = self._parse_report(result.final_response)

        report_path = self.workspace / self.settings["limits"]["report_filename"]
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[one-shot] 报告写入：{report_path}")
        return report

    def _parse_report(self, text: str) -> dict:
        raw = text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass

        return {
            "mode": self.mode,
            "error": "无法从 Agent 输出中解析出合法 JSON 报告",
            "raw_response": raw[:2000],
        }
