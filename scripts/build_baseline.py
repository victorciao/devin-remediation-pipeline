#!/usr/bin/env python3
"""Phase 0c: capture a BASELINE snapshot of REPO B and derive the SIMULATE fixture.

Read-only. Walks a local Superset checkout and records the lane-relevant inventory exactly
as the LANE 2 / LANE 3 enumerators define it (plan §5, §4.2), plus the CodeQL alert set read
from a previously captured alert fixture.

Usage:
    build_baseline.py <superset-checkout> <out-dir> [codeql-alerts.json]
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pipeline.lanes.deprecations import collect_deprecations, current_release, is_eol
from pipeline.lanes.skipped_tests import collect_skipped_tests

VERSION_SOURCE = ".github/ISSUE_TEMPLATE/bug-report.yml"
EOL_MAJOR_LAG = 2


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> int:
    repo = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    alerts_path = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None

    release, major = current_release(repo)
    skipped, excluded = collect_skipped_tests(repo)
    deprecations = collect_deprecations(
        repo,
        current_major=major,
        current_release_value=release,
        eol_major_lag=EOL_MAJOR_LAG,
    )

    if alerts_path and alerts_path.exists():
        alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
        codeql = {
            "open": alerts,
            "accessible": True,
            "source": str(alerts_path.name),
        }
    else:
        codeql = {
            "open": [],
            "accessible": False,
            "note": "code scanning was capability_unavailable when this snapshot was taken",
        }

    baseline = {
        "captured_at": datetime.now(UTC).isoformat(),
        "repo": "victorciao/superset",
        "head_sha": _git(repo, "rev-parse", "HEAD"),
        "default_branch": "master",
        "current_release": release,
        "current_major": major,
        "eol_major_lag": EOL_MAJOR_LAG,
        "eol_threshold_major": major - EOL_MAJOR_LAG,
        "version_source": VERSION_SOURCE,
        "codeql_alerts": codeql,
        "skipped_tests": skipped,
        "excluded_conditional_skips": excluded,
        "deprecations": deprecations,
    }
    baseline["baseline_valid_lanes"] = [
        lane
        for lane, ok in (
            ("codeql", codeql["accessible"]),
            ("skipped_tests", True),
            ("deprecations", True),
        )
        if ok
    ]
    baseline["totals"] = {
        "skipped_tests": len(skipped),
        "excluded_conditional_skips": len(excluded),
        "deprecations": len(deprecations),
        "eol_passed_deprecations": sum(
            1
            for item in deprecations
            if is_eol(
                str(item["deprecated_in"]),
                major,
                removed_in=(str(item["removed_in"]) if item["removed_in"] is not None else None),
                current_release=release,
                eol_major_lag=EOL_MAJOR_LAG,
            )
        ),
        "codeql_open_alerts": len(codeql["open"]),
        "multi_item_skipped_tests": sum(1 for s in skipped if not s["collects_single_item"]),
        "nested_under_skipped_class": sum(1 for s in skipped if s["enclosing_skip_nodeid"]),
    }

    path = out_dir / "baseline.json"
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}: {baseline['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
