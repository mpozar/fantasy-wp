"""GitHub Pages deploy guard — detect and recover a wedged Pages deployment.

The last hop of the pipeline had no watcher. `publish` writes `docs/data.json`,
`fast.sh` commits and pushes it, and a GitHub Actions workflow
(`.github/workflows/pages.yml`) deploys `docs/` to Pages. Everything up to the
push is checked (`app validate`, `ANOM_SITE_STALE`), but those checks read the
LOCAL artifact — so when the deploy stops happening, `docs/data.json` stays
perfectly fresh on disk while the published site freezes, and nothing fires.

**2026-08-31: the site served 4-day-old data (published `2026-08-27T16:00:37Z`)
and no flag ever fired.** Workflow run 33091447664, created 16:06:01Z on 08-27,
wedged in status `waiting` on the `github-pages` environment gate with
`wait_timer: 0`, `reviewers: []` and `current_user_can_approve: false` — no timer
to expire and nobody able to approve it, so it waited indefinitely. It held the
workflow's `concurrency: group: "pages"`, so all ~1150 runs behind it collapsed
to `cancelled`. The freeze needed BOTH the wedge and that concurrency group;
cancelling the one run cleared it and the next tick deployed in 18s.

So: detect the *published* site falling behind, and cancel the wedge.

**The one rule that keeps this safe: never cancel an `in_progress` run.** Doing
that is precisely the 2026-07-02 incident recorded in `pages.yml` — GitHub's
Pages backend slowed deploys from ~1 min to 4-10+ min, `cancel-in-progress: true`
killed every deploy before it finished, and the site froze. A slow-but-healthy
deploy is `in_progress`; a run stranded behind a wedge is `waiting`/`queued`.
Acting only on the latter means a slow deploy is never touched, so this guard
cannot recreate the failure it exists to fix.

Staleness is judged on the deployed SHA, not on elapsed time alone
(`_judge_stale`): the cron laptop dark-wake-sleeps, and an idle stretch with
nothing new to deploy must not read as a wedge.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import validate
from app.espn import _read_zshenv_var

REPO_ROOT = Path(__file__).resolve().parent.parent
GH_API = "https://api.github.com"
PAGES_ENVIRONMENT = "github-pages"
HTTP_TIMEOUT = 15.0

# How long the published site may lag an undeployed docs commit before we call it
# wedged. A healthy pipeline deploys every 5 min (fast.sh's cadence), so 30 min is
# six missed ticks — comfortably clear of the 4-10 min deploys seen during the
# 2026-07-02 Pages slowdown, so a merely slow backend never trips it.
DEPLOY_STALE_MIN = 30

# Minimum age before a `waiting`/`queued` run is considered wedged rather than
# briefly enqueued. Same value for the same reason; the wedge that motivated this
# sat for 5,700+ minutes, so there is no need to be aggressive here.
RUN_STUCK_MIN = 30


@dataclass
class GuardResult:
    """What the guard saw and did. `stale` drives the findings; the rest is for
    the log line, which is the only place a healthy tick reports anything."""
    stale: bool = False
    lag_min: float = 0.0
    deployed_sha: str | None = None
    expected_sha: str | None = None
    cancelled: list[int] = field(default_factory=list)
    uncancellable: list[int] = field(default_factory=list)
    note: str = ""


def _age_min(iso: str | None, now: datetime) -> float | None:
    """Minutes between an ISO-8601 instant and `now`, or None if unparseable."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 60.0


# ── pure decision logic (unit-tested without network or git) ─────────────


def _judge_stale(deployed_sha: str | None, deployed_at: str | None,
                 expected_sha: str | None, now: datetime,
                 *, stale_min: float | None = None) -> tuple[bool, float]:
    """(is the published site wedged, how many minutes behind).

    Two conditions, and BOTH are load-bearing:

    - **The deployed SHA is not the newest docs-touching commit.** This is what
      makes the guard immune to idleness: when the laptop sleeps, or docs simply
      do not change, the deployed SHA still equals the newest docs commit and no
      amount of elapsed time reads as a wedge. It also handles the workflow's
      `paths: ["docs/**"]` filter correctly — a code-only commit never triggers a
      deploy, so HEAD is the wrong thing to compare against and the newest
      *docs* commit is the right one.
    - **The last deployment is older than `stale_min`.** Measured from the
      deployment rather than from the commit, because during healthy operation
      the newest docs commit is always seconds old (a deploy is in flight) while
      the previous deployment is ~5 min old. Under a wedge this is the value that
      blows out — it read 4 days on 2026-08-31.

    Unknown deployment state ⇒ not stale: this guard cancels things, so it stays
    quiet rather than acting on a reading it does not have.

    `stale_min` defaults to `DEPLOY_STALE_MIN` resolved AT CALL TIME, not as a
    default argument — a default would bind the constant at import and silently
    ignore anyone retuning it (which is exactly what a threshold is for).
    """
    stale_min = DEPLOY_STALE_MIN if stale_min is None else stale_min
    if not deployed_sha or not expected_sha:
        return False, 0.0
    if deployed_sha == expected_sha:
        return False, 0.0
    lag = _age_min(deployed_at, now)
    if lag is None:
        return False, 0.0
    return lag > stale_min, lag


