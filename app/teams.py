"""MLB team-ID translation between ESPN (proTeamId) and MLB statsapi (MLBAM).

ESPN's `proTeamId` is what players are tagged with; MLB's statsapi uses MLBAM
team IDs. Both APIs are stable enough for a hardcoded map.
"""

# ESPN proTeamId  ->  MLBAM team_id
ESPN_TO_MLBAM = {
    1: 110, 2: 111, 3: 108, 4: 145, 5: 114, 6: 116, 7: 118, 8: 158,
    9: 142, 10: 147, 11: 133, 12: 136, 13: 140, 14: 141, 15: 144,
    16: 112, 17: 113, 18: 117, 19: 119, 20: 120, 21: 121, 22: 143,
    23: 134, 24: 138, 25: 135, 26: 137, 27: 115, 28: 146, 29: 109, 30: 139,
}

MLBAM_TO_ESPN = {v: k for k, v in ESPN_TO_MLBAM.items()}
