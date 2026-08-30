"""Plan-vs-code disagreements, expressed as strict xfails with the plan clause named.

Phase A derives expected behaviour from `docs/IMPLEMENTATION_PLAN.md`; where production
disagrees, the assertion stays as the plan implies and is marked here. Strict xfails mean each
one flips the suite red again the moment production is corrected, so none can be forgotten.
"""

from __future__ import annotations

import pytest

MARKER_ABSENCE_DEFECT = (
    "plan-vs-code: §14.1 makes a marker search that returns no artifact mean the marker is "
    "*absent*, which is precisely the precondition for a first write. "
    "`CandidateStateStore.marker_search_unavailable` infers failure from a `None` result rather "
    "than from the search having raised, so a configured search that successfully finds nothing "
    "reads as an unavailable capability: `resume_decision` defers every unseen candidate and "
    "`append_if_new_artifact` refuses every first reservation. No candidate can be dispatched in "
    "LIVE. The fix is a per-candidate `raised` set, not a `None` result."
)
RESERVATION_DEADLOCK_DEFECT = (
    "plan-vs-code: §14.1 requires the first artifact write to be reserved atomically. "
    "`append_if_new_artifact` holds `flock(LOCK_EX)` on `candidates.jsonl.lock` and then calls "
    "`append`, which opens the same lock file again and blocks against its own caller — POSIX "
    "record locks are per open file description, so the reservation never returns and the run "
    "hangs forever rather than failing."
)
LOCAL_RESUME_LOOKUP_DEFECT = (
    "plan-vs-code: §14.1 decides resume for a row that already carries artifact identity from "
    "local evidence alone. `resume_decision` calls `marker_search_orphaned` unconditionally, so "
    "every already-completed candidate costs one search API call, and a search outage aborts a "
    "LIVE run in which nothing was left to publish."
)

marker_absence = pytest.mark.xfail(strict=True, reason=MARKER_ABSENCE_DEFECT)
local_resume_lookup = pytest.mark.xfail(strict=True, reason=LOCAL_RESUME_LOOKUP_DEFECT)
