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

"""命令行入口。

用法::

    python -m wordstat.cli <file>
    python -m wordstat.cli example.txt
"""

import logging
import sys

from wordstat.counter import count_file

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv=None) -> int:
    _configure_logging()
    args = list(argv if argv is not None else sys.argv[1:])

    if not args or args[0] in ("-h", "--help"):
        logger.info("usage: python -m wordstat.cli <file>")
        return 0 if args else 1

    for word, n in count_file(args[0]):
        logger.info("%5d  %s", n, word)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
