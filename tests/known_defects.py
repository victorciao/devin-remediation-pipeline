"""Plan-vs-code disagreements, expressed as strict xfails with the plan clause named.

Phase A derives expected behaviour from `docs/IMPLEMENTATION_PLAN.md`; where production
disagrees, the assertion stays as the plan implies and is marked here. Strict xfails mean each
one flips the suite red again the moment production is corrected, so none can be forgotten.
"""

from __future__ import annotations

import pytest

LOCAL_RESUME_LOOKUP_DEFECT = (
    "plan-vs-code: §14.1 decides resume for a row that already carries artifact identity from "
    "local evidence alone. `resume_decision` calls `marker_search_orphaned` unconditionally, so "
    "every already-completed candidate costs one search API call, and a search outage aborts a "
    "LIVE run in which nothing was left to publish."
)

local_resume_lookup = pytest.mark.xfail(strict=True, reason=LOCAL_RESUME_LOOKUP_DEFECT)
