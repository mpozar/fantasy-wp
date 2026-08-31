"""The Pages deploy guard (`app/pages.py`) — the pipeline's last hop.

Two real incidents bound this from opposite sides, and the tests encode both:

  * 2026-08-31 — a run wedged in `waiting` held the workflow's `pages`
    concurrency group for 4 days. The published site served 08-27 data while
    `docs/data.json` on disk was current, so every existing check stayed quiet.
    The guard must DETECT that and cancel the wedge.
  * 2026-07-02 — `cancel-in-progress: true` cancelled in-flight deploys while
    GitHub's Pages backend was taking 4-10 min, so no deploy ever completed and
    the site froze. The guard must NEVER cancel an `in_progress` run, or it
    recreates the very failure it exists to fix.

Between them sits the case that makes a naive "site is old" detector useless:
the cron laptop dark-wake-sleeps, so idleness must not read as a wedge.
"""
from datetime import datetime, timedelta, timezone

from app import pages

NOW = datetime(2026, 8, 31, 7, 0, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _run(rid: int, status: str, minutes_ago: float) -> dict:
    return {"id": rid, "status": status, "created_at": _iso(minutes_ago)}


# ── detection ────────────────────────────────────────────────────────────


def test_detects_the_2026_08_31_wedge():
    """The actual incident: deployed SHA days behind, local docs moved on."""
    stale, lag = pages._judge_stale("ba1d4757", _iso(4 * 24 * 60), "3ec3ebfa", NOW)
    assert stale
    assert lag / 60 / 24 == 4          # reported as ~4 days behind


def test_healthy_pipeline_with_a_deploy_in_flight_is_not_stale():
    """The common case: docs committed seconds ago, previous deploy ~5 min old.
    The SHAs differ because a deploy is in flight — that must not read as a wedge
    or the guard would fire twelve times an hour."""
    stale, _ = pages._judge_stale("aaaaaaa1", _iso(5), "aaaaaaa2", NOW)
    assert not stale


def test_an_idle_or_sleeping_laptop_is_not_stale():
    """CLAUDE.md: the cron host dark-wake-sleeps. After an 8-hour sleep the last
    deployment is ancient, but it still matches the newest docs commit — there is
    nothing to deploy, so this is healthy. A detector keyed on elapsed time alone
    would flag every overnight gap and get muted, which is how a real freeze goes
    unnoticed."""
    stale, _ = pages._judge_stale("aaaaaaa1", _iso(8 * 60), "aaaaaaa1", NOW)
    assert not stale


def test_a_code_only_commit_does_not_look_undeployed_forever():
    """`pages.yml` filters on paths: ["docs/**"], so a code commit never triggers
    a deploy. The comparison is against the newest DOCS commit for exactly this
    reason; were it against HEAD, every code push would look permanently
    undeployed once 30 minutes elapsed."""
    # newest docs commit IS deployed; HEAD has moved on with code-only commits
    stale, _ = pages._judge_stale("d0c50001", _iso(45), "d0c50001", NOW)
    assert not stale


def test_unknown_deployment_state_is_never_stale():
    # This guard cancels things; with no reading it must not act.
    assert pages._judge_stale(None, _iso(999), "abc", NOW) == (False, 0.0)
    assert pages._judge_stale("abc", None, "def", NOW) == (False, 0.0)
    assert pages._judge_stale("abc", "not-a-date", "def", NOW) == (False, 0.0)
    assert pages._judge_stale("abc", _iso(999), None, NOW) == (False, 0.0)


def test_staleness_needs_to_clear_the_threshold():
    just_under = pages._judge_stale("a", _iso(pages.DEPLOY_STALE_MIN - 1), "b", NOW)[0]
    just_over = pages._judge_stale("a", _iso(pages.DEPLOY_STALE_MIN + 1), "b", NOW)[0]
    assert not just_under and just_over


# ── the safety rule: never cancel a running deploy ───────────────────────


def test_never_cancels_an_in_progress_run():
    """The 2026-07-02 regression guard. An in-progress deploy that has been
    running for hours is still not a cancel target: cancelling in-flight deploys
    is what froze the site then. Only `waiting`/`queued` are eligible."""
    runs = [_run(1, "in_progress", 10),
            _run(2, "in_progress", 600),        # pathologically slow, still running
            _run(3, "waiting", 600)]
    assert pages._stuck_run_ids(runs, NOW) == [3]


def test_leaves_briefly_queued_runs_alone():
    """A run queued for a couple of minutes behind a healthy deploy is normal
    (GitHub serialises Pages deployments); cancelling it would fight the
    workflow's own concurrency group."""
    runs = [_run(1, "queued", 2), _run(2, "waiting", 3)]
    assert pages._stuck_run_ids(runs, NOW) == []


def test_cancels_both_wedged_statuses():
    """08-31 had one of each: id 33091447664 `waiting` (the blocker) and
    id 30157820421 `queued` since 07-25."""
    runs = [_run(33091447664, "waiting", 4 * 24 * 60),
            _run(30157820421, "queued", 37 * 24 * 60),
            _run(3, "completed", 5)]
    assert pages._stuck_run_ids(runs, NOW) == [33091447664, 30157820421]


def test_completed_runs_are_never_targets():
    runs = [_run(1, "completed", 9999), _run(2, "cancelled", 9999)]
    assert pages._stuck_run_ids(runs, NOW) == []


# ── findings ─────────────────────────────────────────────────────────────


def test_no_findings_when_healthy():
    assert pages._findings(pages.GuardResult(stale=False)) == []


def test_a_recovered_wedge_warns():
    res = pages.GuardResult(stale=True, lag_min=4 * 24 * 60, deployed_sha="ba1d4757c0",
                            expected_sha="3ec3ebfa80", cancelled=[33091447664])
    f, = pages._findings(res)
    assert f.code == "ANOM_DEPLOY_STALE"
    assert f.severity == "warn"           # we fixed it; next tick deploys
    assert "96.0h behind" in f.detail
    assert "33091447664" in f.detail


def test_an_unrecoverable_wedge_is_an_ERROR_not_a_warning():
    """The case that must reach a human: on 08-31 the 07-25 orphan returned
    HTTP 500 "Failed to cancel workflow run". A guard that keeps silently
    retrying an uncancellable run is just a quieter version of the outage."""
    res = pages.GuardResult(stale=True, lag_min=600, deployed_sha="aaaaaaaaaa",
                            expected_sha="bbbbbbbbbb", uncancellable=[30157820421])
    f, = pages._findings(res)
    assert f.code == "INV_SITE_NOT_DEPLOYED"
    assert f.severity == "error"
    assert "refused to cancel" in f.detail


def test_stale_with_nothing_to_cancel_is_also_an_error():
    """Stale but no wedged run means the cause is something this guard does not
    model (workflow disabled, Pages misconfigured, token rotated). Report it
    rather than implying it was handled."""
    res = pages.GuardResult(stale=True, lag_min=200, deployed_sha="a" * 10,
                            expected_sha="b" * 10)
    f, = pages._findings(res)
    assert f.code == "INV_SITE_NOT_DEPLOYED"
    assert f.severity == "error"
    assert "no wedged waiting/queued run found" in f.detail


def test_thresholds_stay_clear_of_a_merely_slow_backend():
    """Both constants must stay well above the 4-10 min deploys observed during
    the 2026-07-02 Pages slowdown, or the guard starts cancelling healthy work."""
    assert pages.DEPLOY_STALE_MIN >= 15
    assert pages.RUN_STUCK_MIN >= 15
