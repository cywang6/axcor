"""breather plugin 的脚本工具包。

`handoff_tools.py` 同时是：
- 可被 workflow_controller 包内 import 的纯函数模块（handoff.py re-export 它）；
- 可被复制到目标工程 `.workflow-controller/` 下独立运行的 CLI（handoff agent 用 Bash 调用）。
因此 handoff_tools.py 只依赖标准库，不 import workflow_controller。
"""
