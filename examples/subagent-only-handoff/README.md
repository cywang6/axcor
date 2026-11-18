# Claude CLI：三名子代理并行，只有一名越线

这个 Web 示例使用真实 `claude -p` 和真实的 100 万上下文窗口，**主要验证第一次子代理整组交接**。主代理并行派发 `reader`、`auditor`、`reporter`；`reader` 自行生成并读取长输入，另两名子代理等待结果，尽量让三名子代理在 `reader` 越线时都还在途。预期第一次交接只总结三名子代理，随后恢复原主 session。真实模型的派发、读入量和后续行为不可控，**不保证整个任务最终跑通**。

## 运行

1. 确认 Claude CLI 可用，并在 Settings → Plugins 中启用一次 Breather。本目录的 [workflow-controller.yaml](workflow-controller.yaml) 已为本项目配置：

   ```yaml
   context_window: 1000000
   context_threshold_pct: 10
   subagent_context_threshold_pct: 5
   ```

   `1000000` 必须是当前模型实际支持的窗口。`10` 是**主代理**阈值，`5` 是**子代理**阈值；两者分别显示在会话页的主代理上下文卡片和子代理区。主代理启动时的系统提示在实测会话中已占约 2.6%，因此本例要求子代理触发时主代理低于 10%。如果 Breather 已启用且模型确实支持 1M，直接选择本目录新建会话即可，无需修改全局阈值或重启 Web。

2. **每次新建会话前**，从仓库根目录运行 `python3 examples/subagent-only-handoff/reset.py`，清除上次试跑的输入、进度、结果和交接文件。仅在新建会话前运行；交接和续跑过程中不要运行。

3. 在 Web 选择本目录，点击 **Start**。不必手工运行 `prepare.py`：`reader` 会自己生成 `inputs/`。主代理必须在同一轮并行调用三次 Agent 工具；页面的子代理卡片应分别显示三名子代理。`reader` 逐块读十份输入并写进度；`auditor` 和 `reporter` 等待 `result.md`，等待脚本不反复调用模型。

4. `reader` 超过 5% 后，Workflow Controller 应暂停主 session 和三名在途子代理，用独立 handoff session 写三份交接文档；清单的 `main_handoff` 应为空。页面的“最近一次交接”显示**成功**、三份文档和完成信号写入后，第一次交接的验收目标就已达到。可立即在**原主会话**点击“暂停”，无需等新子代理完成，也无需等最终产物。不要点击 handoff session 的“取消交接”。

   从仓库根目录运行默认验收命令：

   ```bash
   python3 examples/subagent-only-handoff/verify.py
   ```

   默认只检查**第一次交接**：三份子代理文档和完成信号、`reader` 越过 5%、主代理低于 10%、没有主代理交接。不要求重新派发或最终产物。脚本检查本次运行的第一个交接批次，避免第二次交接掩盖第一次的结果。

5. 若还想检查“从断点重新派发”，等**原主 session**出现新 `reader`、`auditor`、`reporter` 后即可暂停，再运行：

   ```bash
   python3 examples/subagent-only-handoff/verify.py --redispatched
   ```

   只有决定让任务完整运行时，才在结束后运行 `python3 examples/subagent-only-handoff/verify.py --full`；它额外检查 `result.md`、`audit.md`、`report.md` 和异常标记。完整运行可能再次越线并发生第二次交接，且不属于本示例的默认验收目标。

## Token 成本与时长

在真实 100 万窗口下，5% 仍是**约 5 万输入 token**；70% 是约 70 万。5 万是该阈值不可避免的下界，运行时间取决于模型和工具速度，不能保证固定分钟数。只想低成本检查交接链路，可把项目 YAML 的子代理阈值临时改成 `1`（约 1 万输入 token），验证器会按当前 YAML 的阈值检查；主代理阈值仍是 10%。这属于冒烟测试，默认的 5% 用于确认较高上下文下的真实运行。不要把 `context_window` 虚报成较小数值来省 token。

真实模型可能没有并行派发、没有完整读取或提前结束，提示词不能保证精确触发第一次交接。若现场未生成交接清单，先检查三个 Agent 调用、`inputs/` 是否生成和页面显示的子代理上下文。需要确定性复现三子代理整组交接与续跑，可运行 [无密钥模拟验收](../subagent-group-handoff/README.md#稳定复现一条命令)。

`reset.py` 保留 `.workflow-controller/events.db` 历史事件库；只清理本示例的运行产物。它不会在 Web Start 时自动执行。
