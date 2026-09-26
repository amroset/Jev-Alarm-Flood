"""Load config.yaml and the building asset catalog (spec stage 0)."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

RANK = {"informational": 0, "routine": 1, "review": 2, "urgent": 3, "critical": 4}
RNAME = ["Informational", "Routine", "Needs review", "Urgent", "Critical"]
CHANNEL = ["Morning digest", "Operator queue", "Top of queue for review", "Notified on-shift team", "Paged on-call"]
BMS_DEFAULT = {"High": 3, "Medium": 1, "Low": 0}


@dataclass
class Asset:
    point: str
    label: str
    equip: str
    equip_label: str
    loc: str
    zone: str
    floor: str
    category: str
    criticality: str
    bms: str
    serves: list[str] = field(default_factory=list)
    fed_by: list[str] = field(default_factory=list)
    attributes: str | None = None
    critical_limit: float | None = None
    critical_limit_name: str | None = None
    history: dict = field(default_factory=dict)


@lru_cache
def config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def _fill(value, subs: dict):
    if isinstance(value, str):
        return value.format(**subs)
    if isinstance(value, list):
        return [_fill(v, subs) for v in value]
    return value


@lru_cache
def building() -> dict:
    """Returns {"assets": {point: Asset}, "tickets": [...], "history_incidents": [...]}."""
    raw = yaml.safe_load((BACKEND / "building.yaml").read_text())
    assets: dict[str, Asset] = {}
    for a in raw["assets"]:
        a = dict(a)
        a["floor"] = str(a["floor"])
        assets[a["point"]] = Asset(**a)
    for fam in raw.get("families", []):
        lo, hi = fam["n"]
        for n in range(lo, hi + 1):
            subs = {"n": n, "floor": 1 + n % 4}
            a = {k: _fill(v, subs) for k, v in fam.items() if k != "n"}
            a["floor"] = str(a["floor"])
            assets[a["point"]] = Asset(**a)
    return {
        "assets": assets,
        "tickets": raw.get("tickets", []),
        "history_incidents": raw.get("history_incidents", []),
        "names": raw.get("names", {}),
    }


def asset(point: str) -> Asset:
    return building()["assets"][point]


def floor_for(category: str) -> dict | None:
    return config()["floors"].get(category)


def is_life_safety(a: Asset) -> bool:
    return a.category in config()["life_safety_categories"]


def display_name(key: str) -> str:
    """Human name for an equipment id or zone key."""
    b = building()
    if key in b["names"]:
        return b["names"][key]
    for a in b["assets"].values():
        if a.equip == key:
            return a.equip_label
        if a.zone == key:
            return a.loc
    return key
