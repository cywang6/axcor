---
description: 从 cannbot session db 重建会话 + 检查点重放示例——导入 db → 重建 → 重放到 developer 跑之前 → 注入指令。
mode: primary
agents:
  - designer
  - developer
  - tester
---

# 从 DB 重建会话 + 检查点重放示例（设计 → 开发 → 测试）

一个**轻量但真实**的三阶段 workflow，结构与
[agent-traceback-concurrent](../agent-traceback-concurrent) 对齐（设计 → 开发 → 测试），
但**极简**（3 个 subagent，每个只写一个小文件，省 token、短耗时）。区别在于：
本示例的 session **此前已跑完并录制在一个 cannbot db 文件里**——Workflow Controller 从该 db 文件
**重建**出一条带检查点的 session（含完整对话态 + 文件态 + 项目 `.git` 历史），
用户可从任一检查点 fork 重放。

> **生成 db → 导入重建 → 预览文件态 → 重放到 developer 跑之前 → 注入 developer 指令 → 跳回**

## 这示例演示什么

| 演示点 | 怎么验 |
|---|---|
| 1. **从 db 重建 session** | `POST /api/sessions/import-db` 把 cannbot db 导入 → 重建出带检查点的 Workflow Controller session；`demo.py` Phase 1 |
| 2. **重建产物同构** | 4 检查点（3 `subagent_start` + 1 `session_complete`）+ parent 链 + 文件态逐字节 + 项目 `.git` 历史 + L2 可消费性；`demo.py` Phase 2 |
| 3. **重放（复用 TracebackRunner）** | 从 cp#2（developer start）重放 → re-call developer 移除其 task part 及之后；`demo.py` Phase 3 |
| 4. **重放时 subagent 指令注入** | 在重放 session 登记 developer 补充指令 → developer 系统 prompt 含指令（developer 还没跑）；`demo.py` Phase 4 |
| 5. **跳回原始检查点** | 从重放 session 读 origin 的检查点列表（jump-back 数据源）；`demo.py` Phase 5 |

### 关键语义：检查点在 subagent **start** 时刻

重建的检查点在每个 subagent **start 时刻**打（捕获该 subagent **开始工作前**的磁盘文件态）：
- cp#1（designer start）：磁盘空。
- cp#2（developer start）：`design.md` 已在、`impl.py` 不在——从这里重放即「重放到 developer
  跑之前」。
- cp#3（tester start）：`design.md` + `impl.py` 在、`test.py` 不在。
- session_complete：三个文件齐全。

从 cp#2 重放（re-call developer）：re-call 语义移除 developer 的 task part **及其之后全部**
→ 重放 session 里 developer 还没跑，磁盘恢复到 `design.md` 已写、`impl.py` 未写。然后注入
developer 补充指令 → 下一轮 developer 启动时该指令前置进其系统 prompt → 拨转重跑方向。

## 任务流程

收到请求时，按 workflow-controller.yaml 的 starting_prompt：

1. **阶段一（设计）**：用 task 工具起 `designer`（subagent_type: designer）写 `design.md`。
2. **阶段二（开发）**：前置条件——`design.md` 已落盘，才能起 `developer` 读 design 实现 `impl.py`。
3. **阶段三（测试）**：起 `tester` 读 `impl.py` 写 `test.py` 并自测。
4. 写 `summary.md` 汇总。

一气走完三阶段再收尾，中途不要停下汇报、不要等指令。

> subagent 定义在 `agents/*.md`（运行前 copy 到 `.opencode/agents/`，见 README）。重建
> 与重放都不改这些 `.md` 文件一字符。
