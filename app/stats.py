"""ESPN stat-ID → human name + direction + display grouping/order."""

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

# ESPN scoreboard column order
BATTING_STAT_IDS = [1, 20, 5, 23, 18]      # H, R, HR, SB, OPS
PITCHING_STAT_IDS = [48, 63, 47, 41, 83]   # K, QS, ERA, WHIP, SVHD


def name(stat_id: int) -> str:
    return STAT_NAMES.get(stat_id, f"stat_{stat_id}")


def is_reversed(stat_id: int) -> bool:
    return stat_id in REVERSED_STATS


def group(stat_id: int) -> str:
    if stat_id in BATTING_STAT_IDS:
        return "batting"
    if stat_id in PITCHING_STAT_IDS:
        return "pitching"
    return "other"
