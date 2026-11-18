#!/usr/bin/env python3
# Copyright (c) 2026 Workflow Controller Contributors
#
# Licensed under the MIT License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Workflow Controller getting-started demo —— 真实演练控制面四件套 + 实时观测。

一键起 ``workflow-controller web`` + 跑一个「代码探索」worker session，并在运行中演示：

  [观测]   SSE 实时流  —— worker 的 thinking / 工具调用 / 消息
  [改]     inject      —— 注入 feedback，作下一轮消费
  [暂停]   pause       —— 在 iteration 边界暂停 worker
  [继续]   play        —— 恢复 worker 续跑
  [收尾]   stop        —— 停止 session

worker 跑的是领域无关的轻量任务（探索 ``sample-project/wordstat``，写 SUMMARY.md），
所以任何配好 API key 的环境都能跑。

用法::

    cd examples/getting-started
    python demo.py                 # 默认端口 18602
    python demo.py --port 9000

前置：Workflow Controller 已装（``pip install -e .``）+ API key 已配（见 README.md）。

注：本 demo 在 worker 跑完前就演示控制面并 stop，故 ``SUMMARY.md`` 可能未生成
（演示控制面的代价，非错误）。想验证 worker 端到端写出产物，用
``workflow-controller run --headless -c workflow-controller.yaml`` 完整跑（不打断）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    # setLevel 只能设下限，封顶须用 filter，否则 WARNING+ 会双写到两个流
    stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    logger.setLevel(logging.INFO)


HOST = "127.0.0.1"
DEFAULT_PORT = 18602
WS = str(HERE / "sample-project")
SSE_SECS = 30


def find_controller_cmd() -> tuple[list[str], dict[str, str]]:
    """定位 Workflow Controller 启动命令 + 额外 env。

    优先级：``WORKFLOW_CONTROLLER_BIN`` env（完整命令，如 ``"workflow-controller"``）>
    PATH 里的 ``workflow-controller`` > 开发模式 ``python -m workflow_controller`` + 仓库 ``src``。
    """
    if "WORKFLOW_CONTROLLER_BIN" in os.environ:
        return shlex.split(os.environ["WORKFLOW_CONTROLLER_BIN"]), {}
    controller = shutil.which("workflow-controller")
    if controller is not None:
        try:
            subprocess.run([controller, "--help"], capture_output=True, check=True)
            return [controller], {}
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    src = HERE.parent.parent / "src"  # examples/getting-started → <repo>/src
    if src.is_dir():
        return [sys.executable, "-m", "workflow_controller"], {**os.environ, "PYTHONPATH": str(src)}
    raise RuntimeError(
        "找不到 workflow-controller：请 `pip install -e .`，或设 WORKFLOW_CONTROLLER_BIN 环境变量。"
    )


def banner(title: str) -> None:
    logger.info(f"\n{'─' * 64}\n  {title}\n{'─' * 64}")


