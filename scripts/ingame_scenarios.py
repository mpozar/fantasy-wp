"""Eyeball the in-game QS/SVHD model on hand-built mock scenarios.

A quick way to see how `app/ingame.py` projects various live game states
without waiting for real games or wiring anything into the pipeline. Tweak the
model, re-run, read the table.

    .venv/bin/python scripts/ingame_scenarios.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingame import StarterState, RelieverState, project_qs, project_svhd


def _sp(label, **kw):
    base = dict(game_status="In Progress", appeared=True, exited=False,
                outs=0, er=0, exp_outs_per_start=16.5, er_per_out=0.13,
                pregame_qs_rate=0.55)
    base.update(kw)
    return label, StarterState(**base)


def _rp(label, **kw):
    base = dict(game_status="In Progress", appeared=True, exited=False,
                entered_save_situation=True, lead_intact=True, recorded_out=True,
                svhd_rate=0.45, game_script_gate=1.0, conversion_prob=0.85)
    base.update(kw)
    return label, RelieverState(**base)


QS_SCENARIOS = [
    _sp("pre-game (not started)", game_status="Scheduled", appeared=False),
    _sp("1st inning, cruising (3 outs, 0 ER)", outs=3, er=0),
    _sp("through 4 (12 outs, 0 ER)", outs=12, er=0),
    _sp("through 5 (15 outs, 1 ER)", outs=15, er=1),
    _sp("6 IP / 1 ER, STILL IN", outs=18, er=1, exp_outs_per_start=19),
    _sp("6 IP / 1 ER, EXITED (game live)", outs=18, er=1, exited=True),
    _sp("5.2 IP / 0 ER, EXITED (short)", outs=17, er=0, exited=True),
    _sp("getting shelled (12 outs, 5 ER)", outs=12, er=5),
    _sp("7 IP / 2 ER, EXITED then game FINAL", outs=21, er=2,
        exited=True, game_status="Final"),
]

SVHD_SCENARIOS = [
    _rp("closer, not in yet — close (gate 1.0)", appeared=False, game_script_gate=1.0),
    _rp("closer, not in yet — blowout (gate 0.3)", appeared=False, game_script_gate=0.3),
    _rp("closer, not in yet — losing big (gate 0.1)", appeared=False, game_script_gate=0.1),
    _rp("in the 9th, save spot, lead intact", entered_save_situation=True, lead_intact=True),
    _rp("in the 9th, blew the lead", lead_intact=False),
    _rp("mop-up, not a save spot", entered_save_situation=False),
    _rp("setup man EXITED with the hold (game live)", exited=True),
    _rp("EXITED having blown it", exited=True, lead_intact=False),
    _rp("save earned, game FINAL", exited=True, game_status="Final"),
]


def main() -> None:
    print("\n=== QS (remaining contribution) ===")
    for label, s in QS_SCENARIOS:
        print(f"  {label:<42} {project_qs(s):.3f}")
    print("\n=== SVHD (remaining contribution) ===")
    for label, s in SVHD_SCENARIOS:
        print(f"  {label:<42} {project_svhd(s):.3f}")
    print()


if __name__ == "__main__":
    main()
