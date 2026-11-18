---
description: 三阶段（设计/开发/测试）workflow，阶段一并发派两个设计 subagent，演示 traceback 检查点保存与重放 + subagent 指令注入。
mode: primary
agents:
  - designer_api
  - designer_impl
  - developer
  - tester
---

# 三阶段检查点重放示例（设计 → 开发 → 测试）

一个**轻量但真实**的三阶段 workflow，结构与 [ops-registry-invoke](https://gitcode.com/cann/skills.git)
对齐（设计 → 开发 → 测试），但**极简**（4 个 subagent，每个只写一个小文件，省 token、短耗时）：

- **阶段一（设计，并发）**：primary 并发派 `designer_api`（写 `design_api.md`）与
  `designer_impl`（写 `design_impl.md`），两个 subagent 同时在飞——对齐 ops-registry-invoke
  阶段一 1.3c 的并行分段生成。
- **阶段二（开发）**：primary 派 `developer` 读设计文档实现 `impl.py`——对齐阶段二 A1-Main。
- **阶段三（测试）**：primary 派 `tester` 读 `impl.py` 写 `test.py` 并自测——对齐阶段三验收。

配合 Workflow Controller 的**检查点快照与重放** + **subagent 补充指令注入** + **检查点文件态预览** +
**最终状态跳回**，演示完整链路：

> **跑三阶段 → 预览文件态 → 重放到 developer 跑之前 → 注入 developer 指令 → 跳回最终状态**

## 这示例演示什么

| 演示点 | 怎么验 |
|---|---|
| 1. **并发 subagent** | 阶段一并发的两个设计 subagent 同时在飞（`in-flight=2`）；`demo.py` Phase 1 |
| 2. **检查点保存 + 重放** | 每个 subagent **start 时刻**打一次检查点；`POST /traceback` 从 cp#3 重放（re-call developer）；Phase 1 / Phase 3 |
| 3. **检查点文件态** | `GET /checkpoints/{cp3}/files` 预览那一刻磁盘（两份 design 在、impl.py 不在）；Phase 2 |
| 4. **重放时 subagent 指令注入** | 在重放 session 登记 developer 补充指令 → developer 系统 prompt 含指令（developer 还没跑）；Phase 4 |

**workflow 不感知 traceback**——`workflow-controller.yaml` / `AGENTS.md` 不含任何 checkpoint / 快照 / git
配置。traceback 由 web Dashboard 的「Traceback」开关控制，subagent 指令由 SessionDetailView
的「Subagent 补充指令」入口登记。agent 能读到的所有文本均不含 "fork" 一词（该词会被 agent
安全策略屏蔽，故用「重放 / replay」替代）。

### 关键语义：检查点在 subagent **start** 时刻

检查点在每个 subagent **start 时刻**打（捕获该 subagent **开始工作前**的磁盘文件态）：
- cp#1（designer_api start）：磁盘空。
- cp#2（designer_impl start）：`design_api.md` 已在、`design_impl.md` 不在；此时两个设计
  subagent **同时在飞**（并发）。
- cp#3（developer start）：两份 design 在、`impl.py` 不在——从这里重放即「重放到 developer
  跑之前」。
- cp#4（tester start）：design + impl.py 在、`test.py` 不在。

从 cp#3 重放（re-call developer）：re-call 语义移除 developer 的 task part **及其之后全部**
→ 重放 session 里 developer 还没跑，磁盘恢复到两份 design 已写、`impl.py` 未写。然后注入
developer 补充指令 → 下一轮 developer 启动时该指令前置进其系统 prompt → 拨转重跑方向
（如把 `add` 改成乘法）。

## 任务流程

收到请求时，按 workflow-controller.yaml 的 starting_prompt：

0. **阶段零（清理）**：开工第一件事——用 bash 工具运行 `./clean.sh`，清除上一次运行遗留的
   产物（design_api.md / design_impl.md / impl.py / test.py / summary.md），确保本次从干净状态
   开始。clean.sh 跑完再进入阶段一。
1. **阶段一（并发设计）**：在**同一条 assistant 消息**里用 `task` 工具发出两条调用，并发起
   `designer_api` 与 `designer_impl`，并等待两条都返回——阶段一完成 = 两个设计 subagent 都已返回。
2. **阶段二（开发）**：**前置条件**——`design_api.md` 与 `design_impl.md` 都已落盘、且两个设计
   subagent 都已返回，才能起 `developer` 读 design 实现 `impl.py`。两者未齐前不要提前起 developer。
3. **阶段三（测试）**：起 `tester` 读 `impl.py` 写 `test.py` 并自测。
4. 写 `summary.md` 汇总。

一气走完三阶段再收尾，中途不要停下汇报、不要等指令。**反循环**：每个文件只写一次；若某 design
文件已存在且正确，不要再派对应 designer，直接进入下一阶段，不要回退重派已完成的 subagent。

> subagent 定义在 `agents/*.md`（运行前 copy 到 `.opencode/agents/`，见 README）。traceback
> toggle 与 subagent hint 都不改这些 `.md` 文件一字符——前者控制快照，后者经
> `OPENCODE_CONFIG_CONTENT` overlay 进 subagent 系统 prompt，纯运行时、会话级、可逆。
