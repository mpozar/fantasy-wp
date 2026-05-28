"""ESPN stat-ID → human name + direction.

Only entries we actually use are listed; full mapping is well-documented in
the espn-api project if more are needed later.
"""

STAT_NAMES = {
    1: "H",
    5: "HR",
    18: "OPS",
    20: "R",
    23: "SB",
    34: "OUTS",
    41: "WHIP",
    47: "ERA",
    48: "K",
    63: "QS",
    83: "SVHD",
}

REVERSED_STATS = {41, 47}


def name(stat_id: int) -> str:
    return STAT_NAMES.get(stat_id, f"stat_{stat_id}")


def is_reversed(stat_id: int) -> bool:
    return stat_id in REVERSED_STATS
