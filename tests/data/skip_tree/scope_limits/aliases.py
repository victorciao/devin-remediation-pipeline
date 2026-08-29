"""Mark alias bound at module level — the enumerator must not treat it as a skip decorator."""

import pytest

only_postgresql = pytest.mark.skipif(True, reason="postgres-only")
