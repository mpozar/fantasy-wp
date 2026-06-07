"""acquire_lock (scripts/_common.sh) — stale / wedged lock stealing.

Regression guard for the 2026-06-07 freeze: a tick that dies or hangs without
releasing the lock must not freeze the whole pipeline. acquire_lock must steal
from a dead holder, deny a live recent holder, and kill+steal a holder wedged
past MAX_LOCK_AGE. The lock logic is shell, so we drive it via bash.
"""
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent


def _run(body: str) -> str:
    script = (f'source "{REPO}/scripts/_common.sh"\n'
              'set +e\n'                       # override _common.sh `set -e` for the test
              'LOCKFILE="$(mktemp /tmp/applock.XXXXXX)"; rm -f "$LOCKFILE"\n'
              + body)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.stdout


def test_acquires_when_free():
    assert "RC=0" in _run('acquire_lock; echo "RC=$?"')


def test_steals_from_dead_holder():
    assert "RC=0" in _run('echo 999999 > "$LOCKFILE"; acquire_lock; echo "RC=$?"')


def test_denies_live_recent_holder():
    out = _run('sleep 20 & H=$!; echo $H > "$LOCKFILE"; '
               'acquire_lock; echo "RC=$?"; kill $H 2>/dev/null')
    assert "RC=1" in out


def test_steals_and_kills_wedged_holder():
    out = _run('MAX_LOCK_AGE=1; sleep 20 & H=$!; echo $H > "$LOCKFILE"; '
               'sleep 2; acquire_lock; echo "RC=$?"; sleep 0.3; '
               'kill -0 $H 2>/dev/null && echo HOLDER_ALIVE || echo HOLDER_DEAD; '
               'kill $H 2>/dev/null')
    assert "RC=0" in out            # stole the wedged lock
    assert "HOLDER_DEAD" in out     # and killed the wedged holder
