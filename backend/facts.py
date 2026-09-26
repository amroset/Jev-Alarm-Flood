"""Fact builder (spec stage 2). Computes measured or looked-up facts only; never conclusions.

All arithmetic, time math and counting happens here, because Jev is weak at them.
The result is a structured state with named sections, so questions can point at `alarm`
and Jev does not confuse the alarm being judged with the other alarms listed for comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from catalog import Asset, config, display_name, floor_for
from db import ts

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SHORT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} min" + (f" {r} s" if r and m < 10 else "")
    if s < 86400 * 2:
        h, r = divmod(s // 60, 60)
        return f"{h} h" + (f" {r} min" if r else "")
    return f"{s // 86400} days"


def fmt_num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:.0f}"


@dataclass
class Candidate:
    id: str
    label: str
    entry: dict     # neutral facts shown to Jev
    score: float    # relevance, used only for capping the list
    first_t: float


@dataclass
class Facts:
    state: dict
    candidates: list[Candidate]
    floor: dict | None
    history_max_min: float | None = None
    distance_past_limit: float | None = None
    slope_per_min: float | None = None
    raw: dict = field(default_factory=dict)


def _reading(ev) -> tuple[str | None, float | None]:
    if ev.value is None or ev.limit is None:
        return None, None
    past = (ev.value - ev.limit) if ev.direction == "high" else (ev.limit - ev.value)
    word = "above" if ev.direction == "high" else "below"
    u = f" {ev.unit}" if ev.unit else ""
    return (f"{ev.measure} {fmt_num(ev.value)}{u} (limit {fmt_num(ev.limit)}{u}; "
            f"{fmt_num(past)}{u} {word} the limit)"), past


def _trend(conn, point: str, spec: dict, now: datetime):
    window = spec["window_min"]
    rows = conn.execute(
        "SELECT ts, value FROM trend_samples WHERE point_id=? AND ts<=? AND ts>=? ORDER BY ts",
        (point, ts(now), ts(now - timedelta(minutes=window)))).fetchall()
    if len(rows) < 3:
        return None, None
    xs = [(datetime.fromisoformat(r["ts"]) - now).total_seconds() / 60 for r in rows]
    ys = [r["value"] for r in rows]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / max(1e-9, sum((x - mx) ** 2 for x in xs))
    unit = spec.get("unit", "")
    change = slope * (xs[-1] - xs[0])
    per, scale = ("day", 1440) if window >= 1440 else ("min", 1)
    span = fmt_dur(window * 60)
    if abs(change) < max(0.15, 0.01 * abs(my)):
        text = f"steady around {fmt_num(my)} {unit} over the last {span}"
    else:
        rate = abs(slope) * scale
        text = f"{'rising' if slope > 0 else 'falling'} {fmt_num(rate) if rate >= 0.1 else f'{rate:.2f}'} {unit}/{per} over the last {span}"
    text += f"; now {fmt_num(ys[-1])} {unit}"
    hour_ago = conn.execute(
        "SELECT value FROM trend_samples WHERE point_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (point, ts(now - timedelta(hours=1)))).fetchone()
    if hour_ago and window < 60:
        text += f"; 1 h ago {fmt_num(hour_ago['value'])} {unit}"
    return text, slope


def _history(conn, point: str, now: datetime, before: datetime) -> tuple[dict | str, float | None, dict]:
    rows = conn.execute(
        "SELECT started_at, cleared_at, resolution FROM alarm_events WHERE point_id=? AND started_at<? "
        "AND started_at>? ORDER BY started_at DESC",
        (point, ts(before), ts(now - timedelta(days=365)))).fetchall()
    starts = [datetime.fromisoformat(r["started_at"]) for r in rows]
    def count(days): return sum(1 for s in starts if s > now - timedelta(days=days))
    raw = {"c24h": count(1), "c30d": count(30), "c90d": count(90), "c365d": len(starts)}
    if not starts:
        return "no alarm in the last 12 months", None, raw
    h = {"alarms_last_24_h": raw["c24h"], "alarms_last_30_days": raw["c30d"],
         "alarms_last_90_days": raw["c90d"], "alarms_last_12_months": raw["c365d"]}
    durs = [(datetime.fromisoformat(r["cleared_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            for r in rows if r["cleared_at"]]
    max_min = max(durs) / 60 if durs else None
    raw["durations"] = durs
    if durs:
        h["typical_duration"] = fmt_dur(sum(durs) / len(durs))
        h["longest_duration"] = fmt_dur(max(durs))
        h["cleared_by_themselves"] = f"{sum(1 for r in rows if r['resolution'] == 'auto_cleared')} of {len(rows)}"
        h["led_to_maintenance_ticket"] = f"{sum(1 for r in rows if r['resolution'] == 'ticket')} of {len(rows)}"
    h["last_previous_alarm"] = f"{fmt_dur((now - starts[0]).total_seconds())} ago"
    if now.weekday() < 5 and raw["c90d"]:
        tod = now.hour * 60 + now.minute
        days = [now.date() - timedelta(days=d) for d in range(1, 40)]
        days = [d for d in days if d.weekday() < 5][:20]
        hit = {s.date() for s in starts if abs(s.hour * 60 + s.minute - tod) <= 30}
        n = sum(1 for d in days if d in hit)
        h["alarmed_within_30_min_of_this_time_of_day"] = f"on {n} of the last 20 weekdays"
        raw["same_time_hits"] = n
    return h, max_min, raw


def _occupancy(now: datetime) -> str:
    occ = config()["schedule"]["occupied"]
    day = SHORT_DAYS[now.weekday()]
    t = now.time()
    open_t, close_t = time.fromisoformat(occ["from"]), time.fromisoformat(occ["to"])
    stamp = f"{WEEKDAYS[now.weekday()]} {now:%H:%M}"
    if day in occ["days"] and open_t <= t < close_t:
        return f"occupied ({stamp})"
    if day in occ["days"] and t < open_t:
        return f"unoccupied, building opens at {occ['from']} ({stamp})"
    return f"unoccupied ({stamp})"


def schedule_events(now: datetime) -> list[tuple[str, str, datetime]]:
    """Schedule events that ran in the recent window: (id, title, time)."""
    out = []
    win = timedelta(minutes=config()["timing"]["schedule_window_min"])
    for key, s in config()["schedule"].items():
        if "at" not in s:
            continue
        at = datetime.combine(now.date(), time.fromisoformat(s["at"]))
        if SHORT_DAYS[at.weekday()] in s["days"] and at <= now <= at + win:
            out.append((f"SCHED-{key}-{at:%Y%m%d}", f"{s['title']} (schedule, {s['at']})", at))
    return out


def relation(a: Asset, o: Asset) -> tuple[float, str | None]:
    """How candidate `o` relates to the current alarm's asset `a`, from the catalog only."""
    if o.equip == a.equip:
        return 3, "same equipment"
    if o.equip in a.fed_by:
        return 3, f"supplies {a.equip_label}"
    if a.equip in o.fed_by:
        return 3, f"is supplied by {a.equip_label}"
    if a.zone in o.serves or a.equip in o.serves:
        return 3, f"serves {display_name(a.zone)}"
    if o.zone in a.serves or o.equip in a.serves:
        return 3, f"is served by {a.equip_label}"
    if o.zone == a.zone:
        return 2, "same room"
    upstream = [x for x in o.serves if x in a.fed_by]
    if upstream:
        return 3, f"serves the {display_name(upstream[0])}, which supplies {a.equip_label}"
    shared = [x for x in o.fed_by if x in a.fed_by]
    if shared:
        return 1, "also supplied by " + " and ".join(display_name(x) for x in shared)
    if o.floor == a.floor:
        return 1, "same floor"
    return 0, None


