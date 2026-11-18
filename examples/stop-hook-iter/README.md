# stop-hook-iter example

演示 **stop hook + reviewer decide_stop + 多 iters**（不用 AUQ）。

## 与 multi-iteration 的区别

| | multi-iteration | stop-hook-iter（本）|
|---|---|---|
| runtime | claude_cli | **claude_sdk**（见下方 cli 说明）|
| 阶段门禁 | **AUQ**（worker 主动调 AskUserQuestion 问 reviewer）| **stop hook**（worker 想停 → reviewer decide_stop 判）|
| reviewer 判定 | decide_auq | **decide_stop** |

worker 跑到自然 stop → reviewer decide_stop 判 `block_with_feedback`（没完成）/ `let_exit`
（完成）→ loop 多 iters（resume 续上下文）。

## 运行

```
.venv/bin/workflow-controller web
```

web UI Browse 选本目录（`stop-hook-iter`）→ Start Session。

## 观察

`{project_dir}/.workflow-controller/workflow-controller.log`（注意：日志写 project_dir 的 .workflow-controller，不在 web stdout）：
- reviewer `decide_stop` 判 `let_exit` / `block_with_feedback`（带 rationale）
- 多 iters（iteration 1 → 2 → 3，resume 续同一 claude session）

## cli 验证情况（为什么默认 sdk）

本 example 默认 `runtime: claude_sdk`。**claude_cli 跑不出 stop hook + reviewer**，原因是
claude CLI 的 `claude -p`（非交互/print 模式）**不触发 Stop hook**——hooks.json 配了 Stop 也没用：

- **claude_sdk**（默认）：✅ stop hook → reviewer decide_stop 工作（`_decide_stop` 调 + reviewer
  真判 let_exit/block + 多 iters）。sdk 的 `_stop_cb` in-process（`claude_agent_sdk/_bundled/claude`），
  worker stop 时正确触发。
- **claude_cli**：❌ 不工作。`claude -p` worker stop 不发 `/hooks/stop` → `_handle_stop_request`
  不调 → `_decide_stop` 不调 → worker stop 直接进下一轮（passthrough 推）。**这是 claude CLI 的限制**，
  不是 Workflow Controller bug。cli 的 reviewer 介入只能走 AUQ（PreToolUse，worker 主动调工具），这正是
  multi-iteration 用 AUQ 的原因。

想验证 cli 不工作：把 workflow-controller.yaml 的 `runtime` 改成 `claude_cli` 跑一遍，观察 log 无
`decide_stop`、reviewer 不判、worker 直接 iter。

## 目的

A3（breather 通用化：reviewer REVISE → 自动 check 上下文）依赖 `decide_stop` 工作，所以
先验证这条路。结论：**sdk 可行，cli 不可行**（cli 靠外部 /breather 或 AUQ）。
