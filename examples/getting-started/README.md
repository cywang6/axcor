# Workflow Controller getting-started

用一个**领域无关的轻量任务**真实跑一遍 Workflow Controller，熟悉它的全部能力：
**实时观测 / 控制（暂停 · 改 · 继续）/ reviewer 反馈 / 产物落盘**。

worker 会探索本目录下的 [`sample-project/wordstat`](sample-project)（一个迷你词频统计工具），
读源码、跑一遍、写一份 `SUMMARY.md`。任务无关算子 / NPU，任何配好 API key 的环境都能跑。

---

## 前置

### 1. 装 Workflow Controller + 依赖

Web 模式需要 ASGI server（`uvicorn`/`fastapi`/`sse-starlette`），worker / reviewer 的
`claude_sdk` 路径需要 `claude-agent-sdk`（在 `dev` extras 里）。装法三选一：

```bash
# A) venv + 全依赖（推荐：web + claude-agent-sdk + 测试工具一次装齐）
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/workflow-controller web            # 以后直接用 venv 里的 workflow-controller（已含 console script）

# B) 最小（只要能跑 web + worker）：基础依赖 + 手动补 claude-agent-sdk
pip install -e . && pip install claude-agent-sdk

# C) 全局装（uv）：uv build --wheel && uv tool install dist/workflow_controller-*.whl --force
```

> Web 前端产物已随包（`src/workflow_controller/web/frontend/dist`）；只有改了前端才需
> `cd src/workflow_controller/web/frontend && npx vite build` 重新构建。

### 2. 配 API key

**原生 Anthropic**（默认 `claude_sdk`）：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**GLM / 第三方端点** —— 保持 `claude_sdk` 即可，SDK 经 env 白名单透传，**不用改 runtime**：

```bash
export ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
export ANTHROPIC_AUTH_TOKEN=<glm token>      # Bearer 鉴权（非 x-api-key）
export ANTHROPIC_MODEL=glm-5.2
```

- worker / reviewer 都读上面这些 env（`env_for_driver` 白名单透传
  `ANTHROPIC_AUTH_TOKEN` / `BASE_URL` / `MODEL`）。
- `workflow-controller.yaml` 的 **`reviewer.model` 留空**（`model: ""`）→ reviewer 自动用 env 的
  `ANTHROPIC_MODEL`，无需写死模型名（原生 Anthropic 用户也可留空，SDK 用默认）。
- 想走 CLI 路径（OAuth 等）也可把 `runtime` 改 `claude_cli`，env 同上。

> 详见仓库根 [README](../../README.md) 的「Driver 与 GLM 支持」。

---

## 玩法 A：一键 demo（推荐先跑）

```bash
cd examples/getting-started
python demo.py                 # 默认 :18602；自定义端口 --port 9000
```

`demo.py` 自动起 `workflow-controller web` + 跑 worker session，并在运行中演示：

| 步骤 | 演示的 Workflow Controller 能力 |
|------|------|
| SSE 捕获 | **实时观测**：worker 的 thinking / 工具调用 / 消息 |
| `POST /inject` | **改**：注入 feedback，作下一轮消费 |
| `POST /pause` | **暂停**：iteration 边界暂停 worker |
| `POST /play` | **继续**：`--resume` 续跑 |
| `POST /stop` | **收尾**：停止 session |
| 检查 `SUMMARY.md` | **产物落盘**：worker 写的总结 |

> demo 在 worker 跑完前就演示控制面并 stop，**SUMMARY 可能未生成**（演示控制面的代价）。
> 想验证 worker 端到端写出产物：`workflow-controller run --headless -c workflow-controller.yaml` 完整跑（不打断）。

跑完看终端的 `[观测-汇总]` 与 `控制面四件套` 结论行。

## 玩法 B：手动 Web UI（亲手操作）

```bash
cd examples/getting-started
workflow-controller web                     # 读 workflow-controller.yaml，开 http://127.0.0.1:18600
```

浏览器打开后：

1. **Dashboard** → Project Directory 已指向 `sample-project`（yaml 配的）
2. 点 **Start Session** → worker 开始探索 wordstat
3. **实时观测**：看 Dashboard 的 turn / 工具调用 / token / elapsed 流式更新
4. **控制**：用 Inject Box 改方向，点 Pause / Play / Stop
5. **reviewer 反馈**：开了 reviewer 时，看它如何评估并 push worker
6. **产物**：worker 写完看 `sample-project/SUMMARY.md`

---

## 目录

```
examples/getting-started/
├── README.md           # 本文件
├── workflow-controller.yaml         # demo 配置（project_dir / prompt / reviewer / runtime）
├── demo.py             # 一键自动演示（玩法 A）
└── sample-project/     # worker 的探索目标（迷你 wordstat 词频统计工具）
    ├── README.md
    ├── example.txt
    └── wordstat/{counter,filters,cli}.py
```

## 清理

demo 会产生运行产物（已 gitignore）：

- `sample-project/.workflow-controller/` — Workflow Controller 工作区（events.db / 日志）
- `sample-project/.claude/` — Workflow Controller 写的 hook 配置（给 Claude Code 子进程用，仅 hooks 字段）
- `sample-project/SUMMARY.md` — worker 写的总结
- `.demo-web.log` / `.demo-sse.log` — demo 脚本日志

清掉重跑：

```bash
rm -rf sample-project/.workflow-controller sample-project/.claude sample-project/SUMMARY.md
```

---

## 下一步

- **换成你自己的任务**：改 `workflow-controller.yaml` 的 `project_dir` + `starting_prompt`
- **算子生成场景**：装 cannbot plugin，参考仓库根 [`examples/workflow-controller.yaml`](../workflow-controller.yaml)
- **架构细节**：[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)
