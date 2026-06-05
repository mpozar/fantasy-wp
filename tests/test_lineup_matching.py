"""Optimal lineup-slot assignment (sim._max_slot_assignment).

Regression guard for the greedy-first-fit bug: a flexible bat spent on an early
slot could waste a scarce slot only it could fill, benching a constrained hitter
(the 2026-06-05 Dubon/Keaschall case — Dubon eligible 2B+3B taken at 2B, leaving
3B empty and the 2B-only Keaschall on the bench).
"""

from app.sim import _max_slot_assignment


def _cand(pid, eligible, impact):
    return {"player_id": pid, "factor": 1.0, "eligible": set(eligible), "impact": impact}


def test_scarce_slot_seats_both_via_rerouting():
    # Dubon (flexible 2B/3B, higher impact) + Keaschall (2B only). Slots: 2B, 3B.
    # Greedy seats only Dubon (grabs 2B) and wastes 3B; matching reroutes Dubon→3B.
    cands = [_cand("dubon", {2, 3}, 5.0), _cand("keaschall", {2}, 3.0)]  # impact-sorted
    seated = _max_slot_assignment(cands, slot_instances=[2, 3])
    assert seated == {0, 1}  # both play


def test_capacity_bound_seats_highest_impact_subset():
    # 3 hitters, 2 slots → the two highest-impact (A, B) seat; C benched.
    cands = [_cand("A", {2}, 9.0), _cand("B", {2, 12}, 5.0), _cand("C", {12}, 1.0)]
    seated = _max_slot_assignment(cands, slot_instances=[2, 12])
    assert seated == {0, 1}


def test_multi_instance_slot():
    # Two UTIL instances, three UTIL-only hitters → top two seat.
    cands = [_cand("X", {12}, 9.0), _cand("Y", {12}, 5.0), _cand("Z", {12}, 1.0)]
    seated = _max_slot_assignment(cands, slot_instances=[12, 12])
    assert seated == {0, 1}


def test_all_seat_when_slots_spare():
    cands = [_cand("A", {2}, 9.0), _cand("B", {3}, 5.0)]
    seated = _max_slot_assignment(cands, slot_instances=[2, 3, 12])
    assert seated == {0, 1}


def test_ineligible_hitter_not_seated():
    # A hitter eligible only for an unavailable slot stays out.
    cands = [_cand("A", {2}, 9.0), _cand("B", {99}, 5.0)]
    seated = _max_slot_assignment(cands, slot_instances=[2])
    assert seated == {0}
