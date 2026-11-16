---
name: aero-report-to-risk
description: "TODO：一句话说明用途与触发时机，例如：把航空航天研究报告转换为结构化风险清单，提取风险项、等级与依据。"
---

# aero-report-to-risk

> 把你原来的 `skill.md` 正文粘贴到这里，替换本段占位说明。
> 文件名必须是 `SKILL.md`（大写），frontmatter 中的 `name` 必须与目录名一致且为 kebab-case。

## 适用场景

TODO：说明什么任务、什么样的输入应该使用本 skill。

## 输入与输出

- 输入：TODO（任务 workspace 中的文件）。
- 输出：写入任务 workspace 的 `result.json`（JSON 任务）或 `output/`（文件任务），不要写回 skill 目录。

## 资源使用

- `references/`：TODO 列出两个 md 的用途，并写明在正文中“读取 references/xxx.md”。
- `scripts/`：TODO 列出各脚本的用途与运行方式；运行时会在内容前注入本 skill 的绝对路径（Base directory），脚本用其下的相对路径调用。

## 执行步骤

1. TODO：读取 `references/` 中的规范或示例。
2. TODO：按流程执行 `scripts/` 中的脚本或直接分析。
3. TODO：产出并校验最终结果文件。

## 约束

- 结果必须写入任务 workspace，不要污染本 skill 目录。
- `references/` 与 `scripts/` 不会自动加载，正文必须明确指定读哪个文件、跑哪个脚本。
