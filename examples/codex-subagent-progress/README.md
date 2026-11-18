# Codex 子代理实时进度示例

验证 **Codex CLI runtime（模块 D）的 subagent progress**：父 agent 派发两个只读子代理，
Dashboard 实时显示每个子代理的 thinking / tool use / tool result，最终汇总，start/stop 各一次。

## 前置

- **Codex CLI**：目标版本支持 collab 子代理（约定不低于 `0.166.0`），`codex --version` 可用。
- **Codex provider**：Web「设置 → 模型供应商」已添加 `Codex CLI` provider（base URL +
  API key + model，三者必填；Codex 不回退本机登录态）。
- **HookServer 在役**（Web 默认启动）：模块 B 的 `SubagentStart` / `SubagentStop`
  是进度激活与释放的对账来源。
- **workflow-controller web**：`workflow-controller web --dev`。

## 跑

1. `cd examples/codex-subagent-progress`。
2. Web 主面板选本目录，新建会话时「代理框架」选 `Codex CLI`，Worker 模型选已验证的 provider。
3. **Start**。
4. Dashboard 观察：Agent 拓扑出现两个子节点；每个子节点实时流出 thinking /
   tool use / tool result；父 agent 最终输出 `A=<数字>; B=<首行>`。

## 通过标准

- 每个子代理 `SUBAGENT_START` 恰好一次，进度增量出现，`SUBAGENT_STOP` 恰好一次。
- 子代理进度不晚于 start 出现（不会先进度后 start 被拓扑丢弃）。
- 两个子代理进度互不串线；父会话事件流不含重复的子代理生命周期。

## 已知边界

- 子代理是否派发由模型决定；若父 agent 未 spawn，重跑或加强 prompt 中的
  "spawn 两个子代理" 措辞。
- D 只跟踪本次 invocation 父流声明的 spawn；跨 resume invocation 存活的子代理不续读。
- 多个会话共享本目录 `.codex/` 时进度按 child id + `session_meta` 双校验隔离。
