"""Guard CLAUDE.md against the *mechanically checkable* class of doc-drift —
the kind that has caused real misdiagnoses (a stale symbol name, a constant that
moved, a mis-stated stat id). This does NOT (and can't) verify behavioral prose
like "IL slot is a hard filter"; that class is locked by targeted unit tests
(e.g. test_il_activation.py) and by the standing rule that the code is ground
truth (CLAUDE.md contract #4). What it does catch, automatically, on every run:

  1. Backtick'd symbol references (functions / CONSTANTS / module.attr) that no
     longer exist anywhere in app/ or scripts/ — i.e. renamed or deleted.
  2. Numeric constants the doc quotes (`NAME = 0.21`, `NAME (200)`) that disagree
     with the code's actual value.
  3. The stat-id map the doc hardcodes vs app/stats.py.

A failure here means the doc and code have diverged — fix whichever is wrong
(usually the doc). Rerun ad hoc with `scripts/audit_docs.py`.
"""
import re
import pathlib

from app import sim, validate, cli, playoffs, stats  # noqa: F401 (used via getattr)

REPO = pathlib.Path(__file__).resolve().parent.parent
DOC = (REPO / "CLAUDE.md").read_text()
SRC = " ".join(p.read_text() for p in (REPO / "app").glob("*.py"))
SRC += " ".join(p.read_text() for p in (REPO / "scripts").glob("*.py"))
# docs/ basenames so file references like `index.html` / `app.js` resolve.
DOCS_FILES = {p.name for p in (REPO / "docs").rglob("*")}

# Backtick tokens the doc uses; exempt a few that aren't code symbols.
_EXEMPT = {"binds"}


def _symbol_like(t):
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", t)          # CONSTANT
                or re.fullmatch(r"_?[a-z][a-z0-9_]{3,}", t)      # func_name / _helper
                or re.fullmatch(r"[a-z_]+\.[a-z_]+", t))         # module.attr


def test_doc_symbol_references_exist_in_code():
    missing = []
    for raw in set(re.findall(r"`([^`]+)`", DOC)):
        t = raw.strip()
        if not _symbol_like(t) or t in _EXEMPT or t in DOCS_FILES:
            continue
        needle = t.split(".")[-1]
        if not re.search(r"\b" + re.escape(needle) + r"\b", SRC):
            missing.append(t)
    assert not missing, (
        "CLAUDE.md references symbols not found in app/ or scripts/ "
        f"(renamed/deleted? add to _EXEMPT if intentional): {sorted(missing)}")


def test_doc_numeric_constants_match_code():
    code_vals = {}
    for mod in (sim, validate, cli, playoffs):
        for k in dir(mod):
            v = getattr(mod, k)
            if k.isupper() and isinstance(v, (int, float)) and not isinstance(v, bool):
                code_vals.setdefault(k, v)
    mismatches = []
    for name, val in code_vals.items():
        # First doc occurrence of "NAME = n" / "NAME (n)". Scope: catches the
        # primary threat (code constant changes → the doc's stated value goes
        # stale). A drifted *second* mention of the same constant isn't checked.
        m = re.search(re.escape(name) + r"`?\s*(?:=|\()\s*([0-9]+(?:\.[0-9]+)?)", DOC)
        if m and abs(float(m.group(1)) - float(val)) > 1e-9:
            mismatches.append(f"{name}: doc={m.group(1)} code={val}")
    assert not mismatches, f"CLAUDE.md constant values disagree with code: {mismatches}"


def test_doc_stat_id_map_matches_stats_module():
    # The scored-cat map the playbook hardcodes (stat_id → label).
    doc_ids = {1: "H", 5: "HR", 18: "OPS", 20: "R", 23: "SB",
               34: "OUTS", 41: "WHIP", 47: "ERA", 48: "K", 63: "QS", 83: "SVHD"}
    wrong = {sid: (label, stats.name(sid)) for sid, label in doc_ids.items()
             if label.upper() not in stats.name(sid).upper()}
    assert not wrong, f"CLAUDE.md stat-id labels disagree with app/stats.py: {wrong}"
