# staged-pipeline example

演示 **阶段门禁**：项目在自己的 `workflow-controller.yaml` 里声明 `stages`，`staged_pipeline` plugin
在 iteration 边界**确定性强制阶段顺序**、stop 时**查完成**——worker 跳步被拦回、没收齐
阶段产物不许结束。任务用一个小而具体的「罗马数字转换器」，让 spec→retrospect 五阶段
产出有意义、各不同的产物。

## 与其他 example 的区别

| | getting-started | **本 example** |
|---|---|---|
| 演示 | 控制面四件套 + reviewer | **阶段门禁（顺序 + 完成）** |
| 领域配置 | 无 | 项目 `workflow-controller.yaml` 声明 `stages` |
| 完成 | reviewer 判 | **门禁（确定性）→ reviewer（语义）两层** |

## 前置

1. **启用 staged_pipeline plugin**（关键，否则没有门禁）：web Settings → Plugins →
   找到 `staged_pipeline` → **Enable**。启用是全局的、一次即可——只对声明了 `stages`
   的项目生效（没声明的 session 门禁惰性）。
2. 配 API key（同 getting-started）：
   ```bash
   export ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic  # 或用原生 Anthropic
   export ANTHROPIC_AUTH_TOKEN=<token>
   export ANTHROPIC_MODEL=glm-5.2
   ```

## 运行

```bash
.venv/bin/workflow-controller web
```

web 主面板 **Browse 选本目录**（`examples/staged-pipeline`）→ **Start Session**。
worker 读 `sample-project/README.md` 的任务说明，按五阶段推进，产物写到本目录根。

## 观察

**web 的 Claude Output / Interactions 面板** 看 worker 的每轮产出与门禁回注；
`{project_dir}/.workflow-controller/workflow-controller.log`（写 project_dir 的 .workflow-controller，不在 web stdout）看门禁判定。

门禁会怎么表现：

| 情况 | 门禁 | 你看到 |
|---|---|---|
| worker 跳步（如直接写 `impl/` 但 `plan.md` 还没过门禁） | **顺序门** block | 回注「先完成 `plan`，你跳到了 `execute`」，loop 带 feedback 再跑一轮 |
| worker 想收尾但 5 个产物没齐 | **完成门** block | 点名「当前应推进：`spec`」（或当前缺的那个），退不出去 |
| 5 个产物全过门禁 | 完成门放行 | 落到通用 reviewer 判真实性 → `let_exit` → loop `COMPLETED` |

> 门禁只在 **iteration 边界**生效：单次 agent-loop 内的跳步要等下一轮边界才发现。
> 想看「跳步→block→补全→放行」的确定性证明，跑
> `pytest tests/workflow_controller_core/unit/test_stages.py tests/workflow_controller_core/unit/test_staged_pipeline_plugin.py -q`（不耗 LLM）。

## 目录

```
examples/staged-pipeline/
├── README.md            # 本文件
├── workflow-controller.yaml          # stages + starting_prompt + task_goal（门禁 + 任务声明）
├── sample-project/
│   └── README.md        # 任务说明（给 worker 的罗马数字转换器需求）
└── .gitignore           # 忽略 worker 产出的 spec.md/plan.md/impl//verify.md/retro.md + .workflow-controller/.claude/
```

产物（`spec.md` / `plan.md` / `impl/` / `verify.md` / `retro.md`）由 worker 五阶段产出，
**不预置**（预置会糊弄门禁），运行后出现在本目录根，已 gitignore。

## 清理

```bash
rm -rf spec.md plan.md impl verify.md retro.md .workflow-controller .claude
```

## 换领域

改 `workflow-controller.yaml` 的 `stages`（阶段名 / artifact / 门禁模式 / command）+ `starting_prompt`
+ `reviewer.task_goal` 即可——零动核心、零 plugin 复制。
