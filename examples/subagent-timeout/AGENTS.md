---
description: 演示 watchdog 三条规则——subagent 超时 + 文件不变 + 关键词检测。配置写死在 yaml 中，选目录即生效。
mode: primary
agents:
  - sleeper
---

# watchdog 三条规则示例

选本目录后 watchdog 自动配置（workflow-controller.yaml 中的 `watchdog:` 段），无需手动设置。

## 任务流程

1. 清理旧 `log.md`。
2. 写入 `log.md`（"初始化测试"）。
3. 起 `sleeper` 每 2 秒打印 × 5 次（约 10 秒）→ 超时告警。
4. 起两个快 subagent → 文件不变告警（连续 3 次 stop，log.md 没变）。
5. 起 subagent 写"membase"到 `log.md` → 关键词告警。
6. 报告"测试完成"。
