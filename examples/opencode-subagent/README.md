# opencode + subagent + handoff 示例

演示 Workflow Controller 的 **opencode runtime + subagent 语义 + breather subagent 返回点 handoff**。

primary agent 起一个 reader subagent 读取 articles/ 下的体育新闻文章并写摘要，确认产物后完成。

## 这示例测什么

1. **opencode runtime 整体**——起 session、worker、subagent 跑通。
2. **subagent 返回点 handoff**——subagent 返回是主 worker 的干净 turn 边界，breather 在那 check context，超阈值就切片。
3. **per-project handoff/continuation prompt 模板**——`workflow-controller.yaml` 里配 `handoff_prompt_path` 和 `continuation_prompt_path` 指向本项目自己的模板文件，覆盖 breather 内置默认模板。

## 前置

- **opencode CLI + provider**（配方见 `docs/designs/opencode-support.md`）。
- **workflow-controller web**：`.venv/bin/workflow-controller web`。

## 跑

1. 本项目只维护 `agents/reader.md`，`.opencode/agents/reader.md` 是指向它的相对软连接。OpenCode 可直接发现 reader，Workflow Controller 启动时无需再复制。
2. **Settings → Plugins** 启用 `breather`。
3. 调低阈值（demo 对话量小，默认 1M × 70% = 700K 永远到不了）——编辑 `src/workflow_controller/plugins/breather/config.yaml`，设 `context_threshold_pct: 1`。
4. 主面板选本目录 → **Start**。

## 看什么

- **Dashboard**：worker 跑第 0 步（清理+确认文件）→ 起 reader subagent 读文章写 summary.md → subagent 返回时 breather 在返回点 check context → 超 1% → handoff。
- **handoff 接力**：handoff agent 用本项目极简模板写交接文档（判断产物阶段）→ continuation session 读交接文档按「下一步」接续。
- **完成信号**：模板把交接文档名末尾的 `-doc` 换成 `-done`，与 Breather 检查的信号文件名一致。
- **Workflow Controller log**（`.workflow-controller/workflow-controller.log`）：
  - `subagent start/stop` — driver 把 task 翻译成 SUBAGENT event。
  - `on_context_event: ... over=True` → `finalize_handoff: START`。

## 文件

| 文件 | 作用 |
|------|------|
| `workflow-controller.yaml` | runtime opencode + starting_prompt + `handoff_prompt_path` + `continuation_prompt_path` |
| `AGENTS.md` | opencode primary（agents list 指向 reader） |
| `agents/reader.md` | 唯一的 reader subagent 源文件 |
| `.opencode/agents/reader.md` | 指向唯一源文件的软连接，供 OpenCode 发现 |
| `articles/article.md` | 示例体育新闻文章 |
| `handoff-prompt.md` | 极简 handoff agent prompt 模板（检查产物判断阶段，两个 Write 就结束） |
| `continuation-prompt.md` | 接续 worker prompt 模板（读交接文档 → 按「下一步」执行） |

> `handoff_prompt_path` 和 `continuation_prompt_path` 是 per-project 配置（写在项目 `workflow-controller.yaml` 里），不配则用 breather 内置默认模板。详见 `docs/designs/breather-plugin.md` §8。

本示例仅支持 OpenCode；`runtime: opencode`、`task` 工具指令和 `AGENTS.md` 均按 OpenCode 工作流编写。
