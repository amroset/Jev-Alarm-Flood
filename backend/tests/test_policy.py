"""Safety properties of the deterministic policy (spec section 3)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog import RANK, asset  # noqa: E402
from jev_client import JevAnswer  # noqa: E402
from policy import decide  # noqa: E402

AHU = asset("AHU-1.SAT")      # BMS Medium -> routine, no floor
CHILLER = asset("CH-2.CWF")   # BMS High -> urgent, no floor
LEAK = asset("WL-B2")         # floor: urgent
LIGHT = asset("LC-S.COMM")    # BMS Low -> informational


def jev(severity="routine", conf=0.95, team="HVAC", team_conf=0.95, p_history=0.9,
        same=None, same_p=None, offered=True):
    return JevAnswer(severity=severity, sev_conf=conf, sev_probs={}, team=team, team_conf=team_conf,
                     p_history=p_history, same_target=same, same_p=same_p, same_offered=offered,
                     p_wait=0.5, impact=2, impact_conf=0.8)


def test_fallback_keeps_bms_priority():
    r = decide(CHILLER, None, set(), {})
    assert r.rank == RANK["urgent"] and r.team == "Facilities desk"


def test_downgrade_needs_high_confidence_and_history_match():
    assert decide(CHILLER, jev("informational", conf=0.89), set(), {}).rank == RANK["urgent"]
    assert decide(CHILLER, jev("informational", conf=0.95, p_history=0.80), set(), {}).rank == RANK["urgent"]
    assert decide(CHILLER, jev("informational", conf=0.95, p_history=0.90), set(), {}).rank == RANK["informational"]


def test_uncertainty_goes_up_to_review():
    assert decide(LIGHT, jev("informational", conf=0.4), set(), {}).rank == RANK["review"]


def test_uncertain_high_answer_is_not_lowered():
    assert decide(LIGHT, jev("urgent", conf=0.2), set(), {}).rank == RANK["urgent"]


def test_floor_cannot_be_lowered_even_by_a_confident_answer():
    r = decide(LEAK, jev("informational", conf=0.99, p_history=0.99), set(), {})
    assert r.rank == RANK["urgent"]


def test_injected_mistake_is_contained():
    r = decide(LEAK, jev("routine", conf=0.72, p_history=0.68), set(), {})
    assert r.rank == RANK["urgent"]


def test_reevaluation_never_undoes_escalation():
    r = decide(CHILLER, jev("routine", conf=0.99, p_history=0.99), set(), {}, current_rank=RANK["critical"], escalated=True)
    assert r.rank == RANK["critical"]


def test_team_needs_confidence():
    assert decide(AHU, jev(team_conf=0.5), set(), {}).team == "Facilities desk"


def test_grouping_needs_probability_and_a_real_candidate():
    assert decide(AHU, jev(same="X", same_p=0.85), {"X"}, {}).parent == "X"
    assert decide(AHU, jev(same="X", same_p=0.75), {"X"}, {}).parent is None
    assert decide(AHU, jev(same="Y", same_p=0.99), {"X"}, {}).parent is None


def test_floor_alarms_are_linked_not_grouped():
    r = decide(LEAK, jev("urgent", same="X", same_p=0.95), {"X"}, {})
    assert r.parent is None and r.link == "X"


def test_alarm_outliving_its_cleared_cause_is_pulled_out():
    r = decide(AHU, jev(same="X", same_p=0.95, p_history=0.2), {"X"}, {}, cleared_ids={"X"}, outlived_history=True)
    assert r.parent is None and r.link == "X"


def test_floor_alarm_is_never_hidden_inside_a_group():
    import asyncio
    from db import connect
    from jev_client import JevClient
    from scheduler import Engine
    eng = Engine(connect(":memory:"), JevClient())
    eng.mode = "replay"
    eng.load("server")
    asyncio.run(eng.advance(eng.sc.duration))
    snap = eng.snapshot()
    for p, a in snap["alarms"].items():
        if a["def"]["floor"] is not None:
            assert a["parent"] is None, p
