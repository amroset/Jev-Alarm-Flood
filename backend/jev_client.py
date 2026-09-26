"""Jev client (spec stage 4): one request, six typed questions, with timeout fallback and record/replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy, Score

from catalog import ROOT, config
from facts import Facts

log = logging.getLogger("jev")

SEVERITY = Choice(
    instructions=(
        "How soon does a building operator need to act on `alarm` itself (not on the other alarms listed)? "
        "Judge from its reading and trend, `history_of_this_point`, `building_context`, and whether "
        "`other_alarms_last_30_min` make it more or less serious."),
    criteria={
        "critical": "Act now: ongoing or imminent harm to people, critical equipment such as servers, or building "
                    "operations. Someone should be paged immediately, even at night.",
        "urgent": "A real problem for the on-shift team to handle within this shift; it could get worse if left for hours, "
                  "but there is no immediate harm.",
        "routine": "A real but minor issue that can go in the normal work queue and be handled within a day or two.",
        "informational": "Expected or self-resolving behavior, for example a transient that matches this point's own "
                         "history. Nobody needs to act; a line in the daily digest is enough.",
    },
)

HISTORY = Noul(
    instructions=(
        "Is `alarm` consistent with `history_of_this_point`? Yes means it is happening the way this point has "
        "alarmed many times before (similar time of day, similar duration, usually cleared by itself). "
        "No means it is new, rare, or unusual for this point, for example lasting longer than ever before."),
)

WAIT = Noul(
    instructions=(
        "Can `alarm` safely be left until the next business day without risk to people, equipment "
        "or building operations?"),
)

IMPACT = Score(
    instructions="If nobody acts on `alarm`, what is the potential impact on people, equipment or operations?",
    criteria=[
        "None: nobody would notice",
        "Minor: small comfort or efficiency loss in one area",
        "Moderate: noticeable discomfort or degraded service for part of the building",
        "Serious: likely equipment damage or disruption of important operations",
        "Severe: danger to people or loss of critical systems",
    ],
)


def team_question() -> Choice:
    return Choice(instructions="Which team should handle `alarm`?", criteria=dict(config()["teams"]))


def same_cause_question(facts: Facts) -> Choice | None:
    if not facts.candidates:
        return None
    criteria = {c.id: c.label for c in facts.candidates}
    criteria["none"] = "None of the listed alarms or events: an independent cause"
    return Choice(
        instructions=(
            "Is `alarm` explained by the same underlying cause as one of the alarms or events in "
            "`other_alarms_last_30_min`, such as the same equipment failure, the same power event, or the same "
            "scheduled operation? Being in the same room or at the same time is not enough: pick one only if there "
            "is a plausible physical cause-and-effect link. Otherwise pick none."),
        criteria=criteria,
    )


def questions_for(facts: Facts) -> dict:
    q = {"severity": SEVERITY, "team": team_question(), "history": HISTORY, "wait": WAIT, "impact": IMPACT}
    sc = same_cause_question(facts)
    if sc:
        q["same_cause"] = sc
    return q


@dataclass
class JevAnswer:
    severity: str
    sev_conf: float
    sev_probs: dict
    team: str
    team_conf: float
    p_history: float
    same_target: str | None     # None when "none" was chosen or no candidates were offered
    same_p: float | None        # probability of the chosen option
    same_offered: bool
    p_wait: float
    impact: float               # 1..5
    impact_conf: float
    source: str = "live"        # live | replay | injected
    latency_ms: int | None = None
    tokens: int = 0
    model: str | None = None

    def to_dict(self):
        return asdict(self)


def parse(raw: dict, source: str, latency_ms: int | None) -> JevAnswer:
    a = raw["answers"]
    sc = a.get("same_cause")
    choice = sc["choice"] if sc else None
    return JevAnswer(
        severity=a["severity"]["choice"], sev_conf=a["severity"]["confidence"], sev_probs=a["severity"]["probabilities"],
        team=a["team"]["choice"], team_conf=a["team"]["confidence"],
        p_history=a["history"]["noul"],
        same_target=None if choice in (None, "none") else choice,
        same_p=sc["probabilities"].get(choice) if sc else None, same_offered=bool(sc),
        p_wait=a["wait"]["noul"],
        impact=a["impact"]["score"] + 1, impact_conf=a["impact"]["confidence"],
        source=source, latency_ms=latency_ms, tokens=raw.get("usage", {}).get("input_tokens", 0), model=raw.get("model"),
    )


def injected(spec: dict) -> JevAnswer:
    """Build a scripted wrong answer for the safety demo."""
    s = spec
    same = s["same_cause"]
    return JevAnswer(
        severity=s["severity"]["choice"], sev_conf=s["severity"]["confidence"], sev_probs={},
        team=s["team"]["choice"], team_conf=s["team"]["confidence"], p_history=s["history"],
        same_target=None if same["choice"] == "none" else same["choice"], same_p=same["p"], same_offered=True,
        p_wait=s["wait"], impact=s["impact"]["score"] + 1, impact_conf=s["impact"]["confidence"], source="injected",
    )


class JevClient:
    def __init__(self):
        cfg = config()["jev"]
        self.model = cfg["model"]
        self.timeout = cfg["timeout_s"]
        self.path = ROOT / cfg["recordings"]
        self.recordings: dict = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.client = None
        if os.environ.get("TYPESAFE_API_KEY"):
            self.client = AsyncTypeSafeClient(
                model=self.model, retry=RetryPolicy(max_retries=1, backoff_max=0.2, timeout=self.timeout))

    @property
    def live_available(self) -> bool:
        return self.client is not None

    def request_body(self, state: dict, questions: dict) -> dict:
        return {"model": self.model, "state": state,
                "questions": {k: q.model_dump(exclude_none=True) for k, q in questions.items()}}

    @staticmethod
    def key(body: dict) -> str:
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def ask(self, state: dict, questions: dict, mode: str):
        """Returns (answer | None, request_body, raw_response | None, error | None)."""
        body = self.request_body(state, questions)
        k = self.key(body)
        if mode == "replay":
            rec = self.recordings.get(k)
            if not rec:
                return None, body, None, "no recorded answer for this exact state (replay mode)"
            return parse(rec["response"], "replay", rec.get("latency_ms")), body, rec["response"], None
        if not self.client:
            return None, body, None, "no API key configured"
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(self.client.system_one(state, questions), self.timeout + 0.5)
        except Exception as e:  # any failure or timeout falls back to today's behavior
            log.warning("Jev call failed: %r", e)
            return None, body, None, f"{type(e).__name__}: {e}"[:300]
        latency = round((time.perf_counter() - t0) * 1000)
        raw = resp.raw_http_response.json()
        self.recordings[k] = {"response": raw, "latency_ms": latency}
        return parse(raw, "live", latency), body, raw, None

    def save_recordings(self):
        """Merge with what is on disk, so several processes can add recordings."""
        on_disk = json.loads(self.path.read_text()) if self.path.exists() else {}
        on_disk.update(self.recordings)
        self.recordings = on_disk
        self.path.write_text(json.dumps(on_disk, ensure_ascii=False))
