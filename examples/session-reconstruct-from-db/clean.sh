#!/usr/bin/env bash
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

# 删除 gen_db.py / demo 产生的文件，便于开一个全新环境重跑。
# 只删除生成产物；源文件（agents/*.md、workflow-controller.yaml、AGENTS.md、demo.py、gen_db.py
# 、README.md 等）及运行时目录（.opencode 等）保留不动。
#
# 用法：
#   ./clean.sh           删除生成的文件
#   ./clean.sh -n        dry-run，只打印将删除什么，不实际删除
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

DRY_RUN=0
if [[ "${1:-}" == "-n" || "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

# gen_db.py 产物 + workflow 产物（live 重放时 subagent / 主 agent 写出的文件）
GENERATED_FILES=(
    cannbot_session.db
    design.md
    impl.py
    test.py
    summary.md
)

rm_path() {
    local target="$1"
    if [[ -e "$target" || -L "$target" ]]; then
        if (( DRY_RUN )); then
            echo "  [dry-run] would remove: $target"
        else
            rm -rf -- "$target"
            echo "  removed: $target"
        fi
    fi
}

echo "cleaning generated files in: $HERE"

for f in "${GENERATED_FILES[@]}"; do
    rm_path "$f"
done

if (( DRY_RUN )); then
    echo "dry-run done — nothing was deleted. Re-run without -n to actually delete."
else
    echo "done — directory is ready for a fresh run."
fi
