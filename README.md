# Jev Alarm Triage

Live triage of building-management alarms with Jev (TypeSafe). The design is in
[jev-alarm-triage-spec.md](jev-alarm-triage-spec.md); the original static mock-up is
[alarm-triage-poc.html](alarm-triage-poc.html). This build replaces the mock-up's scripted answers with real
Jev calls behind a Python backend.

## Run it

```bash
# one-time setup (Python 3.10+; this machine uses /usr/bin/python3.12)
python3.12 -m venv .venv            # if ensurepip is missing: --without-pip, then run get-pip.py
.venv/bin/python -m pip install -r requirements.txt
cp backend/.env.example backend/.env   # then paste your TypeSafe key into backend/.env

./run.sh                             # open http://localhost:8000
.venv/bin/python -m pytest -q backend/tests
```

**Jev answers** (control bar):
- **Live API** calls Jev for every decision and records each answer in `backend/recordings.json`.
- **Replay recorded** reuses those answers, so the demo works with no network. Runs are deterministic
  (simulated clock and seeded history), so a replay reproduces a live run exactly. Recordings for all
  four scenarios are already there.

Other toggles: **Simulate Jev outage** (every decision falls back to the BMS priority) and **Inject a Jev
mistake** (water-leak scenario only).

## How it works

```
scenario event → fact builder (code) → structured state → Jev: 6 typed questions in 1 request
             → policy (code) → rank, team, grouping → queue / escalation timers → UI over WebSocket
```

| File | Role (spec stage) |
|---|---|
| `config.yaml` | thresholds, floors, teams, schedule (4, 7) |
| `backend/building.yaml` | asset catalog: location, serves / fed_by, history profiles (0) |
| `backend/scenarios/*.yaml` | timed events, readings, trends, injected mistake (8) |
| `backend/seed.py` | synthetic history, trends and tickets relative to the scenario date |
| `backend/facts.py` | history stats, trend fits, candidates with decoys: facts only (2) |
| `backend/state_writer.py` | state for Jev plus the same content as lines for the UI (3) |
| `backend/jev_client.py` | the six questions, SDK call, 2 s timeout, record/replay (4) |
| `backend/policy.py` | deterministic rules; writes the operator-facing explanation (5) |
| `backend/scheduler.py` | settling window, re-evaluation, escalation, audit trail (1, 6, 7, 8) |
| `backend/app.py` | FastAPI: WebSocket state push, controls, acknowledge |
| `frontend/index.html` | the PoC UI, driven by the backend |

Every decision is written to the `decisions` table in `backend/triage.db`: state, request, raw response,
policy steps, latency and fallback flag.

## Changes from the spec (found while testing against live Jev)

1. **The state is JSON with named sections, not free text.** Questions refer to `alarm`,
   `history_of_this_point` and `other_alarms_last_30_min`, as the TypeSafe docs recommend. With plain text,
   Jev judged the whole situation instead of the alarm itself (a lighting controller came out *critical*
   because a temperature alarm was listed next to it). With named sections it came out *informational*.
2. **Code does the comparisons Jev is weak at**: "already longer than all 46 past occurrences" and "reaches
   the IT shutdown limit (32 °C) in about 6 min at the current rate". Without the first, the air handler
   that fails to settle at startup still matched its history at 0.83. With it, the match fell to 0.16.
3. **New policy rule, "outlived its cause"**: a grouped alarm is pulled out and shown on its own, with a
   link, when its cause has cleared, it is longer than every past occurrence, and P(history) < 0.5. Jev is
   right that AHU-7's fan failure was *caused* by the voltage dip, so question 4 alone never separates it.
4. **Group head**: the group is shown under its schedule incident if it has one, else its earliest alarm. An
   alarm with a floor always heads its group or stands alone; it is never hidden inside one.
5. **Escalation clock**: moving between urgent and critical does not restart the 5-minute timer.
6. **Confidence semantics**: Jev's Choice `confidence` is normalized, `(n·p_max − 1)/(n − 1)`, not the top
   option's probability. With four severity options, confidence 0.90 means p_max ≈ 0.93, so the downgrade bar
   is slightly stricter than it reads. Grouping uses the chosen option's probability.
7. The server-room decoy is a lighting controller *in the server room* (the spec's version), not the lobby.

## Observed behaviour (live, jev-1.13.0)

- Server room: the temperature alarm links to CRAC-2 (1.00), not to the lighting-controller decoy in the
  same room. Critical (0.97), paged once.
- Power dip: 43 alarms become 4 items for a person. One group under the switchboard; the elevators are
  separate with a link; the fire panel passes through; AHU-7 is pulled out after failing to restart.
- Startup: noise is grouped (under the chiller, a plausible physical cause, rather than the schedule
  incident). The air handler that does not settle and the VAV box that never alarms at startup stay in
  front of a person.
- Water leak with the injected mistake: the downgrade bar blocks it, the floor keeps it urgent, and the
  timer escalates it to critical at 5 minutes.
- Latency 85–420 ms per request (average about 140 ms). About 1,500–2,200 input tokens per decision (the PoC assumed 350), so about
  $0.09 per 1,000 decisions.

## Open items

- **Ground-truth labels** in `scenarios/*.yaml` (`labels:`) must be written by the team, not an LLM
  (spec section 8). They are empty.
- **Evaluation** (`eval/`, spec section 9) is not started: rules and LLM baselines, 30–50 labeled cases,
  and the decoys-only set.
- **Many "Needs review" items**: severity is often split between *informational* and *routine*, which puts
  normalized confidence below 0.60 and triggers the review rule. Worth a team decision; see the note in the
  hand-off message.
- Rotate the API key after the hackathon; it was shared in a chat transcript.
