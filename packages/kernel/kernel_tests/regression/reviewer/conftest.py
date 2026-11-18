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

"""Conftest for reviewer replay regression tests.

Provides the --live CLI flag. Without it, every test marked `live_llm`
is skipped — replays call the LLM via SDK which is slow and costs API
tokens, so CI / regular `pytest` runs should not trigger them.

Usage:
  pytest tests/regression/reviewer                        # auto-skip (default)
  pytest tests/regression/reviewer --live                 # actually call the reviewer LLM
  pytest tests/regression/reviewer --live -k let_exit     # single fixture
"""
# --live is registered at tests/conftest.py so collection is consistent even
# when this directory is not part of the selected test path.
