# task-runner — 配置驱动的通用任务处理器（dsh harness + OpenAI 兼容网关）

上传任务允许的文件（1 个或多个）→ Agent 在隔离 workspace 内按预设提示词执行 → 结果统一为 **JSON** 或 **文件产物**。任务类型、可接收格式、提示词、输出形态、交付方式全部在配置里声明，新增任务不改 Python 代码。

内置两个示例：

| 任务类型 | 输入 | 输出 | 交付 |
|---|---|---|---|
| `ppt_extract` | `.pptx`（1 个） | 结构化 JSON（每页标题/正文/表格/图表/备注/图片） | `sync`：POST 直接返回结果 |
| `ppt_extract_assets` | `.pptx`（1 个） | `output/result.json` + 每页截图 `output/slides/*.png` | `async`：轮询，按文件 URL 取 JSON 与图片 |
| `doc_to_markdown` | `.docx` / `.pdf` / `.pptx`（1–5 个） | 每个输入一个 `.md` | `async`：轮询，完成后取 zip |

> 旧的 `single` / `multi` 文档校核接口已移除；`POST /api/tasks` 现在返回 `410 Gone`。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入 GATEWAY_API_KEY / GATEWAY_BASE_URL
python main.py              # http://localhost:11443  （/docs 可看 OpenAPI）
```

Windows 提示：`data/` 目录需要当前用户完全控制权限，否则 dsh 沙箱 ACL 初始化失败：

```powershell
icacls data /grant %USERNAME%:(OI)(CI)F
```

## API

```bash
# 任务类型元数据（accept / 文件数量 / output.kind / delivery）
curl http://localhost:11443/api/task-types

# 提交：任务类型在路径里
curl -F "files=@demo.pptx" http://localhost:11443/api/tasks/ppt_extract
#   sync 任务：阻塞到完成，200 直接返回 result JSON（响应头 X-Task-Id）
#   超时/排队过长：202 + task_id，任务继续跑，改用轮询

curl -F "files=@demo.pptx" http://localhost:11443/api/tasks/ppt_extract_assets
#   async 任务：轮询 /api/tasks/{id}，result.files 里同时有 result.json 和 slides/*.png 的 url
#   图片可直接按 url 取；/api/tasks/{id}/result 会打包 zip

curl -F "files=@a.docx" -F "files=@b.pdf" http://localhost:11443/api/tasks/doc_to_markdown
#   async 任务：202 {task_id, status_url, result_url}

# 轮询：completed 且是 JSON 任务时，response.result.data 就是结果本体
curl http://localhost:11443/api/tasks/{task_id}

# 显式取结果：JSON 任务返回 JSON；文件任务单文件直返、多文件打包 zip
curl http://localhost:11443/api/tasks/{task_id}/result
curl -OJ http://localhost:11443/api/tasks/{task_id}/result?download=1

# 任务列表（可过滤）与 workspace 文件（含 output/ 子目录）
curl "http://localhost:11443/api/tasks?task_type=ppt_extract&status=completed"
curl -OJ http://localhost:11443/api/tasks/{task_id}/files/output/a.md

# 健康检查
curl http://localhost:11443/health
```

状态机：`pending → running → completed | failed`。

## 结果合同

**JSON 任务**：Agent 把最终 JSON 写入任务 workspace 的 `result.json`，服务端校验后原子写回（`result.json.tmp` → `os.replace`）。`result.json` 是唯一权威来源：

- 轮询 `GET /api/tasks/{id}` 到 `completed` 时，服务端**现读该文件**并放入 `result.data`，前端拿到即可用；
- `GET /api/tasks/{id}/result` 可重复取数，`?download=1` 转附件；
- 文件被外部删除时状态仍为 completed，但 `result.available=false`，`/result` 返回 404。

**文件任务**：产物写 `workspace/output/`（目录可配置），校验数量/扩展名/非空/总大小；无产物即 `failed`。`result.json` 只是摘要，缺失时服务端用产物清单合成。`/result`：单文件直返，多文件现场打 zip（临时文件自动清理）。

文件任务也可以把结构化 JSON 作为产物放进 `output/`（例如 `ppt_extract_assets` 的 `output/result.json`）：它会和图片一起出现在 `GET /api/tasks/{id}` 的 `result.files[].url` 里，前端按 URL 单独取 JSON 和图片；`/result` 仍返回 zip。

失败信息在任务的 `error` 字段；JSON 任务解析失败时原始回复保存在 `_artifacts/agent_raw.txt`。

## 新增一个任务（零代码）

1. 在 `config/prompts/` 新建提示词，例如 `summarize.md`；
2. 在 `config/tasks.yaml` 加一个条目：

```yaml
tasks:
  summarize:
    name: 文档摘要
    description: 对上传文档生成结构化摘要
    accept: [".docx", ".pdf"]
    min_files: 1
    max_files: 3
    prompt: summarize.md
    delivery: async            # 或 sync
    output:
      kind: json               # 或 files（配合 output.dir / allowed_exts）
      file: result.json