def req(method: str, base: str, path: str, body=None, timeout: int = 15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"<{type(e).__name__}: {e}>"


def tail(path: Path, n: int = 1500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except Exception:
        return "(no log)"


def _spawn_web(args: argparse.Namespace, web_log_path: Path) -> subprocess.Popen | None:
    """起 workflow-controller web 子进程；找不到命令时记日志并返回 None。"""
    try:
        cmd, child_env = find_controller_cmd()
    except RuntimeError as exc:
        logger.error(f"[demo] {exc}")
        return None
    with open(web_log_path, "w", encoding="utf-8") as web_log:
        return subprocess.Popen(
            [*cmd, "web", "--port", str(args.port), "--host", args.host, "--no-open"],
            cwd=str(HERE),
            env=child_env,
            stdout=web_log,
            stderr=subprocess.STDOUT,
            text=True,
        )


def _wait_web_up(proc: subprocess.Popen, base: str, web_log_path: Path) -> bool:
    """轮询 /api/health 直到 web 就绪；失败时打印日志并返回 False。"""
    for _ in range(40):
        if proc.poll() is not None:
            logger.info(f"  web 提前退出 rc={proc.returncode}\n{tail(web_log_path)}")
            return False
        if req("GET", base, "/api/health")[0] == 200:
            return True
        time.sleep(0.5)
    logger.info(f"  ✗ web 未起\n{tail(web_log_path)}")
    return False


def _start_worker_session(base: str, web_log_path: Path) -> str | None:
    """POST /api/sessions/start 起一个 worker session；失败时返回 None。"""
    st, body = req("POST", base, "/api/sessions/start", {"project_dir": WS}, timeout=20)
    sid = None
    with contextlib.suppress(Exception):
        sid = json.loads(body).get("session_id")
    logger.info(f"  POST /api/sessions/start → {st}  sid={sid}")
    if not sid:
        logger.info(f"  ✗ start 失败：{body[:200]}\n{tail(web_log_path)}")
    return sid


def _start_sse_capture(base: str, sse_log_path: Path) -> subprocess.Popen:
    """后台 curl 捕获 SSE 实时流，写入 sse 日志。"""
    curl = shutil.which("curl")
    if curl is None:
        raise RuntimeError("找不到 curl：请安装 curl 并加入 PATH。")
    with open(sse_log_path, "w") as sse_out:
        return subprocess.Popen(
            [curl, "-sN", "-m", str(SSE_SECS), f"{base}/api/events/stream"],
            stdout=sse_out,
            stderr=subprocess.DEVNULL,
        )


def _log_session_state(base: str, sid: str) -> None:
    """等 8 秒后查询并打印 worker session 的状态快照。"""
    t0 = time.time()
    time.sleep(8)
    _, b_ = req("GET", base, f"/api/sessions/{sid}")
    try:
        obj = json.loads(b_)
        logger.info(
            f"  state={obj.get('state')}  iter={obj.get('current_iteration')}  "
            f"(worker 已跑 {time.time() - t0:.0f}s)"
        )
    except Exception:
        logger.info(f"  session 查询：{b_[:120]}")


def _run_control_plane(base: str, sid: str) -> tuple[int, int, int, int]:
    """演示控制面四件套 inject → pause → play → stop；返回四次调用的状态码。"""
    banner("[改] inject —— 注入 feedback（入队作下一轮消费）")
    st_i, bi = req(
        "POST",
        base,
        f"/api/sessions/{sid}/inject",
        {"text": "[demo] 重点分析停用词过滤为何没接入主流程，写进 SUMMARY。"},
        timeout=8,
    )
    logger.info(f"  POST /inject → {st_i}  {bi[:80]}")

    banner("[暂停] pause —— 在 iteration 边界暂停 worker")
    st_p, _ = req("POST", base, f"/api/sessions/{sid}/pause", timeout=8)
    time.sleep(3)
    obj = json.loads(req("GET", base, f"/api/sessions/{sid}")[1])
    logger.info(f"  POST /pause  → {st_p}   state after = {obj.get('state')}")

    banner("[继续] play —— 恢复 worker 续跑")
    st_py, _ = req("POST", base, f"/api/sessions/{sid}/play", timeout=8)
    time.sleep(2)
    obj = json.loads(req("GET", base, f"/api/sessions/{sid}")[1])
    logger.info(f"  POST /play   → {st_py}   state after = {obj.get('state')}")

    banner("[收尾] stop —— 停止 session")
    st_s, _ = req("POST", base, f"/api/sessions/{sid}/stop", timeout=12)
    time.sleep(2)
    obj = json.loads(req("GET", base, f"/api/sessions/{sid}")[1])
    logger.info(f"  POST /stop   → {st_s}   state after = {obj.get('state')}")
    return st_i, st_p, st_py, st_s


def _report_sse(sse: subprocess.Popen | None, sse_log_path: Path) -> int:
    """收 curl 进程、统计 SSE 流并打印汇总；返回 claude_event 数。"""
    if sse and sse.poll() is None:
        sse.terminate()
        try:
            sse.wait(timeout=5)
        except Exception:
            sse.kill()
    raw = (
        sse_log_path.read_text(encoding="utf-8", errors="replace") if sse_log_path.exists() else ""
    )
    n_ev = raw.count("event:")
    n_claude = raw.count("event: claude_event")
    subtypes: dict[str, int] = {}
    for m in re.finditer(r'"subtype"\s*:\s*"([^"]+)"', raw):
        subtypes[m.group(1)] = subtypes.get(m.group(1), 0) + 1
    banner("[观测-汇总] SSE 流")
    logger.info(f"  {n_ev} event 行，含 {n_claude} 个 claude_event（worker payload）")
    if subtypes:
        logger.info(f"  worker 活动：{subtypes}")
    return n_claude


def _report_artifact() -> None:
    """检查 worker 是否写出 sample-project/SUMMARY.md。"""
    banner("[产物]")
    summary = HERE / "sample-project" / "SUMMARY.md"
    if summary.exists():
        logger.info(f"  ✓ worker 写了 sample-project/SUMMARY.md（{summary.stat().st_size} bytes）")
    else:
        logger.info("  SUMMARY.md 未生成 —— demo 在 worker 跑完前就演示了控制面并 stop，")
        logger.info(
            "  worker 没机会写完（非错误）。想看产物：workflow-controller run --headless -c workflow-controller.yaml"
        )


def _log_final_summary(ctrl_ok: bool, n_claude: int) -> None:
    """打印控制面与实时观测的最终结论。"""
    logger.info(f"\n{'═' * 64}")
    logger.info(f"  控制面四件套（inject/pause/play/stop 全 200）：{'✓' if ctrl_ok else '✗'}")
    logger.info(f"  实时观测 SSE（有 worker payload）：{'✓' if n_claude > 0 else '(无，见上)'}")
    logger.info(f"{'═' * 64}")


def main() -> int:
    _configure_logging()
    ap = argparse.ArgumentParser(description="Workflow Controller getting-started demo")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=HOST)
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"
    web_log_path = HERE / ".demo-web.log"
    sse_log_path = HERE / ".demo-sse.log"

    banner("Workflow Controller getting-started demo")
    logger.info(f"  config : {HERE / 'workflow-controller.yaml'}")
    logger.info(f"  target : {WS}  (worker 探索 wordstat，写 SUMMARY.md)")
    logger.info(f"  web    : {base}")

    banner("[启动] workflow-controller web（读 workflow-controller.yaml）")
    proc = _spawn_web(args, web_log_path)
    if proc is None:
        return 1
    sse = None
    try:
        if not _wait_web_up(proc, base, web_log_path):
            return 2
        logger.info("  ✓ web up")

        banner("[观测-准备] 起一个 worker session")
        sid = _start_worker_session(base, web_log_path)
        if not sid:
            return 2

        banner(f"[观测] 捕获 {SSE_SECS}s SSE 实时流，看 worker 干什么")
        sse = _start_sse_capture(base, sse_log_path)
        _log_session_state(base, sid)

        st_i, st_p, st_py, st_s = _run_control_plane(base, sid)

        n_claude = _report_sse(sse, sse_log_path)
        _report_artifact()

        ctrl_ok = all(s == 200 for s in (st_i, st_p, st_py, st_s))
        _log_final_summary(ctrl_ok, n_claude)
        return 0 if ctrl_ok else 2
    finally:
        if sse and sse.poll() is None:
            sse.kill()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        logger.info("\n[demo] web 已停止。完整 web 日志见 .demo-web.log")


if __name__ == "__main__":
    sys.exit(main())
