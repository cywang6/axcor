# File Provenance

这个示例用于在 Workflow Controller Web 中通过真实 LLM session 验证 File Provenance。示例只提供配置、待修改的项目和验收步骤，不会自行启动 Workflow Controller、调用 Web API 或模拟文件工具事件。

## 前置条件

- 已安装 Workflow Controller，并配置好 `opencode` runtime 使用的 Provider/API key。
- `file_provenance` 插件已在 **Settings → 插件** 中启用。
- `sample-project` 已初始化成独立 Git 仓库。

根目录的 [`workflow-controller-config.yaml`](workflow-controller-config.yaml) 是 Web 配置，负责把当前项目指向 `sample-project`；[`sample-project/workflow-controller.yaml`](sample-project/workflow-controller.yaml) 是项目配置，声明真实 LLM session 的 runtime 和两阶段任务。初始 Prompt 先修改 `greeting()`，第二阶段通过不同的当前 Prompt 新增 `provenance_marker()`，两个阶段都会对 `provenance_demo.py` 产生真实 Capture。

File Provenance 会从工具调用目录向上查找最近的 Git 仓库。必须使用下面的独立仓库，避免把示例产生的 commit note 和证据关联到 Workflow Controller 源码仓库。

```bash
cd examples/file-provenance/sample-project
git init
git config user.name "File Provenance Demo"
git config user.email "file-provenance@example.com"
git add workflow-controller.yaml provenance_demo.py
git commit -m "file provenance demo baseline"
cd ..
```

如果目录已经是 Git 仓库，不要重复初始化；确认工作区干净即可：

```bash
git -C sample-project status --short
```

## 从 Web 运行

在 `examples/file-provenance` 目录自行启动 Workflow Controller：

```bash
workflow-controller web
```

然后在 Web 中完成以下操作：

1. 打开 **Settings → 插件**，确认 `file_provenance` 已启用。
2. 确认项目目录为本示例的 `sample-project`；Dashboard 会读取 [`sample-project/workflow-controller.yaml`](sample-project/workflow-controller.yaml)。
3. 在 Dashboard 启动 session。真实 LLM 第一阶段按照初始 Prompt 修改 `greeting()`，第二阶段收到不同的当前 Prompt 后新增 `provenance_marker()`；两个阶段都必须使用文件工具，但不会提交 Git commit。示例使用 `passthrough` evaluator 并在两个阶段后到达 `max_iterations`，这是预期结束状态。两阶段演示期间不要暂停或恢复 session，否则恢复提示会跳过第二阶段指令；如需中途干预，请使用「发送消息」，该路径仍会渲染阶段 Prompt。
4. 在 Session Detail 中观察执行过程；插件加载后会显示 **文件归因**页签。
5. session 自然完成后保持 Workflow Controller Web 运行，在另一个终端检查并提交改动：

   ```bash
   git -C sample-project diff -- provenance_demo.py
   git -C sample-project add provenance_demo.py
   git -C sample-project commit -m "apply provenance demo change"
   ```

6. 等待数秒让 finalizer 处理新 commit，然后回到对应 Session Detail 的 **文件归因**页签，选择 `provenance_demo.py` 和刚创建的 commit。

`runtime` 或 `model` 需要调整时，直接修改 `sample-project/workflow-controller.yaml`，或使用 Web 中对应的配置项。

## 预期结果

- `provenance_demo.py` 中 LLM 新增或修改的行显示为 Agent 归因，并带有本次 session、runtime 和 tool 信息。
- `greeting()` 修改行的 Capture 由初始 Prompt 产生；`provenance_marker()` 新增行的 Capture 由第二阶段当前 Prompt 产生。分别点击两组行的「Capture 详情」，都能看到 `provenance_demo.py` 的真实修改快照和对应修改指令；第二个 Capture 的「当前 Prompt」与「初始 Prompt」内容不同。
- commit 指标显示 AI 生成行；Git commit 带有 `authorship/3.0.0` note。
- 证据写入 `sample-project/.workflow-controller/file-provenance/`，而不是 Workflow Controller 源码仓库。

也可以从命令行检查底层结果：

```bash
git -C sample-project notes --ref=refs/notes/ai show HEAD

sqlite3 sample-project/.workflow-controller/file-provenance/provenance.db \
  'SELECT capture_id, session_id, runtime, model, tool_name, conflicted FROM captures;'

sqlite3 sample-project/.workflow-controller/file-provenance/provenance.db \
  "SELECT c.capture_id, json_extract(c.attributes, '$.prompt') != json_extract(c.attributes, '$.initial_prompt') AS prompt_changed FROM captures AS c WHERE c.session_id = (SELECT session_id FROM captures ORDER BY started_at DESC LIMIT 1) AND EXISTS (SELECT 1 FROM mutations AS m WHERE m.capture_id = c.capture_id AND m.path = 'provenance_demo.py') ORDER BY c.started_at;"
```

本示例的两个新 Capture 都应看到 `conflicted = 0`；按时间排序时，第一阶段的 `prompt_changed = 0`，第二阶段的 `prompt_changed = 1`。重复演示前先用 `git -C sample-project restore provenance_demo.py` 恢复目标文件，否则 LLM 发现结果已经存在时可能不会再次调用修改工具。若同一工作区同时存在多个文件修改 capture、capture 跨越 commit，或进程异常退出，证据会标为 conflict，避免把无法唯一确认的改动错误归因。