```

3. 重启服务。`GET /api/task-types` 会出现新类型，`POST /api/tasks/summarize` 即可调用。

提示词可用占位符：`<<workspace>>`、`<<task_name>>`、`<<file_types>>`、`<<input_files>>`、`<<output_dir>>`、`<<constraint>>`（全局输出合同，见 `settings.yaml.prompt_vars`）。

任务还可以在 `tasks.yaml` 里声明自己的占位符，例如 `ppt_extract_assets` 用 `prompt_vars.skill_name` 指定要加载的 skill，提示词里写 `<<skill_name>>` 即可。自定义变量不能覆盖上面 6 个内置占位符。

## 配置

| 文件 | 内容 | 改动频率 |
|---|---|---|
| `config/settings.yaml` | 网关/SDK、server、agent 超时、上传大小、paths、输出合同 | 高 |
| `config/tasks.yaml` | 任务注册表：格式/数量/提示词/输出/交付方式 | **做新任务就改它** |
| `config/prompts/*.md` | 每个任务的提示词 | **做新任务就改它** |
| `.env`（gitignore） | `GATEWAY_API_KEY`、`GATEWAY_BASE_URL` | 换密钥时 |
| `config/gateway.patch.yml` | provider 路由模板（占位符自动填充） | 极低 |

**换模型**：只改 `settings.yaml` 的 `model`，`task_engine/config.py` 会连同 base_url/协议/上下文窗口重新渲染 `config/_generated/gateway.patch.yml`。第三方网关保持 `supports_developer_role: false`。

## 目录结构

```
config/           settings.yaml · tasks.yaml · prompts/*.md · gateway.patch.yml(模板)
task_engine/      config.py(加载/渲染) · tasks.py(注册表) · llm.py(harness) · runner.py(执行/产物校验)
web/              app.py(FastAPI) · worker.py(并发队列) · waiter.py(sync 等待) · db.py(sqlite)
tests/            test_api.py（FakeRunner，无 LLM）
data/             运行时产物（gitignore）；test_docs/ 为示例文档；e2e_tasks.py 真实 e2e 脚本
main.py           入口（单进程 workers=1）
```

## 执行模型

- 多个 worker 线程共享同一优先队列，同时执行最多 `agent.max_concurrency` 个任务（环境变量 `TASK_MAX_CONCURRENCY`，默认 2）；所有用户共享该并发池，超出的任务排队。`sync` 任务优先出队。Worker 必须单进程运行（queue/Event 在进程内），`main.py` 已固定 `workers=1`。
- `sync` 等待超过任务配置的 `sync_timeout_seconds`（默认 300s）或并发等待名额已满时，返回 `202`，任务继续执行，可改轮询。
- 每个任务独立 workspace + 独立 session（`task-{id}`），长任务不会污染其他任务。
- Agent 硬超时由 `agent.timeout_seconds` 控制（默认 900s），超时会强制关闭 harness，任务转 `failed`。
- 沙箱权限档位由 `agent.permission_mode`（环境变量 `DSH_PERMISSION_MODE`）控制，默认 `danger-full-access`，以兼容打包 exe 内沙箱执行器无法启动的情况。该档位会关闭文件作用域限制和审批，Agent 可读写工作区外文件并读取项目内 `.env`，仅限受控内网部署；源码/收紧环境可设 `DSH_PERMISSION_MODE=workspace-write`。

## 排障

| 症状 | 根因 | 处置 |
|---|---|---|
| 任务秒失败且错误里有“疑似网关 403 配额/路由” | 网关配额耗尽 / 路由失败 | 看网关控制台；检查 `.env` 的 key/base_url 与 `settings.yaml` 的 model/provider |
| `result.json` 解析失败 | 输出合同未遵守 | 保留提示词末尾的 `<<constraint>>`；确认 Agent 写入了 `result.json` |
| 文件任务 `failed` 但 Agent 说做完了 | 产物没写到 `output/` 或扩展名不符 | 检查任务 `output.dir` / `allowed_exts`，提示词里明确写到 `<<output_dir>>/` |
| 初始化 404 / 400 developer role | 网关协议不匹配 | `api_style` 与网关实际协议核对；`supports_developer_role: false` |
| 打包 exe 后报 sandbox runner 起不来 | PyInstaller 临时目录里沙箱执行器无法启动 | 默认已是 `danger-full-access`；确认 `agent.permission_mode` 未被改回 `workspace-write`。长期方案：改 `--onedir` 并 `--collect-all deepseek_harness_runtime` |
| Agent 说没权限写文件 | Windows 沙箱 ACL | `icacls data /grant %USERNAME%:(OI)(CI)F` |
| `POST /api/tasks` 返回 410 | 旧接口已移除 | 改用 `POST /api/tasks/{task_type}` |

## 测试

```bash
python tests/test_api.py     # 无 LLM：校验/轮询/zip/sync 超时/迁移/路径安全

python data/e2e_tasks.py --gen   # 只生成 data/test_docs/sample.pptx
python main.py                   # e2e 需要服务已在 11443 端口运行
python data/e2e_tasks.py         # 真实模型 e2e（需可用网关配额；跑完检查任务结果）
```

> e2e 会真实调用网关，单任务通常 2–5 分钟。默认模型配额不足时会 403，可临时把 `settings.yaml` 的 `model` 改成可用模型，跑完改回。

进阶：任务接入、提示词规则、沙箱 Office skill 用法、e2e 与故障排查，见 **[docs/任务改造指导手册.md](docs/任务改造指导手册.md)**。
