# multi-iteration sample

真正需要多轮 iteration 的任务,用来验证 loop 的核心能力:
**iteration 边界 / reviewer 真审查 / feedback 衔接 / 检查点接管**(auto + manual)。

worker 从零写一个计算器 CLI,分 4 阶段:`calc.py` → `cli.py` → `test_calc.py` → `README.md`。
每阶段是一个 checkpoint —— worker 做完当前阶段就停下等判定,不会一口气跑完。

## 跑

```bash
cd examples/multi-iteration
~/workflow-controller/.venv/bin/workflow-controller web   # 浏览器 http://localhost:18600
```

> 首次需装 Workflow Controller + 依赖(见仓库根 README)。本机 GLM env 已全局配好(SDK 透传)。

## auto(验证 loop 自动多 iteration)

- reply 保持 **Auto**
- **Start Session**
- worker 每阶段停下 → reviewer 按 `task_goal` 判产物齐没 → 没齐 push 下一轮
- 预期:session 卡片 `iter X/8` 从 1 涨到 4+,4 件产物逐个写出,最后 COMPLETED

## manual(验证检查点接管)

- reply 切 **Manual**
- **Start Session**
- worker 到阶段 Stop → 你在 InputBox 判定 / 给方向(如「继续写 cli.py」)→ 下轮
- 预期:`iter X/8` 随你每次 Send 增长(worker 到检查点真心等你,不是跑完硬拽)

## 为什么 getting-started 看不到这些

getting-started 用 `always_complete` evaluator(永远判完成)→ 一次 iteration 就 COMPLETED,
没有 iteration 边界 / reviewer 审查。这个 sample 换成 `feedback`(真判定)才有多 iteration ——
这是两者的核心区别。

## 配置要点

- `loop.evaluator: feedback` —— 用真判定的 evaluator(非 always_complete)
- `loop.max_iterations: 8` —— 4 阶段 + reviewer push 余量
- `reviewer.task_goal` —— 四件产物 + 测试通过的完成标准,reviewer 按它判
- `starting_prompt` —— 强制 worker「做完阶段就停」,保证到 checkpoint

## 清理

```bash
rm -rf sample-project/.workflow-controller sample-project/.claude sample-project/calc.py sample-project/cli.py sample-project/test_calc.py sample-project/README.md
```
