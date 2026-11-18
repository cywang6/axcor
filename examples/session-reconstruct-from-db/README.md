# 从 DB 重建会话 + 检查点重放示例（设计 → 开发 → 测试）

一个**轻量但真实**的 session 重建 + 检查点重放端到端示例。一条此前已跑完的三阶段
workflow（设计 → 开发 → 测试）被录制在一个 **cannbot session db 文件**里——Workflow Controller
从该 db 文件**重建**出一条带检查点的 Workflow Controller session（含完整对话态 + 文件态 + 项目
`.git` 历史），用户可从任一检查点 fork 重放。

> **生成 db → 导入重建 → 预览文件态 → 重放到 developer 跑之前 → 注入 developer 指令 → 跳回**

## 与 agent-traceback-concurrent 的区别

| 维度 | agent-traceback-concurrent | 本示例 |
|---|---|---|
| 数据来源 | session **运行中**实时打快照 | session **已跑完**后从 cannbot db **离线重建** |
| 文件态 | 运行时 git 镜像 | 重放 write/edit/bash/git **前向重建** |
| 对话态 | 直接读 origin 的 opencode.db | 从 cannbot db 的 Turn+ToolCall **转写**成 opencode.db |
| 检查点 | 钩子实时写 checkpoints 表 | 从 task 边界**推导**后批量写 |
| 重放 | TracebackRunner（实时快照） | **完全复用** TracebackRunner（重建产物同构） |

## 这示例演示什么

| 演示点 | 怎么验 |
|---|---|
| 1. **从 db 重建 session** | `POST /api/sessions/import-db` 把 cannbot db 导入 → 重建出带检查点的 Workflow Controller session；`demo.py` Phase 1 |
| 2. **重建产物同构** | 4 检查点（3 `subagent_start` + 1 `session_complete`）+ parent 链 + 文件态逐字节 + 项目 `.git` 历史 + L2 可消费性 + 合成历史事件 + birth_traceback 溯源；`demo.py` Phase 2 |
| 3. **重放（复用 TracebackRunner）** | 从 cp#2（developer start）重放 → re-call developer 移除其 task part 及之后；`demo.py` Phase 3 |
| 4. **重放时 subagent 指令注入** | 登记 developer 补充指令 → developer 系统 prompt 含指令（developer 还没跑）；`demo.py` Phase 4 |
| 5. **跳回原始检查点** | 从重放 session 读 origin 的检查点列表（jump-back 数据源）；`demo.py` Phase 5 |

### 关键语义：检查点在 subagent **start** 时刻

重建的检查点在每个 subagent **start 时刻**打（捕获该 subagent **开始工作前**的磁盘文件态）：
- cp#1（designer start）：磁盘空。
- cp#2（developer start）：`design.md` 已在、`impl.py` 不在——从这里重放即「重放到 developer
  跑之前」。
- cp#3（tester start）：`design.md` + `impl.py` 在、`test.py` 不在。
- session_complete：三个文件齐全。

## 跑（确定性链路测试，无需 LLM / opencode）

`demo.py` 在**进程内**起真实 Orchestrator + REST 路由（httpx ASGITransport），不依赖
LLM / opencode 二进制。它跑的是**真实代码路径**（db_reader → l2_transcriber →
file_state_rebuilder → checkpoint_deriver → orchestrator → TracebackRunner.restore/seed）。

```bash
cd examples/session-reconstruct-from-db
.venv/bin/python demo.py
```

前置：Workflow Controller 已装（`pip install -e .`）+ git 已装。预期：5 行 ✓ 全绿，退出码 0。

```
  Phase 1  从 cannbot db 导入重建 session : ✓
  Phase 2  重建产物同构 (检查点/文件态/git/L2/事件/溯源) : ✓
  Phase 3  重放到 developer 跑之前 : ✓
  Phase 4  注入 developer 指令 → 系统 prompt : ✓
  Phase 5  跳回 origin 检查点列表 : ✓
```

## 跑（真实 AI agent，端到端含 LLM）

```bash
cd examples/session-reconstruct-from-db
.venv/bin/python demo.py --live
```

`--live` 模式不 mock `_activate_session`——fork session 被真实 opencode driver 激活，
worker 用真实 LLM（bailian-payg/glm-5.2）续跑。developer subagent 实际执行、写 impl.py，
验证重建产物可被真实 opencode 消费。

前置：确定性模式的前置 + opencode CLI + provider 配置（见 `docs/designs/opencode-support.md`）。

`--live` 模式的 Phase 3 会等待真实 agent 写 impl.py（timeout=180s）。hint 注入在 replay
**前**登记在 origin session（fork 继承），让 developer 启动时补充指令已前置进其系统 prompt。

## Live 跑法（workflow-controller web Dashboard 端到端）

> **项目目录** = `examples/session-reconstruct-from-db/`（含 `workflow-controller.yaml`、`AGENTS.md` 等）。
> Workflow Controller 以此为工作目录（cwd），从 `<cwd>/.opencode/agents/` 加载 subagent 定义。
> 所有前置步骤都在此目录下执行。

前置准备（只需做一次）：

1. 生成 cannbot db：`python gen_db.py`（生成 `cannbot_session.db`）。
2. 安装 agents：`mkdir -p .opencode/agents && cp agents/*.md .opencode/agents/`。
3. 准备一个空目录作为重建目标（如 `mkdir -p /tmp/workflow-controller-replay`）。注意：这是**另一个**
   目录，不要跟项目目录搞混——Import Session 会把重建产物（文件态、快照仓、opencode.db）
   写到这里。
4. 启动 workflow-controller web：`.venv/bin/workflow-controller web`。

导入与重放：

5. Dashboard → Import Session → 选 `cannbot_session.db`，`target_cwd` 填第 3 步的空目录 → 导入。
6. 重建后 session 出现在 Dashboard，带 4 个检查点（3 subagent_start + 1 session_complete）。
7. 预览文件态：SessionDetailView → 检查点卡片 → 点 📁 浏览文件态。
8. 重放到 developer 跑之前：点 cp#2（developer start）的 ↻ → 确认重放。
9. 注入 developer 指令：重放 session → Subagent 补充指令 → 填 developer + 指令（如「本次 `add(a,b)` 应返回乘法 `a*b`」）→ 登记。
10. 跳回：重放 session → 源会话卡片 → 点 origin 跳回。

## 文件

- `gen_db.py`：生成合成 cannbot session db（也作为 demo.py 的 db 工厂模块）。
- `demo.py`：确定性链路测试（+ `--live` 真实 agent 模式）。
- `workflow-controller.yaml`：runtime opencode + starting_prompt（三阶段：设计 → 开发 → 测试）。
- `AGENTS.md`：opencode primary（`agents:` list 指向 designer + developer + tester）。
- `agents/designer.md`：写 design.md（API 规格）。
- `agents/developer.md`：读 design 实现 impl.py。
- `agents/tester.md`：读 impl.py 写 test.py 并自测。
- `clean.sh`：删除生成的文件。
