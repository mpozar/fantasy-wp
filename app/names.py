"""Player-name normalization shared across the ESPN/MLB name-matching paths.

There's no ESPN↔MLBAM player-id crosswalk, so names from different feeds (MLB
box scores / probables / injuries vs ESPN rosters) are matched by a normalized
key. Every caller MUST use this one function so the write-key and read-key can't
diverge — a past bug stored injury keys with one normalizer and looked them up
with a fuller one, silently dropping IL return dates for names carrying a middle
initial or suffix (e.g. "José A. Ferrer" ⇄ "Jose Ferrer").
"""

from __future__ import annotations

import unicodedata

# Generational suffixes dropped during name matching (one source carries them, the
# other often doesn't): "Daniel Lynch IV" ⇄ "Daniel Lynch".
_NAME_SUFFIXES = {"jr", "jnr", "sr", "snr", "ii", "iii", "iv", "v"}


def norm_name(s: str | None) -> str:
    """Normalize a player name to a match key. There's no ESPN↔MLBAM player-id
    crosswalk, so box-score lines are attributed to fantasy slots by this key — it
    must collapse the spelling differences between the two feeds:

      - **Diacritics:** MLB uses accents ("Cristopher Sánchez"), ESPN often doesn't.
      - **Middle initials:** MLB may carry one the roster omits — "José A. Ferrer"
        ⇄ "Jose Ferrer" (2026-06-09: this exact mismatch left Ferrer's relief line
        unmatched, so the live ERA/WHIP reconstruction came up short of the scrape
        and the rate guard rejected the whole side until the daily settle).
      - **Suffixes:** "Daniel Lynch IV" ⇄ "Daniel Lynch".

    The transform is applied to *both* sides, so it only ever adds correct matches;
    it leaves first + last name intact (drops only single-letter *middle* tokens and
    suffixes), so distinct players don't collapse together."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    toks = ["".join(c for c in t if c.isalnum()) for t in s.lower().split()]
    toks = [t for t in toks if t and t not in _NAME_SUFFIXES]
    # Drop single-letter *middle* tokens (middle initials); keep first + last so two
    # players sharing a surname (e.g. "J.D. Martinez" vs "Nick Martinez") stay distinct.
    if len(toks) > 2:
        toks = [toks[0]] + [t for t in toks[1:-1] if len(t) > 1] + [toks[-1]]
    return "".join(toks)
