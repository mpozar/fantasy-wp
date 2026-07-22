#!/usr/bin/env python3
"""Ad-hoc runner for the CLAUDE.md ↔ code consistency checks.

Same checks as tests/test_docs_consistency.py (symbol references, numeric
constants, stat-id map), but as a script you can run before an investigation or
after a refactor without invoking pytest:

    .venv/bin/python scripts/audit_docs.py

Exits non-zero if anything drifted. The checks live in the test module so there's
exactly one implementation; this just runs them and prints a readable report.
Behavioral prose (e.g. "IL slot is a hard filter") is NOT checkable here — verify
that class in the code and lock it with a targeted unit test instead.
"""
import importlib.util
import pathlib
import sys

_spec = importlib.util.spec_from_file_location(
    "test_docs_consistency",
    pathlib.Path(__file__).resolve().parent.parent / "tests" / "test_docs_consistency.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

CHECKS = [
    ("symbol references exist in code", t.test_doc_symbol_references_exist_in_code),
    ("numeric constants match code", t.test_doc_numeric_constants_match_code),
    ("stat-id map matches app/stats.py", t.test_doc_stat_id_map_matches_stats_module),
]


def main() -> int:
    failed = 0
    for label, fn in CHECKS:
        try:
            fn()
            print(f"  OK   {label}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {label}\n       {e}")
    print(f"\n{'DRIFT DETECTED' if failed else 'clean'} — "
          f"{len(CHECKS) - failed}/{len(CHECKS)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
