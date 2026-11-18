# agent-traceback 三阶段示例（设计 → 开发 → 测试）

一个**轻量但真实**的三阶段 workflow，结构与 [ops-registry-invoke](https://gitcode.com/cann/skills.git)
对齐（设计 → 开发 → 测试），但**极简**（4 个 subagent，每个只写一个小文件，省 token、短耗时），
配合 Workflow Controller 的 **web toggle 控制的检查点快照与重放** + **subagent 补充指令注入** +
**检查点文件态预览** + **最终状态跳回**，当面向他人演示完整链路：

> **跑三阶段 → 预览文件态 → 重放到 developer 跑之前 → 注入 developer 指令 → 跳回最终状态**

## 与 ops-registry-invoke 的对应

| ops-registry-invoke | 本示例 | 说明 |
|---|---|---|
| 阶段一 1.3c 并行分段生成（5 个设计 Agent 并发） | 阶段一并发派 `designer_api` + `designer_impl` | 并发设计 subagent（in-flight=2） |
| 阶段二 A1-Main 主线代码开发 | 阶段二 `developer` 读 design 实现 `impl.py` | 开发 subagent |
| 阶段三验收（tester 跑 ST 用例） | 阶段三 `tester` 读 `impl.py` 写 `test.py` 并自测 | 测试 subagent |
| 阶段四上库 / CP 检查点 | （略） | 本示例聚焦前三阶段 + traceback |

每个 subagent 任务都极简（各写一个几字节的文件），但保留了「设计(并发) → 开发 → 测试」
三阶段骨架与并发结构。

## 这示例演示什么

| 演示点 | 怎么验 |
|---|---|
| 1. **并发 subagent** | 阶段一并发的两个设计 subagent 同时在飞（`in-flight=2`）；`demo.py` Phase 1 |
| 2. **检查点保存 + 重放** | 每个 subagent **start 时刻**打一次检查点；`POST /traceback` 从 cp#3 重放（re-call developer）；Phase 1 / Phase 3 |
| 3. **检查点文件态** | `GET /checkpoints/{cp3}/files` 预览那一刻磁盘（两份 design 在、impl.py 不在）；Phase 2 |
| 4. **重放时 subagent 指令注入** | 在重放 session 登记 developer 补充指令 → developer 系统 prompt 含指令（developer 还没跑）；Phase 4 |

**workflow 不感知 traceback**——`workflow-controller.yaml` / `AGENTS.md` 不含任何 checkpoint / 快照 /
git 配置。traceback 由 web Dashboard 的「Traceback」开关控制，subagent 指令由
SessionDetailView 的「Subagent 补充指令」入口登记。agent 能读到的所有文本均不含
"fork" 一词（该词会被 agent 安全策略屏蔽，故用「重放 / replay」替代）。

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
developer 补充指令（如「`add` 改成乘法 `a*b`」）→ 下一轮 developer 启动时该指令前置进其系统
prompt → 拨转重跑方向。

## 跑（确定性链路测试，无需 LLM / opencode）

`demo.py` 在**进程内**起真实 Orchestrator + REST 路由（httpx ASGITransport），不依赖
LLM / opencode 二进制。它跑的是**真实代码路径**（`SubagentTracker.on_start` → `SnapshotEngine.snapshot`
→ checkpoints 表 → `TracebackRunner.run` → seed 预制 session / overlay 注入 / 文件态预览 /
`_snapshot_session_complete`）。

```bash
cd examples/agent-traceback-concurrent
.venv/bin/python demo.py
```

前置：Workflow Controller 已装（`pip install -e .`）+ git 已装。预期：5 行 ✓ 全绿，退出码 0。

```
  Phase 1  三阶段(设计并发/开发/测试) + 四检查点 + parent 链 : ✓
  Phase 2  预览检查点 #3 文件态（两份 design 在 / impl.py 不在） : ✓
  Phase 3  重放到 developer 跑之前（re-call developer） : ✓
  Phase 4  注入 developer 指令 → 系统 prompt（developer 还没跑） : ✓
  Phase 5  最终状态检查点 + 跳回 origin 检查点 : ✓
```

## Live 跑法（端到端含 LLM）

1. **opencode CLI + provider**：bailian-payg + glm-5.2（全局 `~/.config/opencode/opencode.json`）。
2. **workflow-controller web 起好**：`.venv/bin/workflow-controller web`。
3. 安装 agents：`mkdir -p .opencode/agents && cp agents/*.md .opencode/agents/`。
4. **展开 Quick Settings → 开 Traceback**：Dashboard → Quick Settings → 点「Traceback」
   的 Enable。开关变 ON（琥珀色）后，每个 subagent start 都会自动打快照；session 跑完打最终状态检查点。
5. 主面板选本目录 → **Start**（阶段一并发派两个设计 subagent → 阶段二 developer 实现 →
   阶段三 tester 测试 → 主 agent 写 summary.md）。
6. **预览文件态**：SessionDetailView 右栏「检查点」卡片 → 点检查点 #3 的 📁 按钮 → 弹文件浏览器，
   浏览那一刻的磁盘文件（developer 的 cp 处两份 design 在、impl.py 不在）→ 决定从哪重放。
7. **重放到 developer 跑之前**：点 developer 的 checkpoint（或拓扑图上的 ↻ re-call 按钮）
   → 确认重放 → 新重放 session 跳转（拓扑图显示 replay 边；文件态恢复两份 design，impl.py 不在）。
   re-call 语义移除 developer 的 task 调用及其之后全部 → 重放 session 里 developer 还没跑。
8. **注入 subagent 指令**：在重放 session 详情页「Subagent 补充指令」卡 → `subagent_type`
   填 `developer`、文本框贴补充指令（如「本次 `add(a,b)` 应返回乘法 `a*b`」）→ 登记。
   developer 还没跑，下一轮它启动时该指令已前置进其系统 prompt → developer 据此改变行为。
9. **跳回**：若重放结果不满意，重放 session 左栏「源会话」卡片 → 点 origin 跳回原始 session，
   从任一检查点（含最终状态检查点）重新重放。清除指令用卡片「清除」按钮（可逆）。

> toggle OFF 时不会打任何快照——workflow 完全不感知 traceback。只有用户主动开 toggle，
> Workflow Controller 才在每个 subagent start 后 `await` 一次快照（同步阻塞 worker 保证文件态一致），
> 并在 session 跑完打最终状态检查点。

## 文件

- `workflow-controller.yaml`：runtime opencode + starting_prompt（三阶段：设计(并发) → 开发 → 测试）+ reviewer。
  **不含 traceback 配置**——toggle 在 web 控制。**不含 "fork" 一词**（agent 会屏蔽）。
- `AGENTS.md`：opencode primary（`agents:` list 指向 designer_api + designer_impl + developer + tester）。
- `agents/designer_api.md`：写 design_api.md（API 规格）。
- `agents/designer_impl.md`：写 design_impl.md（实现方案）。
- `agents/developer.md`：读 design 实现 impl.py。
- `agents/tester.md`：读 impl.py 写 test.py 并自测。
- `demo.py`：确定性链路测试（进程内 REST + 真实快照 + 假 opencode.db + overlay 注入 + 文件态预览 + 最终状态检查点）。

## 与其他示例的区别

- 本示例：**三阶段（设计(并发) → 开发 → 测试）** workflow，结构与 ops-registry-invoke 对齐但极简，
  验证**并发 + 重放 + 预览 + 注入 + 跳回的组合**——重放到 developer 跑之前，用一条 subagent
  指令拨转重跑方向，并可跳回原始最终状态。每个 subagent 任务都极简，省 token、短耗时、易演示。
