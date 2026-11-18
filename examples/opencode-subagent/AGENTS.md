---
description: 演示 opencode subagent + breather handoff——primary 起 reader subagent 读文章写摘要，确认产物后完成。
mode: primary
agents:
  - reader
---

# opencode subagent + handoff 示例

本示例演示 opencode 的 **subagent 语义 + breather subagent 返回点 handoff**：primary agent 起一个 `reader` subagent 读取 articles/ 下的文章并写摘要，确认产物后完成。

## 任务流程

收到请求时，按 workflow-controller.yaml 的 starting_prompt：

0. 清理旧产物，确认 articles/ 目录下有文件，写 `articles/files_exist.md`。
1. 用 `task` 工具起 `reader` subagent，读取 `articles/` 下所有文件，写摘要到 `articles/summary.md`。
2. 确认 `articles/summary.md` 已生成，写 `articles/done.md`。

> subagent 定义保存在 `agents/reader.md`；`.opencode/agents/reader.md` 是指向它的相对软连接。
