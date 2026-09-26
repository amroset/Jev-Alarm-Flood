"""State writer (spec stage 3): short, neutral state built only from facts.

Jev receives the structured state (named sections, referenced from questions as `alarm`,
`history_of_this_point`, `other_alarms_last_30_min`). Operators see the same content as lines.
"""
from __future__ import annotations

from facts import Facts

SECTION_TITLES = {
    "alarm": "Alarm",
    "history_of_this_point": "History of this point",
    "open_tickets_for_this_equipment": "Open tickets for this equipment",
    "building_context": "Building context",
    "other_alarms_last_30_min": "Other alarms and events in the last 30 minutes",
}


def write(facts: Facts) -> dict:
    return facts.state


def _human(key: str) -> str:
    return key.replace("_", " ").capitalize()


def display_lines(state: dict) -> list[dict]:
    """[{title, items: [str]}] for the operator UI."""
    out = []
    for key, value in state.items():
        title = SECTION_TITLES.get(key, _human(key))
        if isinstance(value, dict):
            items = [f"{_human(k)}: {v}" for k, v in value.items()]
        elif isinstance(value, list):
            items = []
            for v in value:
                if isinstance(v, dict):
                    what = v.get("alarm") or v.get("event")
                    extra = [v.get("location"), v.get("relation_to_this_equipment"), f"started {v.get('started')}", v.get("status")]
                    items.append(f"{what} ({'; '.join(x for x in extra if x)})")
                else:
                    items.append(str(v))
        else:
            items = [str(value)]
        out.append({"title": title, "items": items})
    return out