def _stuck_run_ids(runs: list[dict], now: datetime,
                   *, min_age_min: float | None = None) -> list[int]:
    """Ids of runs wedged long enough to cancel.

    `in_progress` is deliberately absent from the eligible statuses — see the
    module docstring. A run that is actually running is making progress, however
    slowly, and cancelling it is the known way to freeze the site.

    `min_age_min` resolves `RUN_STUCK_MIN` at call time, same reason as above.
    """
    min_age_min = RUN_STUCK_MIN if min_age_min is None else min_age_min
    out: list[int] = []
    for r in runs:
        if r.get("status") not in ("waiting", "queued"):
            continue
        age = _age_min(r.get("created_at"), now)
        if age is not None and age > min_age_min:
            out.append(int(r["id"]))
    return out


def _findings(res: GuardResult) -> list[validate.Finding]:
    """Flags for the run. Two codes rather than one escalating severity, because
    `validate.persist` dedups on (code, matchup_id, flag_date) and does NOT
    update severity on conflict — a single code would be pinned to whichever
    severity happened to land first that day.
    """
    if not res.stale:
        return []
    hours = res.lag_min / 60.0
    where = (f"published site is {hours:.1f}h behind "
             f"(deployed {(res.deployed_sha or '?')[:8]}, "
             f"local docs at {(res.expected_sha or '?')[:8]})")
    if res.cancelled:
        return [validate.Finding(
            code="ANOM_DEPLOY_STALE", severity="warn", matchup_id=None,
            detail=(f"{where}; cancelled {len(res.cancelled)} wedged Pages run(s) "
                    f"{res.cancelled} — the next tick's push should deploy"))]
    extra = (f"; {len(res.uncancellable)} run(s) refused to cancel "
             f"{res.uncancellable}" if res.uncancellable else
             "; no wedged waiting/queued run found to cancel")
    return [validate.Finding(
        code="INV_SITE_NOT_DEPLOYED", severity="error", matchup_id=None,
        detail=(f"{where}{extra}. The guard cannot recover this on its own — "
                f"check the Pages workflow / github-pages environment gate"))]


# ── I/O (thin; everything above is pure) ─────────────────────────────────


def _repo_slug() -> str | None:
    """`owner/name` from the origin remote."""
    try:
        url = subprocess.run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10,
                             check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else None


def _newest_docs_commit() -> str | None:
    """SHA of the newest commit touching `docs/` — what Pages should be serving.

    Not HEAD: `pages.yml` filters on `paths: ["docs/**"]`, so a code-only commit
    never triggers a deploy and would otherwise look permanently undeployed.
    """
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-list", "-1",
                              "HEAD", "--", "docs"],
                             capture_output=True, text=True, timeout=10,
                             check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return out or None


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
    )


def check_and_recover(conn, now_iso: str, *, dry_run: bool = False) -> GuardResult:
    """Detect a wedged Pages deploy and cancel what is holding it up.

    Best-effort by contract: every network failure degrades to "no opinion"
    (`note` explains why, no findings persisted), because this runs on the 5-min
    tick and must never cost a data update. Findings are persisted through
    `validate.persist` so they surface in `app validate --list` alongside every
    other flag — the same reason `check_scrape_health` is raised from `fetch`
    rather than from `validate.run`: only this step knows the deploy state.
    """
    res = GuardResult()
    now = datetime.now(timezone.utc)

    slug = _repo_slug()
    token = _read_zshenv_var("GH_TOKEN")
    if not slug or not token:
        res.note = "no repo slug or GH_TOKEN; skipped"
        return res

    res.expected_sha = _newest_docs_commit()
    if not res.expected_sha:
        res.note = "no docs commit found; skipped"
        return res

    try:
        with _client(token) as c:
            dep = c.get(f"{GH_API}/repos/{slug}/deployments",
                        params={"environment": PAGES_ENVIRONMENT, "per_page": 1})
            dep.raise_for_status()
            rows = dep.json()
            if not rows:
                res.note = "no Pages deployment yet; skipped"
                return res
            res.deployed_sha = rows[0].get("sha")
            res.stale, res.lag_min = _judge_stale(
                res.deployed_sha, rows[0].get("created_at"), res.expected_sha, now)
            if not res.stale:
                res.note = f"healthy (last deploy {res.lag_min:.0f} min ago)"
                return res

            runs = c.get(f"{GH_API}/repos/{slug}/actions/runs", params={"per_page": 50})
            runs.raise_for_status()
            targets = _stuck_run_ids(runs.json().get("workflow_runs", []), now)
            for rid in targets:
                if dry_run:
                    res.cancelled.append(rid)
                    continue
                r = c.post(f"{GH_API}/repos/{slug}/actions/runs/{rid}/cancel")
                # 202 accepted; anything else (the 2026-07-25 orphan returned a
                # 500 "Failed to cancel workflow run") is a run we cannot clear,
                # which is exactly what has to reach a human instead of being
                # retried silently forever.
                (res.cancelled if r.status_code == 202
                 else res.uncancellable).append(rid)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        res.note = f"GitHub API unavailable ({type(e).__name__}); skipped"
        res.stale = False
        return res

    res.note = (f"STALE {res.lag_min / 60:.1f}h — cancelled {res.cancelled}"
                + (f", uncancellable {res.uncancellable}" if res.uncancellable else "")
                + (" (dry run)" if dry_run else ""))
    if not dry_run:
        validate.persist(conn, _findings(res), now_iso)
    return res