def build(conn, sc, alarms: dict, al, now_t: float) -> Facts:
    """`alarms` maps point -> live alarm (scheduler.LiveAlarm); `al` is the one being judged."""
    cfg = config()["timing"]
    a: Asset = al.asset
    now = sc.at(now_t)

    alarm = {"description": a.label, "equipment": a.equip_label, "location": a.loc}
    if a.attributes:
        alarm["equipment_notes"] = a.attributes
    ev = al.event
    if ev.value is not None and a.point in sc.trends:
        latest = conn.execute("SELECT value FROM trend_samples WHERE point_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                              (a.point, ts(now))).fetchone()
        if latest:
            ev = replace(ev, value=latest["value"])
    reading, past = _reading(ev)
    if reading:
        alarm["reading"] = reading
    slope = None
    if a.point in sc.trends:
        trend, slope = _trend(conn, a.point, sc.trends[a.point], now)
        if trend:
            alarm["trend"] = trend
        if a.critical_limit is not None and ev.value is not None and slope and slope > 0 and ev.value < a.critical_limit:
            mins = (a.critical_limit - ev.value) / slope
            alarm["projection"] = (f"at the current rate it reaches the {a.critical_limit_name} "
                                   f"({fmt_num(a.critical_limit)} {ev.unit}) in about {fmt_dur(mins * 60)}")
    alarm["status"] = (f"active for {fmt_dur(now_t - al.last_trigger_t)}" if al.status != "cleared"
                       else f"returned to normal {fmt_dur(now_t - al.cleared_t)} ago")
    if al.count > 1:
        alarm["triggers"] = f"triggered {al.count} times in the last {fmt_dur(now_t - al.first_t)}"

    history, max_min, hraw = _history(conn, a.point, now, sc.at(al.first_t))
    if hraw.get("durations") and al.status != "cleared":
        active = now_t - al.last_trigger_t
        longer = sum(1 for d in hraw["durations"] if active > d)
        n = len(hraw["durations"])
        alarm["duration_compared_with_history"] = (
            f"already longer than all {n} past occurrences" if longer == n else
            f"already longer than {longer} of {n} past occurrences")

    lookback = cfg["candidate_lookback_min"] * 60
    recent = [o for o in alarms.values() if o is not al and not o.life_safety and now_t - o.first_t <= lookback]
    context = {"occupancy": _occupancy(now)}
    if sc.weather:
        context["weather"] = sc.weather
    kind = a.point.split(".")[-1]
    same_kind = [o for o in recent if o.asset.category == a.category and o.asset.point.split(".")[-1] == kind]
    if same_kind:
        context["other_alarms_of_the_same_kind_last_30_min"] = (
            f"{len(same_kind)} ({sum(1 for o in same_kind if o.status == 'cleared')} have cleared)")
    context["all_other_alarms_last_30_min"] = len(recent)

    # Candidates: nearby, connected, or part of a building-wide burst. Generous, then capped.
    cands: list[Candidate] = []
    burst_ids = set()
    early = sorted(recent, key=lambda o: o.first_t)
    if len(early) >= cfg["burst_min_alarms"]:
        window = [o for o in early if o.first_t - early[0].first_t <= cfg["burst_window_s"]]
        if len(window) >= cfg["burst_min_alarms"]:
            burst_ids = {o.asset.point for o in window[:2]}
    for o in recent:
        score, rel = relation(a, o.asset)
        if o.asset.point in burst_ids:
            score = max(score, 1.5)
        entry = {"id": o.asset.point, "alarm": o.asset.label, "location": o.asset.loc}
        if rel:
            entry["relation_to_this_equipment"] = rel
        entry["started"] = f"{fmt_dur(now_t - o.first_t)} ago"
        entry["status"] = "still active" if o.status != "cleared" else f"cleared {fmt_dur(now_t - o.cleared_t)} ago"
        cands.append(Candidate(o.asset.point, o.asset.label, entry, score, o.first_t))
    for sid, title, at in schedule_events(now):
        entry = {"id": sid, "event": f"Building schedule: {title.split(' (')[0]}",
                 "started": f"{fmt_dur((now - at).total_seconds())} ago ({at:%H:%M:%S})"}
        cands.append(Candidate(sid, title, entry, 2, (at - sc.start).total_seconds()))
    cands.sort(key=lambda c: (-c.score, c.first_t))
    cands = sorted(cands[: cfg["max_candidates"]], key=lambda c: c.first_t)

    open_t = conn.execute("SELECT summary, opened_at FROM tickets WHERE equipment=? AND opened_at<=? AND "
                          "(closed_at IS NULL OR closed_at>?)", (a.equip, ts(now), ts(now))).fetchall()
    tickets = [f"{t['summary']} (opened {fmt_dur((now - datetime.fromisoformat(t['opened_at'])).total_seconds())} ago)"
               for t in open_t] or "none"

    state = {
        "alarm": alarm,
        "history_of_this_point": history,
        "open_tickets_for_this_equipment": tickets,
        "building_context": context,
        "other_alarms_last_30_min": [c.entry for c in cands] or "none",
    }
    return Facts(state=state, candidates=cands, floor=floor_for(a.category), history_max_min=max_min,
                 distance_past_limit=past, slope_per_min=slope, raw=hraw)
