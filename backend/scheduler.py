"""Engine: replays a scenario on a simulated clock and runs the pipeline (spec stages 1-8).

Timing rules (stage 7): floors decide immediately; everything else waits a settling window.
Re-evaluation on retrigger, new connected/nearby alarm, running past its longest history,
worsening trend, and scheduled checks. Urgent + unacknowledged escalates by a fixed rule.
The clock pauses while Jev answers, so replay mode reproduces live runs exactly.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import uuid
from dataclasses import dataclass, field

import facts as factmod
import policy
import seed
from catalog import CHANNEL, RANK, RNAME, Asset, asset, building, config, floor_for, is_life_safety
from db import ts
from facts import fmt_dur
from jev_client import JevClient, injected, questions_for
from simulator import Event, Scenario, scenarios
from state_writer import display_lines, write

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class LiveAlarm:
    asset: Asset
    event: Event
    first_t: float
    last_trigger_t: float
    status: str = "settling"        # settling | active | cleared | bypass
    count: int = 1
    cleared_t: float | None = None
    rank: int | None = None
    rank_since: float = 0
    team: str | None = None
    parent: str | None = None
    link: str | None = None
    acked: bool = False
    escalated: bool = False
    evals: list = field(default_factory=list)
    last_decision_t: float | None = None
    last_distance: float | None = None
    last_slope: float | None = None
    settle_due: float | None = None
    typical_done: bool = False
    db_id: int | None = None

    @property
    def life_safety(self) -> bool:
        return is_life_safety(self.asset)


class Engine:
    def __init__(self, conn, jev: JevClient):
        self.conn = conn
        self.jev = jev
        self.lock = asyncio.Lock()
        self.speed = 10
        self.offline = False
        self.mistake = False
        self.mode = "live" if jev.live_available else "replay"
        self.playing = False
        self.version = 0
        self.load(next(iter(scenarios())))

    # ------------------------------------------------------------------ setup
    def load(self, scenario_id: str):
        self.sc: Scenario = scenarios()[scenario_id]
        self.reset()

    def reset(self):
        self.playing = False
        seed.seed(self.conn, self.sc)
        self.run_id = uuid.uuid4().hex[:12]
        self.now = 0.0
        self.ev_idx = 0
        self.alarms: dict[str, LiveAlarm] = {}
        self.order: list[str] = []
        self.feed: list[dict] = []
        self.incidents: dict[str, dict] = {}
        self.timers: list = []
        self.seq = itertools.count()
        self.stats = {"jev_calls": 0, "fallbacks": 0, "lat_sum": 0, "tokens": 0, "live": 0, "replay": 0}
        self.ended = False
        self.version += 1

    def _timer(self, t: float, kind: str, point: str, reason: str = ""):
        heapq.heappush(self.timers, (t, next(self.seq), kind, point, reason))

    def clock(self, t: float) -> str:
        d = self.sc.at(t)
        return f"{DAYS[d.weekday()]} {d:%H:%M:%S}"

    # ------------------------------------------------------------------ simulation
    def _next_escalation(self) -> float:
        after = config()["timing"]["escalate_urgent_after_s"]
        times = [al.rank_since + after for al in self.alarms.values() if self._escalatable(al)]
        return min(times, default=float("inf"))

    def _escalatable(self, al: LiveAlarm) -> bool:
        return al.status == "active" and not al.acked and not al.escalated and not al.parent and al.rank == RANK["urgent"]

    def next_time(self) -> float:
        n = min(self.sc.events[self.ev_idx].t if self.ev_idx < len(self.sc.events) else float("inf"),
                self.timers[0][0] if self.timers else float("inf"), self._next_escalation())
        return self.sc.duration if n == float("inf") else max(n, self.now + 0.001)

    async def advance(self, to: float):
        to = min(to, self.sc.duration)
        while True:
            nxt = min(self.sc.events[self.ev_idx].t if self.ev_idx < len(self.sc.events) else float("inf"),
                      self.timers[0][0] if self.timers else float("inf"), self._next_escalation())
            if nxt > to:
                break
            self.now = max(self.now, nxt)
            due: dict[str, str] = {}
            while self.ev_idx < len(self.sc.events) and self.sc.events[self.ev_idx].t <= self.now:
                self._apply(self.sc.events[self.ev_idx], due)
                self.ev_idx += 1
            while self.timers and self.timers[0][0] <= self.now:
                _, _, kind, point, reason = heapq.heappop(self.timers)
                self._fire(kind, point, reason, due)
            if due:
                await asyncio.gather(*(self._decide(self.alarms[p], r) for p, r in due.items()))
            self._escalations(self.now)
        self.now = max(self.now, to)
        self._escalations(self.now)
        if self.now >= self.sc.duration:
            self.ended = True
            self.playing = False
            self.jev.save_recordings()
        self.version += 1

    def _apply(self, ev: Event, due: dict):
        t = self.now
        cfg = config()["timing"]
        if ev.kind == "clear":
            al = self.alarms.get(ev.point)
            if not al or al.status == "cleared":
                return
            al.status = "cleared"
            al.cleared_t = t
            self.conn.execute("UPDATE alarm_events SET cleared_at=?, resolution='auto_cleared' WHERE id=?",
                              (ts(self.sc.at(t)), al.db_id))
            self.feed.insert(0, {"t": t, "id": ev.point, "kind": "clear"})
            return

        a = asset(ev.point)
        cur = self.conn.execute(
            "INSERT INTO alarm_events(point_id, started_at, value, limit_value, bms_priority, run_id) VALUES(?,?,?,?,?,?)",
            (a.point, ts(self.sc.at(t)), ev.value, ev.limit, a.bms, self.run_id))
        existing = self.alarms.get(ev.point)
        chatter = existing and not existing.life_safety and (
            existing.status != "cleared" or t - existing.cleared_t <= cfg["chatter_window_min"] * 60)
        if chatter:
            al = existing
            al.count += 1
            al.last_trigger_t = t
            al.db_id = cur.lastrowid
            if ev.value is not None:
                al.event = ev
            self.feed.insert(0, {"t": t, "id": ev.point, "kind": "retrigger", "count": al.count})
            if al.status == "cleared":
                al.status = "active" if al.rank is not None else "settling"
                al.cleared_t = None
            if al.status == "active":
                due.setdefault(ev.point, f"Re-evaluation: triggered again ({al.count} times)")
            return

        al = LiveAlarm(asset=a, event=ev, first_t=t, last_trigger_t=t, db_id=cur.lastrowid, rank_since=t)
        if existing:
            self.order.remove(ev.point)
        self.alarms[ev.point] = al
        self.order.append(ev.point)
        self.feed.insert(0, {"t": t, "id": ev.point, "kind": "alarm"})
        if al.life_safety:
            al.status = "bypass"
        elif floor_for(a.category):
            due[ev.point] = "Arrived: category with a floor, decided immediately"
        else:
            al.settle_due = t + cfg["settle_s"]
            self._timer(al.settle_due, "settle", ev.point)
        # A new alarm on connected equipment or in the same room re-opens decided alarms.
        for other in self.alarms.values():
            if other is al or other.status != "active":
                continue
            score, _ = factmod.relation(other.asset, a)
            if score >= 2:
                where = "on connected equipment" if score >= 3 else "in the same room"
                self._timer(t + 1, "related", other.asset.point, f"Re-evaluation: new alarm {where} ({a.label})")

    def _fire(self, kind: str, point: str, reason: str, due: dict):
        al = self.alarms.get(point)
        if not al:
            return
        cfg = config()["timing"]
        if kind == "settle":
            if al.status == "settling" and al.settle_due == self.now:
                due.setdefault(point, f"Decided after a {cfg['settle_s']} s settling window")
            return
        if al.status != "active":
            return
        recent = al.last_decision_t is not None and self.now - al.last_decision_t < cfg["reeval_debounce_s"]
        if kind == "related" and not recent:
            due.setdefault(point, reason)
        elif kind in ("check", "typical"):
            due.setdefault(point, reason)
        elif kind == "trend":
            f = factmod.build(self.conn, self.sc, self.alarms, al, self.now)
            d0, d1, s0, s1 = al.last_distance, f.distance_past_limit, al.last_slope, f.slope_per_min
            worse = (d0 is not None and d1 is not None and d1 - d0 >= max(0.5, 0.5 * abs(d0))) or \
                    (s0 is not None and s1 is not None and s1 > 0 and s1 >= 1.5 * max(s0, 0.01))
            if worse and not recent:
                due.setdefault(point, "Re-evaluation: trend worsening")
            else:
                self._timer(self.now + cfg["trend_check_s"], "trend", point)

    # ------------------------------------------------------------------ decisions
    def _root(self, pid: str | None, avoid: str | None = None) -> str | None:
        seen = set()
        while pid in self.alarms and self.alarms[pid].parent and pid not in seen:
            seen.add(pid)
            if pid == avoid:
                return avoid
            pid = self.alarms[pid].parent
        return pid

    async def _decide(self, al: LiveAlarm, reason: str):
        t = self.now
        f = factmod.build(self.conn, self.sc, self.alarms, al, t)
        state = write(f)
        qs = questions_for(f)
        body = self.jev.request_body(state, qs)
        raw = err = None
        mistake = self.mistake and self.sc.mistake and self.sc.mistake["point"] == al.asset.point
        if self.offline:
            jev, err = None, "Jev outage simulated by the operator toggle"
        elif mistake:
            jev = injected(self.sc.mistake["answers"])
        else:
            jev, body, raw, err = await self.jev.ask(state, qs, self.mode)
        titles = {c.id: c.label for c in f.candidates}
        cleared = {c.id for c in f.candidates if c.id in self.alarms and self.alarms[c.id].status == "cleared"}
        outlived = "all" in str(f.state["alarm"].get("duration_compared_with_history", ""))
        pol = policy.decide(al.asset, jev, {c.id for c in f.candidates}, titles, al.rank, al.escalated,
                            cleared, outlived)

        if pol.parent and self._root(pol.parent, avoid=al.asset.point) == al.asset.point:
            pol.steps.append({"t": "Grouping would create a loop with an alarm already grouped under this one, "
                                   "so it stays on its own.", "stop": False})
            pol.parent = None
        if pol.parent and pol.parent.startswith("SCHED-") and pol.parent not in self.incidents:
            self.incidents[pol.parent] = {"title": titles[pol.parent], "created_at": t}
            self.conn.execute("INSERT OR IGNORE INTO incidents VALUES(?,?,?)",
                              (pol.parent, titles[pol.parent], ts(self.sc.at(t))))

        # The escalation clock keeps running while an alarm moves between urgent and critical.
        if al.rank != pol.rank and not (al.rank is not None and al.rank >= RANK["urgent"] and pol.rank >= RANK["urgent"]):
            al.rank_since = t
        al.rank, al.team, al.parent, al.link = pol.rank, pol.team, pol.parent, pol.link
        if al.status == "settling":
            al.status = "active"
        first = al.last_decision_t is None
        al.last_decision_t, al.last_distance, al.last_slope = t, f.distance_past_limit, f.slope_per_min

        if jev:
            self.stats["jev_calls"] += 1
            self.stats["lat_sum"] += jev.latency_ms or 0
            self.stats["tokens"] += jev.tokens
            self.stats[jev.source] = self.stats.get(jev.source, 0) + 1
        else:
            self.stats["fallbacks"] += 1
        al.evals.append({
            "t": t, "reason": reason, "state": display_lines(f.state), "stateRaw": state, "jev": jev.to_dict() if jev else None, "error": err,
            "steps": pol.steps, "rank": pol.rank, "team": pol.team,
            "candidates": {c.id: c.label for c in f.candidates},
        })
        self.conn.execute(
            "INSERT INTO decisions(run_id, alarm_id, ts, reason, state_text, jev_request, jev_response, policy_steps, "
            "final_rank, team, latency_ms, fallback) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, al.asset.point, ts(self.sc.at(t)), reason, json.dumps(state, ensure_ascii=False), json.dumps(body, ensure_ascii=False),
             json.dumps(raw or (jev.to_dict() if jev else {"error": err})), json.dumps(pol.steps), pol.rank, pol.team,
             jev.latency_ms if jev else None, int(jev is None)))
        self.conn.commit()

        cfg = config()["timing"]
        if first:
            for s in cfg["reeval_checks_s"]:
                if al.first_t + s < self.sc.duration:
                    self._timer(al.first_t + s, "check", al.asset.point,
                                f"Re-evaluation: scheduled check, still active after {fmt_dur(s)}")
            if al.asset.point in self.sc.trends:
                self._timer(t + cfg["trend_check_s"], "trend", al.asset.point)
        if f.history_max_min and not al.typical_done:
            due_t = al.last_trigger_t + f.history_max_min * 60 + 1
            if t < due_t < self.sc.duration:
                al.typical_done = True
                self._timer(due_t, "typical", al.asset.point,
                            f"Re-evaluation: active longer than its longest past occurrence ({fmt_dur(f.history_max_min * 60)})")
        self.version += 1

    def _escalations(self, t: float):
        after = config()["timing"]["escalate_urgent_after_s"]
        for al in self.alarms.values():
            if self._escalatable(al) and t - al.rank_since >= after:
                at = al.rank_since + after
                al.rank, al.escalated, al.rank_since = RANK["critical"], True, at
                al.evals.append({"t": at, "reason": "Escalation timer", "kind": "escalation", "rank": al.rank, "team": al.team,
                                 "steps": [{"t": f"Urgent and not acknowledged within {fmt_dur(after)}. Escalated to "
                                                 "Critical by a fixed rule in code; Jev is not involved.", "stop": True}]})
                self.version += 1

    def ack(self, point: str):
        al = self.alarms.get(point)
        if not al or al.acked:
            return
        al.acked = True
        al.evals.append({"t": self.now, "reason": "Acknowledged by operator", "kind": "ack", "rank": al.rank, "team": al.team,
                         "steps": [{"t": "Operator acknowledged the alarm. The escalation timer stops.", "stop": False}]})
        self.conn.execute("UPDATE alarm_events SET acked_at=? WHERE id=?", (ts(self.sc.at(self.now)), al.db_id))
        self.conn.execute("INSERT INTO feedback(run_id, alarm_id, ts, kind) VALUES(?,?,?,?)",
                          (self.run_id, point, ts(self.sc.at(self.now)), "ack"))
        self.conn.commit()
        self.version += 1

    # ------------------------------------------------------------------ view
    def group_heads(self) -> dict[str, str | None]:
        """Each alarm's group head: the incident if the linked group has one, else its earliest alarm."""
        comp: dict[str, str] = {}
        def find(x):
            while comp.get(x, x) != x:
                x = comp[x]
            return x
        for p, al in self.alarms.items():
            if al.parent:
                ra, rb = find(p), find(al.parent)
                if ra != rb:
                    comp[ra] = rb
        groups: dict[str, list[str]] = {}
        for p, al in self.alarms.items():
            if al.parent or any(o.parent == p for o in self.alarms.values()):
                groups.setdefault(find(p), []).append(p)
        heads = {}
        for root, members in groups.items():
            inc = root if root in self.incidents else next(
                (self.alarms[m].parent for m in members if self.alarms[m].parent in self.incidents), None)
            # Alarms with a floor always stay visible, so they head the group or stand alone.
            floors = [m for m in members if floor_for(self.alarms[m].asset.category)]
            head = inc or min(floors or members, key=lambda m: self.alarms[m].first_t)
            for m in members:
                heads[m] = None if (m == head or m in floors) else head
        return heads

    def snapshot(self) -> dict:
        price = config()["jev"]["price_per_mtok"]
        alarms = {}
        heads = self.group_heads()
        for p, al in self.alarms.items():
            a = al.asset
            fl = floor_for(a.category)
            alarms[p] = {
                "def": {"label": a.label, "equip": a.equip_label, "loc": a.loc, "bms": a.bms,
                        "floor": fl["rank"] if fl else None, "floorName": fl["name"] if fl else None,
                        "lifeSafety": al.life_safety},
                "arrivedAt": al.first_t, "status": al.status, "rank": al.rank, "rankSince": al.rank_since,
                "parent": heads.get(p), "directParent": al.parent, "link": al.link, "team": al.team,
                "acked": al.acked, "escalated": al.escalated, "clearedAt": al.cleared_t, "count": al.count,
                "settleDue": al.settle_due, "evals": al.evals,
            }
        return {
            "type": "snapshot", "version": self.version,
            "building": config()["building"], "channels": CHANNEL, "rankNames": RNAME,
            "scenarios": [{"id": s.id, "name": s.name, "short": s.short} for s in scenarios().values()],
            "scenario": {"id": self.sc.id, "name": self.sc.name, "desc": self.sc.desc, "duration": self.sc.duration,
                         "hasMistake": bool(self.sc.mistake)},
            "now": self.now, "clock": self.clock(self.now), "playing": self.playing, "ended": self.ended,
            "speed": self.speed, "offline": self.offline, "mistake": self.mistake, "mode": self.mode,
            "liveAvailable": self.jev.live_available, "model": self.jev.model,
            "settle": config()["timing"]["settle_s"],
            "feed": [dict(f, clock=self.clock(f["t"])) for f in self.feed],
            "alarms": alarms, "order": self.order, "incidents": self.incidents,
            "stats": dict(self.stats, cost=self.stats["tokens"] * price / 1e6),
        }
