"""Mini-tree fixture for the §5 LANE 2 enumerator (2 included / 2 excluded).

Not named ``test_*.py`` so the REPO A suite does not collect it by default; the tests that
need it collect it explicitly with ``-o python_files=skipmod_*.py``.
"""

import unittest
from unittest import skip

import pytest

RUN_OPTIONAL = False


class TestAliasedBareSkip(unittest.TestCase):
    @skip("Flaky on CI")
    def test_aliased_bare_skip(self) -> None:
        assert True

    @unittest.skipUnless(RUN_OPTIONAL, "optional backend not installed")
    def test_conditional_guard(self) -> None:
        assert True


class TestQualifiedMarks:
    @pytest.mark.skip(reason="broken since the api/v1 migration")
    def test_qualified_skip(self) -> None:
        assert True

    @pytest.mark.xfail(reason="known failure, still collected")
    def test_expected_failure(self) -> None:
        raise AssertionError
