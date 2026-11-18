# AscendC 算子（aclnn 注册调用）staged-pipeline 样例

> 这是 `staged_pipeline` 应用于「真复杂」领域（cannbot registry 算子生成 workflow）的样例。门禁机制见 [`stage-gating-design.md`](../../docs/designs/stage-gating.md)；可跑的最小自包含样例（roman 数字转换器）见 [`../staged-pipeline/`](../staged-pipeline/)。

cannbot 的 registry 算子生成 workflow（skill `ops-registry-invoke-workflow`：需求 → 设计 → 三轮开发迭代 → 精度/性能验收 → 上库，含 CP1–CP5 检查点 + spec 9-stage + cp2/cp3 validator）是「真复杂」领域的样板。它的 stages 镜像如下——worker 当该 workflow 的 primary agent（用 Task 工具分发 architect/developer/tester/reviewer 四个 subagent），Workflow Controller 在每个门界强制顺序，并把 registry 原本**软引导**的三道机器校验（spec 9-stage / cp2 / cp3）用 `gate: command` 升级成**运行时硬门**（rc==0 才放行）——这正是 Workflow Controller 套在 registry 外面补的「运行时强制力」。

## 用法

**算子生成时直接拷贝** [`workflow-controller.yaml`](workflow-controller.yaml) 到算子工作目录（= `operators/<operator_name>/`，即 validator 的 `--operator-dir`），改 `<尖括号>` 占位即可用。

## 拷贝后要改的

`<operator_name>` / `<一句话功能>` / `算子参数`（dtype·shape·平台·是否含性能指标）；`stages` 一般不动（镜像 registry workflow，对所有算子通用）。

## 前置（workflow-controller.yaml 本身变不出的环境）

- `staged_pipeline` plugin 全局启用（[设计文档 §5.1](../../docs/designs/stage-gating.md)）；
- 环境变量 `CANNBOT_SKILLS` 指向 cannbot-skills 检出（command 门靠它定位两个 validator）；
- worker runtime 支持 subagent（opencode/claude_sdk，registry primary 要分发 4 个 subagent）；
- 全局 `skills_path` 指 cannbot-skills、`agents_path` 指 registry 的 4 个 agent；
- `asc-devkit` 已 init、NPU+CANN 就绪（ST 禁 Mock）。

> ⚠️ **非自包含样例**：跑起来需要 cannbot-skills + NPU + CANN，不能像 [`../staged-pipeline/`](../staged-pipeline/) 那样在本仓直接 end-to-end 跑。这份是「拷贝到算子工作目录当门禁镜像」的参考 yaml。

## 两处要知晓的取舍

（不影响拷贝即用，但碰到别意外）

- **一个 stage 可挂多个 artifact（`artifacts:` 列表，各自独立 gate）**：如 `design` 阶段实际产 DESIGN/TEST/PLAN/CSV 多个文件，可全列入门禁、也可只挂核心文件（如 `DESIGN.md`）。不必把同阶段每个文件都挂门禁——未挂的靠 plugin 自动注入的逐阶段工作纪律（[设计文档 §4.4](../../docs/designs/stage-gating.md)「一轮写完该阶段全部文件、然后停下」）同轮落盘，漏写的归 per-stage reviewer 查（不是门禁的活）。
- **可选/条件 stage**：3.2 性能验收是可选分支（仅需求含性能指标时才跑）。线性 stages 没有「N/A 就跳过」的语义——无性能需求时**不列**该 stage（worker 在 `precision`/`release` 里 ungated 做或不做）；有性能需求时在 `precision` 后加：

  ```yaml
  - name: perf
    artifacts:
      - artifact: docs/performance-report.md
        gate: non_placeholder
  ```
