"""Scenario scripts: timed alarm / clear events and trend data on a simulated clock (spec section 8)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

import yaml

from catalog import BACKEND

SCENARIO_ORDER = ["startup", "power", "server", "leak"]


@dataclass
class Event:
    t: float
    kind: str  # "alarm" | "clear"
    point: str
    value: float | None = None
    limit: float | None = None
    unit: str | None = None
    measure: str | None = None
    direction: str = "high"


@dataclass
class Scenario:
    id: str
    name: str
    short: str
    start: datetime
    duration: float
    desc: str
    weather: str | None
    events: list[Event]
    trends: dict = field(default_factory=dict)
    mistake: dict | None = None
    labels: dict = field(default_factory=dict)

    def at(self, t: float) -> datetime:
        return self.start + timedelta(seconds=t)


def _expr(e, n):
    return float(eval(str(e), {"__builtins__": {}}, {"n": n}))


def _load(path) -> Scenario:
    raw = yaml.safe_load(path.read_text())
    events: list[Event] = []
    for e in raw.get("events", []):
        e = dict(e)
        if "alarm" in e:
            events.append(Event(kind="alarm", point=e.pop("alarm"), **e))
        else:
            events.append(Event(t=e["t"], kind="clear", point=e["clear"]))
    for fam in raw.get("families", []):
        lo, hi = fam["n"]
        for n in range(lo, hi + 1):
            point = fam["alarm"].format(n=n)
            events.append(Event(t=_expr(fam["t"], n), kind="alarm", point=point))
            if n not in fam.get("no_clear", []) and "clear" in fam:
                events.append(Event(t=_expr(fam["clear"], n), kind="clear", point=point))
    # Clears sort after alarms at the same instant.
    events.sort(key=lambda e: (e.t, e.kind != "alarm"))
    return Scenario(
        id=raw["id"], name=raw["name"], short=raw["short"],
        start=datetime.fromisoformat(raw["start"]), duration=float(raw["duration_s"]),
        desc=" ".join(raw["desc"].split()), weather=raw.get("weather"), events=events,
        trends=raw.get("trends") or {}, mistake=raw.get("mistake"), labels=raw.get("labels") or {},
    )


@lru_cache
def scenarios() -> dict[str, Scenario]:
    found = {s.id: s for s in (_load(p) for p in sorted((BACKEND / "scenarios").glob("*.yaml")))}
    return {k: found[k] for k in SCENARIO_ORDER if k in found} | found


def trend_samples(sc: Scenario):
    """Yield (point, timestamp, value) by linear interpolation between the scripted trend points."""
    for point, spec in sc.trends.items():
        pts = sorted(spec["points"])
        step = spec["step_s"]
        t = pts[0][0]
        while t <= pts[-1][0]:
            for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
                if t0 <= t <= t1:
                    v = v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
                    break
            yield point, sc.at(t), round(v, 2)
            t += step
