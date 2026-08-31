"""Mini-tree fixture for the §5 enumerator scope limits — yields no rows at all.

`pytestmark` assignments, imperative in-body skips and mark aliases (including a relative
import) are out of scope: they appear in neither the included nor the excluded set.
"""

import pytest

from .aliases import only_postgresql

pytestmark = pytest.mark.skip(reason="module-level assignment, out of scope")


class TestImperativeSkips:
    def test_imperative_skip(self) -> None:
        pytest.skip("imperative in-body skip, out of scope")

    @only_postgresql
    def test_relative_mark_alias(self) -> None:
        assert True
