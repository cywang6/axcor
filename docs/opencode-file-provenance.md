# OpenCode 文件归因插件使用指南

本指南说明如何在直接运行 OpenCode、而不启动 Workflow Controller Web 或控制面的情况下使用文件归因插件。包级 API 说明见 [`packages/opencode-file-provenance/README.md`](../packages/opencode-file-provenance/README.md)。

## 前置条件

- `workflow-controller` Python 包已安装，且 `workflow-controller` 命令位于 `PATH`。插件会按配置自动启动本地 sidecar。
- 目标项目可被 OpenCode 打开，并允许写入项目下的 `.workflow-controller/file-provenance/`。

## 安装插件

本次 PR 提供的是可发布 npm 包，尚未执行 `npm publish`，因此当前不能直接从公共 npm registry 安装。开发验证时，从仓库打包并在运行 OpenCode 的项目中安装本地 tarball：

```bash
cd /path/to/workflow-controller/packages/opencode-file-provenance
npm pack
npm install /path/to/generated-package.tgz
```

包正式发布后，直接在 OpenCode 项目中执行：

```bash
npm install @workflow-controller/file-provenance
```

## OpenCode 配置

在项目的 `opencode.json`（或用户级配置）中加入：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [
    [
      "@workflow-controller/file-provenance",
      {
        "backendCommand": ["workflow-controller"],
        "ignore": [],
        "maxFileBytes": 2097152,
        "requestTimeoutMs": 60000,
        "staleCaptureMs": 600000
      }
    ]
  ]
}
```

从 registry 或本地 tarball 安装时，配置保持不变。`backendCommand` 可替换为无 shell 参数数组，例如 `["uv", "run", "workflow-controller"]`。

## 记录内容与边界

插件固定跟踪 `edit`、`write`、`apply_patch`、`bash`，把前后文件快照、工具输入输出、原生 session、model、agent 以及首个/最近文本 prompt 写入项目的 `.workflow-controller/file-provenance/`。附件不进入 prompt 元数据；该目录包含源码和对话证据，应按项目隐私与保留策略管理。

before hook 会扫描整个 worktree，大型仓库可用 `ignore` 追加生成物或大目录；内置 `.git`、`.workflow-controller`、虚拟环境、依赖与构建目录规则不能移除。后端不可用、超时或崩溃时插件 fail-open，只告警而不阻止 OpenCode 工具，失效进程会在下一次工具调用时惰性重启。

Standalone 模式只保存证据与当前工作树逐行归因：不捕获 OpenCode 之外的编辑，不提供查询命令、UI 或人工认领，不写 Git notes 或 commit 指标。Workflow Controller 托管会话若设置了 `WORKFLOW_CONTROLLER_HOOK_URL` 与 `WORKFLOW_CONTROLLER_SESSION_ID`，插件会自动停用 standalone capture，避免重复归因。
