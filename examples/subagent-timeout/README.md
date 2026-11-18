# watchdog 三条规则示例

演示 Workflow Controller **watchdog** 的三条规则：

1. **subagent 超时** — subagent 运行超过阈值 → 告警。
2. **文件不变** — 连续 N 个 subagent 结束后文件无变化 → 告警。
3. **关键词检测** — 文件中出现指定关键词 → 告警。

Watchdog 配置写在 `workflow-controller.yaml` 的 `watchdog:` 段，**选目录即自动生效**，无需手动在 Settings 里配置。

## 场景

1. 清理旧 `logs/` 目录。
2. 写入 `logs/log.md`（"初始化测试"）。
3. 起 sleeper 每 2 秒打印 × 5 次（约 10 秒）→ 超时告警。
4. 起两个快 subagent → 文件不变告警（sleeper + 2 个 = 连续 3 次 stop，logs/log.md 没变）。
5. 起 subagent 写"membase"到 `logs/log.md` → 关键词告警。
6. 报告"测试完成"。
7. 点「上传现场到 Context Bay」按钮 → 把 `logs/` 目录 + events.db 打包到以 session 名命名的文件夹，git push 到 Context Bay 仓。

## 前置

- **opencode CLI + provider**（配方见 `docs/designs/opencode-support.md`）。
- **workflow-controller web**：`workflow-controller web --dev`。
- **git 凭据**：本机已配好对 Context Bay 仓的写权限（HTTPS token 存 `~/.git-credentials`，或 SSH key 加到 gitcode）。

## 跑

1. 进入示例目录：`cd examples/subagent-timeout`。
2. 安装 agents：`mkdir -p .opencode/agents && cp agents/*.md .opencode/agents/`。
3. 启动 workflow-controller web：`workflow-controller web --dev`。
4. 打开浏览器进入 workflow-controller web（默认 `http://localhost:18601`）。
5. 主面板选本目录 → 选中的瞬间 watchdog 配置自动加载。
6. **Start**。
7. 观察告警徽标依次变红 → 三条告警依次出现在弹窗中。
8. 点「上传现场到 Context Bay」→ 进度条走完，`logs/` 和 events.db 已 push 到 Context Bay 仓的 `<session-name>/` 文件夹下。

## 配置（workflow-controller.yaml 中的 watchdog 段）

```yaml
watchdog:
  subagent_timeout_seconds: 10   # 规则 1：超时阈值
  track_file: logs/log.md        # 规则 2：追踪文件
  file_stall_threshold: 3        # 规则 2：连续未变化阈值
  track_keyword_file: logs/log.md  # 规则 3：检测文件
  track_keyword: membase         # 规则 3：关键词
  snapshot_paths:               # 上传切片：整目录一起上传
    - logs/
```

## 文件

| 文件 | 作用 |
|------|------|
| `workflow-controller.yaml` | runtime + starting_prompt + watchdog 配置 |
| `AGENTS.md` | opencode primary（agents list 指向 sleeper） |
| `agents/sleeper.md` | sleeper subagent 定义 |
