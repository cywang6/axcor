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

"""HookServer CLI — start the hook server from the terminal.

Usage:
    workflow-controller-hookserver [--port PORT] [--host HOST]
"""

import argparse
import asyncio
import json
import logging
import signal
import sys

from workflow_controller_hookserver import HookServer

log = logging.getLogger(__name__)


async def _echo_handler(data: dict) -> None:
    log.info("[%s] session=%s", data["event_type"], data["session_id"])


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standalone Claude Code / opencode hook server")
    p.add_argument("--port", type=int, default=18765, help="Listen port (default: 18765)")
    p.add_argument("--host", type=str, default="localhost", help="Listen host (default: localhost)")
    p.add_argument(
        "--print-events",
        action="store_true",
        help="Print every hook event as JSON to stdout",
    )
    return p.parse_args()


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main() -> None:
    args = _parse_args()
    _configure_logging()

    async def _on_event(data: dict) -> None:
        if args.print_events:
            log.info("%s", json.dumps(data, indent=2, default=str))

    async def _run() -> None:
        server = HookServer(
            port=args.port,
            host=args.host,
            on_event=_on_event,
        )
        await server.start()
        log.info("HookServer listening on http://%s:%s", args.host, args.port)
        log.info("Receiving: /hooks/post-tool-use, /hooks/pre-tool-use, /hooks/stop, ...")
        log.info("Press Ctrl+C to stop")

        stop_event = asyncio.Event()

        def _handler(*_: object) -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _handler)
        loop.add_signal_handler(signal.SIGTERM, _handler)

        await stop_event.wait()
        await server.stop_server()
        log.info("\nHookServer stopped.")

    asyncio.run(_run())


if __name__ == "__main__":
    main()
