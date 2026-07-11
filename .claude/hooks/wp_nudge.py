"""UserPromptSubmit hook: inject the evidence-first reminder when the prompt
looks like a WP investigation. Backstop for the wp-investigate skill (fires
even if the skill doesn't trigger). See .claude/skills/wp-investigate/SKILL.md."""
import json
import re
import sys

try:
    prompt = json.load(sys.stdin).get("prompt", "")
except Exception:
    sys.exit(0)

MOVE = re.compile(
    r"caus|drop|jump|swing|mov|chang|increas|decreas|spike|dip|fell|fall"
    r"|rose|climb|crater|suppress|wrong|weird|odd", re.I)
looks_like_investigation = (
    re.search(r"any flags", prompt, re.I)
    or re.search(r"what (caused|happened|drove)", prompt, re.I)
    or (re.search(r"\b(wp|win ?prob\w*)\b", prompt, re.I) and MOVE.search(prompt))
    or (re.search(r"\bwhy (did|is|was|does|are)\b", prompt, re.I) and MOVE.search(prompt))
)

if looks_like_investigation:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "WP-investigation prompt detected — follow the wp-investigate skill: "
                "(1) run `.venv/bin/python scripts/wp_diff.py <team> <start> <end>` BEFORE "
                "any causal statement (naive times = Europe/Oslo); (2) weigh EVERY delta in "
                "its output — a LEADER-FLIPPED category dominates; salient ≠ causal; "
                "(3) verify mechanics in code with file:line, never from memory "
                "(slot 17=IL, 16=bench); (4) label anything unconfirmed 'hypothesis:'; "
                "(5) append the outcome to INVESTIGATIONS.md."
            ),
        }
    }))
