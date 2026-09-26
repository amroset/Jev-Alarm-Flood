"""Deterministic policy (spec stage 5): turns Jev's judgments into a rank, a team and grouping.

Explanations are assembled here from facts and answers; Jev produces no text.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from catalog import BMS_DEFAULT, RANK, RNAME, Asset, config, floor_for
from jev_client import JevAnswer


@dataclass
class PolicyResult:
    rank: int
    team: str
    parent: str | None = None   # grouped under this alarm or incident
    link: str | None = None     # shown on its own with a "probably caused by" link
    steps: list[dict] = field(default_factory=list)


def _p(x: float) -> str:
    return f"{x:.2f}"


def decide(a: Asset, jev: JevAnswer | None, candidate_ids: set[str], titles: dict[str, str],
           current_rank: int | None = None, escalated: bool = False,
           cleared_ids: set[str] = frozenset(), outlived_history: bool = False) -> PolicyResult:
    """cleared_ids: candidates that have returned to normal. outlived_history: computed fact that this
    alarm is already longer than every past occurrence of its point."""
    th = config()["thresholds"]
    fallback_team = config()["fallback_team"]
    steps: list[dict] = []
    add = lambda t, stop=False: steps.append({"t": t, "stop": stop})
    dflt = BMS_DEFAULT[a.bms]
    parent = link = None

    if jev is None:
        r = dflt
        team = fallback_team
        add(f"Jev did not answer, so the building system's own priority is kept: {a.bms} ({RNAME[dflt]}). "
            "This is today's behavior.", True)
    else:
        r = RANK[jev.severity]
        add(f"Jev's severity: {RNAME[r]} (confidence {_p(jev.sev_conf)}).")
        if r < dflt:
            if jev.sev_conf >= th["downgrade_min_severity_conf"] and jev.p_history >= th["downgrade_min_p_history"]:
                add(f"Lower than the building system's default ({RNAME[dflt]}). Confidence ≥ "
                    f"{th['downgrade_min_severity_conf']:.2f} and history match ≥ {th['downgrade_min_p_history']:.2f} "
                    f"({_p(jev.p_history)}), so the downgrade is allowed.")
            else:
                add(f"Lower than the building system's default ({RNAME[dflt]}). A downgrade needs confidence ≥ "
                    f"{th['downgrade_min_severity_conf']:.2f} and history match ≥ {th['downgrade_min_p_history']:.2f}; "
                    f"got {_p(jev.sev_conf)} and {_p(jev.p_history)}, so it stays {RNAME[dflt]}.", True)
                r = dflt
        else:
            add(f"At or above the building system's default ({RNAME[dflt]}), so no downgrade check is needed.")
        if jev.sev_conf < th["review_below_conf"] and r < RANK["review"]:
            r = RANK["review"]
            add(f"Confidence below {th['review_below_conf']:.2f}: uncertainty goes up, so a person reviews it.", True)
        if jev.team_conf >= th["team_min_conf"]:
            team = jev.team
            add(f"Routed to {jev.team} (confidence {_p(jev.team_conf)}).")
        else:
            team = fallback_team
            add(f"Team confidence {_p(jev.team_conf)} is below {th['team_min_conf']:.2f}, so routed to the {fallback_team.lower()}.")

    floor = floor_for(a.category)
    if floor:
        if r < floor["rank"]:
            add(f"Floor for {floor['name']}: {RNAME[floor['rank']]}. Raised to the floor.", True)
            r = floor["rank"]
        else:
            add(f"Floor for {floor['name']} ({RNAME[floor['rank']]}) is satisfied.")

    if escalated and current_rank is not None and r < current_rank:
        add(f"Already escalated by the timer; a re-evaluation can raise the priority but never undo an escalation. "
            f"Stays {RNAME[current_rank]}.", True)
        r = current_rank

    if jev is not None and jev.same_offered:
        if jev.same_target is None:
            add(f"Jev linked it to none of the other alarms ({_p(jev.same_p or 0)}), so it is shown on its own.")
        else:
            title = titles.get(jev.same_target, jev.same_target)
            if jev.same_p >= th["group_min_p"] and jev.same_target in candidate_ids:
                outlives = jev.same_target in cleared_ids and outlived_history and jev.p_history < 0.5
                if outlives:
                    link = jev.same_target
                    add(f"Likely the same cause as “{title}” ({_p(jev.same_p)}), but that alarm has cleared while this one "
                        f"is still active, longer than ever before and not matching its history ({_p(jev.p_history)} < 0.50). "
                        "Pulled out and shown on its own, with a link.", True)
                elif floor:
                    link = jev.same_target
                    add(f"Likely the same cause as “{title}” ({_p(jev.same_p)}), but alarms with a floor always stay "
                        "visible on their own. Shown separately, with a link.")
                else:
                    parent = jev.same_target
                    add(f"Same cause as “{title}” ({_p(jev.same_p)}): grouped with it instead of shown separately.")
            else:
                add(f"Link to “{title}” is too uncertain ({_p(jev.same_p)} < {th['group_min_p']:.2f}), "
                    "so it is shown on its own.")
    return PolicyResult(rank=r, team=team, parent=parent, link=link, steps=steps)
