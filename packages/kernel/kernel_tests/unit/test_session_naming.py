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

"""Tests for session naming system."""

from workflow_controller.api.session_api import _is_design_md_compliant_name


class TestDesignMdCompliantName:
    """Test _is_design_md_compliant_name regex."""

    @staticmethod
    def test_handoff_name_matches():
        """Handoff names should match."""
        assert _is_design_md_compliant_name("76c3c040-handoff-4") is True
        assert _is_design_md_compliant_name("abc123-handoff-1") is True
        assert _is_design_md_compliant_name("00000000-handoff-999") is True

    @staticmethod
    def test_continuation_name_matches():
        """Continuation names should match."""
        assert _is_design_md_compliant_name("76c3c040-continue-2") is True
        assert _is_design_md_compliant_name("abc123-continue-1") is True
        assert _is_design_md_compliant_name("00000000-continue-999") is True

    @staticmethod
    def test_non_compliant_names_rejected():
        """Non-compliant names should not match."""
        assert _is_design_md_compliant_name("session 07-09 20:30") is False
        assert _is_design_md_compliant_name("ops-registry-invoke") is False
        assert _is_design_md_compliant_name("开发支持float16的xlogy算子") is False
        assert _is_design_md_compliant_name("按既定计划推进任务") is False
        assert _is_design_md_compliant_name("") is False

    @staticmethod
    def test_old_format_with_operator_rejected():
        """Old format with operator prefix should not match."""
        assert _is_design_md_compliant_name("xlogy-76c3c040-handoff-1") is False
        assert _is_design_md_compliant_name("operator-abc123-continue-2") is False
