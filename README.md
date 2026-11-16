# doc_audit_web — 文档校核 Web 服务（dsh harness + 任意 OpenAI 兼容网关）

上传 docx/pdf，Agent 在隔离沙箱内自主完成校核：`single` 模式做文档自检（只报告不修改），`multi` 模式做跨文档矛盾比对并可自动修正（修改前先读、改后必验）。每个任务独立 workspace + 独立 session，服务单线程串行消费队列。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入你的 GATEWAY_API_KEY / GATEWAY_BASE_URL
python main.py              # http://localhost:8000
```

> Windows 提示：`data/` 目录需要当前用户完全控制权限，否则 dsh 沙箱 ACL 初始化失败。若报错执行：
> `icacls data /grant %USERNAME%:(OI)(CI)F`

## API

```bash
# 提交任务（single：1 个文件；multi：2–10 个文件）
curl -F "mode=single" -F "files=@a.docx" http://localhost:8000/api/tasks
curl -F "mode=multi"  -F "files=@a.docx" -F "files=@b.pdf" http://localhost:8000/api/tasks

# 查询
curl http://localhost:8000/api/tasks/{task_id}            # 状态 pending/running/completed/failed
curl http://localhost:8000/api/tasks/{task_id}/report     # 下载 JSON 报告
curl http://localhost:8000/api/tasks/{task_id}/files/{文件名}  # 下载 Agent 修正后的文档
```

## 配置：全部参数集中在 `config/settings.yaml`

代码中没有硬编码参数；改行为不改代码。

| 分组 | 关键项 | 说明 |
|------|--------|------|
| 网关/SDK | `base_url` `model` `provider` `api_style` `profile` `reasoning_effort` `max_tokens` `context_window` `supports_developer_role` `initialize_timeout_seconds` | dsh SDK 构建 harness 的全部参数 |
| 密钥 | `api_key_env` + `${GATEWAY_API_KEY}` | 密钥只存在于环境变量 / `.env`（已 gitignore），任何配置文件不含明文 |
| server | `host` `port` | 监听地址 |
| limits | `allowed_exts` `multi_max_files` | 上传约束 |
| paths | `workspace_root` `dsh_home` `db_file` 等 | 全部相对路径自动解析到项目根 |
| prompt_vars | `min_confidence` `final_output_constraint` | 自动修正门槛、输出合同 |
| limits | `report_filename` | 报告文件名（相对任务 workspace，勿写成路径） |

**换模型**：只改 `settings.yaml` 的 `model` 一项——`doc_audit/config.py` 会把它连同 base_url/协议/上下文窗口一起渲染进 `config/_generated/gateway.patch.yml`（该目录已 gitignore），作为 dsh profile patch 注入 `llm-pi-ai` 自定义 provider 路由。第三方网关不走 SDK 内置的 Anthropic 协议路由，这一层路由是必需的。

**改任务内容**：`config/prompts.yaml` 是任务逻辑的唯一入口（占位符 `<<workspace>>` `<<file_types>>` `<<min_confidence>>` `<<constraint>>` 自动替换，JSON 示例无需转义）。按你的工作流新增 skill：把 `<name>/SKILL.md` 放进任务 workspace 的 `.dsh/skills/`（运行时原生扫描该目录），prompt 里引用 skill 名即可。

## 目录结构

```
config/           settings.yaml · gateway.patch.yml(模板) · prompts.yaml
doc_audit/        config.py(加载/渲染) · llm.py(harness 构建) · runner.py(one-shot 执行)
web/              app.py(FastAPI) · worker.py(串行队列) · db.py(sqlite)
data/             运行时产物（gitignore）；test_docs/ 为示例文档
main.py           入口
```

## 报告格式

```json
{ "mode": "multi", "documents": [...], "findings": [
    { "kind": "conflict", "severity": "high", "docs": ["a.docx","b.pdf"],
      "locations": [...], "detail": {...}, "evidence": {...},
      "confidence": 0.92, "suggested": {...}, "action": "auto_applied|reported" }
  ],
  "auto_applied": 3, "skipped": [ {"target": "b.pdf", "reason": "..."} ] }
```

解析失败时报告为 `{ "mode", "error", "raw_response" }`，不抛异常、任务仍计 completed。

## 进阶

基于本服务构建自己的任务（改 prompt / 加 mode / 挂 skill / 换模型 / 排障），见 **[docs/任务改造指导手册.md](docs/任务改造指导手册.md)**。
