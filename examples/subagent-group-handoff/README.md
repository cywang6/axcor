# Claude CLI：主代理与子代理同时越线

本例验证**第一次整组交接**：主代理并行派发 `researcher`、`checker`、`writer`，`checker` 读取长输入时超过子代理阈值，主代理也超过自己的阈值。一个独立交接会话应写出主代理摘要和三份子代理摘要；接续时创建**新主会话**，重新派发三名子代理。第一次交接完成后即可停止，无需等待最终任务结束。

## 确定性验收

从仓库根目录运行：

```bash
python3 examples/subagent-group-handoff/demo.py
```

`demo.py` 使用真实 Orchestrator、Breather 和 `claude_cli` 驱动，并把 `claude -p` 替换为受控程序；不需要模型密钥，也不调用真实模型。它读取本项目 `workflow-controller.yaml` 的窗口、双阈值和同一份 [交接指令](handoff-instructions.md)，模拟主代理 3%、`checker` 1.6%、另两名子代理低于 1%，并断言交接代理同时收到主代理和子代理两段指令、`main_handoff` 非空、主代理源快照和四份交接文档齐全、完成信号写入、新主会话建立且重新派发三名子代理。成功打印四行 `PASS`，默认删除临时产物。加 `--keep` 可保留并打印 `ARTIFACTS` 路径。

## Web 中用真实 Claude CLI 试跑

本项目的 `workflow-controller.yaml` 已配置 `context_window: 1000000`、主代理阈值 `context_threshold_pct: 1`、子代理阈值 `subagent_context_threshold_pct: 1`。**模型实际窗口必须支持 100 万 tokens**；1% 对应约 1 万 tokens。主代理的系统提示在先前真实会话中约占 2%～3%，因此在子代理越线时它也应越线。`researcher` 和 `writer` 等待 `checklist.md`，使 `checker` 读取长输入时三人仍在途。真实模型行为仍可能变化，页面应以实际 `main_context`、`subagent_context` 和清单为准。

1. 确认 `claude` CLI 可用，在 Settings → Plugins 启用 Breather。本项目 YAML 已分别配置阈值，无需修改插件全局窗口或阈值。
2. 每次新建会话前，从仓库根目录运行 `python3 examples/subagent-group-handoff/reset.py`，清理上次的输入和产物。交接与接续过程中不要运行。
3. 在 Web 选择本目录，点击 **Start**。主代理应在同一轮派发三个 Agent。`checker` 运行 `prepare.py`、逐块读取 `inputs/` 并写进度；另外两名子代理等待 `checklist.md`。
4. 首次交接完成后，运行 `python3 examples/subagent-group-handoff/verify.py`。它检查主代理与 `checker` 均超过 1%、三名旧子代理在清单中、主代理摘要和三份子代理摘要及完成信号齐全。随后可暂停**新主会话**，无需等待最终产物。若想检查重新派发，等新主会话启动三名新子代理后运行 `verify.py --redispatched`。

真实模型可能提前结束子代理或改变执行顺序，因此 Web 试跑不保证每次成功；确定性验收命令负责稳定覆盖双越线分支。本例每次真实运行至少消耗使 `checker` 超过 1% 所需的输入量。只需验证「仅子代理越线、原主会话恢复」时，使用 [subagent-only-handoff](../subagent-only-handoff/README.md)。
