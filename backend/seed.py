"""Synthetic alarm history, trends and tickets, generated relative to the scenario start.

History profiles (building.yaml `history`):
  startup:   {hits, of, dur_min}  alarms shortly after the 06:00 HVAC start on `hits` of the last `of` weekdays
                                   (same rate on earlier weekdays), always auto-cleared, longest = dur_min[1]
  recurring: {per_30d, dur_min, hours?, weekdays_only?, ticket_share?}  spread over the last 90 days
  rare:      {days_ago: [...], dur_min, resolution?}
  none:      no history
"""
from __future__ import annotations

import fnmatch
import random
import zlib
from datetime import datetime, time, timedelta

from catalog import building
from db import ts
from simulator import Scenario, trend_samples

DAYS_BACK = 365


def _rng(key: str) -> random.Random:
    return random.Random(zlib.crc32(key.encode()))


def _events_for(asset, start: datetime):
    h = asset.history or {}
    pat = h.get("pattern", "none")
    rng = _rng(asset.point)
    lo, hi = h.get("dur_min", [1, 5])
    out = []  # (started, minutes, resolution)
    if pat == "startup":
        weekdays = [start.date() - timedelta(days=d) for d in range(1, 91)]
        weekdays = [d for d in weekdays if d.weekday() < 5]
        recent, older = weekdays[: h["of"]], weekdays[h["of"]:]
        chosen = rng.sample(recent, h["hits"]) + [d for d in older if rng.random() < h["hits"] / h["of"]]
        for i, d in enumerate(sorted(chosen, reverse=True)):
            s = datetime.combine(d, time(6, 0)) + timedelta(seconds=rng.randint(5, 60))
            out.append((s, hi if i == 1 else rng.uniform(lo, hi), "auto_cleared"))
    elif pat == "recurring":
        h0, h1 = h.get("hours", [0, 24])
        n = h["per_30d"] * 3
        while len(out) < n:
            d = start - timedelta(days=rng.uniform(0.5, 90))
            if h.get("weekdays_only") and d.weekday() >= 5:
                continue
            s = datetime.combine(d.date(), time()) + timedelta(hours=rng.uniform(h0, h1))
            res = "ticket" if rng.random() < h.get("ticket_share", 0) else "auto_cleared"
            out.append((s, rng.uniform(lo, hi), res))
    elif pat == "rare":
        for days in h["days_ago"]:
            s = datetime.combine((start - timedelta(days=days)).date(), time()) + timedelta(hours=rng.uniform(8, 17))
            out.append((s, rng.uniform(lo, hi), h.get("resolution", "auto_cleared")))
    return out


def seed(conn, sc: Scenario) -> None:
    b = building()
    conn.execute("DELETE FROM alarm_events")
    conn.execute("DELETE FROM trend_samples")
    conn.execute("DELETE FROM tickets")
    rows = []
    for a in b["assets"].values():
        for s, minutes, res in _events_for(a, sc.start):
            rows.append((a.point, ts(s), ts(s + timedelta(minutes=minutes)), a.bms, res))
    for inc in b["history_incidents"]:
        day = (sc.start - timedelta(days=inc["days_ago"])).date()
        hh, mm = map(int, inc["at"].split(":"))
        for a in b["assets"].values():
            if any(fnmatch.fnmatchcase(a.point, p) for p in inc["points"]):
                rng = _rng(a.point + inc["name"])
                s = datetime.combine(day, time(hh, mm)) + timedelta(seconds=rng.randint(0, 50))
                rows.append((a.point, ts(s), ts(s + timedelta(minutes=rng.uniform(*inc["dur_min"]))), a.bms, "auto_cleared"))
    conn.executemany(
        "INSERT INTO alarm_events(point_id, started_at, cleared_at, bms_priority, resolution) VALUES(?,?,?,?,?)", rows)
    for t in b["tickets"]:
        opened = sc.start - timedelta(days=t["opened_days_ago"])
        closed = None if t.get("closed_days_ago") is None else ts(sc.start - timedelta(days=t["closed_days_ago"]))
        conn.execute("INSERT INTO tickets(equipment, opened_at, closed_at, summary) VALUES(?,?,?,?)",
                     (t["equip"], ts(opened), closed, t["summary"]))
    conn.executemany("INSERT INTO trend_samples VALUES(?,?,?)",
                     [(p, ts(d), v) for p, d, v in trend_samples(sc)])
    conn.commit()
