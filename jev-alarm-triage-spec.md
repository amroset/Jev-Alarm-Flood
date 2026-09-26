# Jev Alarm Triage — Build Spec

Handoff document for building the hackathon project. It captures the design decisions made during planning. Read it fully before writing code. Where this spec and the Jev documentation disagree about the API, the documentation wins.

A static proof-of-concept UI exists (`alarm-triage-poc.html`). It runs the policy logic in the browser but uses **scripted** Jev answers. Use it as the reference for the UI and the policy; the goal of this build is to replace the scripted answers with real Jev calls behind a backend and to fix the known issues listed in section 11.

---

## 1. Context

- **Event:** hackathon built around Jev, a model from TypeSafe AI. Mixed team (not only hardware/software engineers). Judges are mostly non-technical, so the demo must be understandable without domain knowledge.
- **Organizer constraints:** no game demos.
- **Chosen idea:** live triage of alarms from building management systems (BMS), aimed at the problem of alarm floods and alarm fatigue in commercial buildings.

### Elevator pitch

Building alarm systems generate far more alarms than operators can handle: guidelines (EEMUA 191 / ISA-18.2) target about one alarm per operator every ten minutes, yet a single power dip can trigger dozens in a minute. Operators start ignoring them, and the one that matters gets lost. Jev fits this job: many fast judgments where the possible answers are known up front (how urgent, which team, same cause as another alarm, can it wait). It answers several questions in one call, cheaply enough to judge every alarm in context, needs no training data, and reports confidence so the system knows when to act and when to ask a person. Deterministic code handles facts and the safety net, so Jev can make operators' work easier but can never make the building less safe than today.

---

## 2. About Jev (verify against docs.typesafe.ai)

- **Shape:** send a *state* (text or structured context) plus *typed questions*; receive constrained answers with probabilities. No text generation.
- **Question primitives:** `Choice` (pick one option), `Score` (ordered rubric levels), `Noul` (probability of "yes", 0–1). Choice/Score have a confidence value; Noul does not have a separate confidence field.
- **Multiple questions per request** over the same state, answered in one parallel pass.
- **Published performance claims (TypeSafe's own numbers):** roughly 70–500 ms end to end; $0.042 per million input tokens, output free.
- **Limits:** up to 255 options per question. Current model `jev-1.13.0` (aliases `jev-latest`, `jev-preview`). Pin the version for evaluations.
- **Documented weaknesses:** arithmetic, counting, date/time comparison, literal reading, distractors, large irrelevant state. → All arithmetic, time math and counting must happen in code before Jev sees the state. Keep the state short and focused.
- **Access:** Python SDK `pip install typesafe-sdk` (Python ≥ 3.10; v0.7.0 switched to Pydantic and added `response_model`). JS SDK `@typesafe-ai/sdk`. Also available via Vercel AI Gateway, OpenRouter (`typesafe/jev-1.13`) and Cloudflare AI if early access is not available.
- **Do not guess SDK parameter names.** Read the SDK README / docs first (`docs.typesafe.ai/primitives`, `/confidence`, `/models`, `/model-jaggedness/jev-1.13`).
- **The API key stays server-side.** A browser page cannot call Jev directly.

---

## 3. Core design principles (non-negotiable)

1. **Code provides facts, never opinions.** The state Jev reads contains only measured or looked-up facts. Never write conclusions such as "Related alarms: X", "part of the startup burst", or "caused by the voltage dip". Those are exactly the judgments Jev must make.
2. **Jev makes the judgment calls** that would otherwise need a hand-written rule for every situation (severity in context, causal links between alarms, whether something can wait).
3. **Code turns judgments into actions** through an explicit, deterministic policy with thresholds.
4. **Never delete or suppress an alarm.** Jev only reorders and routes. Every alarm stays in the full log.
5. **Floors:** dangerous categories have a minimum priority that Jev can never lower (water leaks, server room temperature, elevators, lab/medicine freezers).
6. **Asymmetric thresholds:** downgrading needs high confidence; uncertainty pushes items *up* to human review.
7. **Escalation by timer:** unacknowledged urgent alarms escalate automatically, without Jev. A re-evaluation can raise a priority but never undo an escalation.
8. **Fallback:** if Jev is unavailable or errors, use the BMS's own priority (today's behavior).
9. **Life-safety systems are out of scope:** fire alarm signals bypass the pipeline and are shown as "passed through".
10. **No operator notes in v1.** Free-text notes can go stale or be wrong. Use objective alarm history instead. Notes are a future feature (section 12).

---

## 4. Pipeline

### Stage 0 — Setup per building (automatic where possible)
- **Asset catalog:** point → equipment, location, criticality, and which spaces/equipment each unit serves (e.g. CRAC-2 serves the server room; AHU-3 is fed by the chilled-water loop). In real buildings this comes from the BMS and tagging standards (Project Haystack, Brick Schema).
- **Floor table:** ~10–20 rows, category → minimum priority.
- **Teams:** list of teams used for routing.
- **Thresholds:** configurable constants (section 7).

### Stage 1 — Alarm arrives
Raw event: point id, value, limit, timestamp, BMS priority (High/Medium/Low). In the hackathon these come from the simulator (section 8).

### Stage 2 — Fact builder (code, no Jev)
- **Life-safety check:** bypass immediately.
- **Chatter grouping:** repeated triggers of the same point within a short window become one alarm with a counter.
- **Computed facts:**
  - duration so far, value vs limit (how far past the limit);
  - trend from trend logs, including *before* the alarm fired (linear fit over the last N minutes → "rising 0.4 °C/min");
  - history of this point: occurrences in last 24 h / 30 / 90 days, typical time of day, typical duration, share auto-cleared, share that led to a ticket, time since last occurrence;
  - schedule context (e.g. HVAC startup at 06:00), occupancy, time of day, weather if available;
  - open maintenance tickets for the equipment.
- **Candidate alarms (neutral, with decoys):** other alarms active or recent within a lookback window (e.g. 30 min) that are nearby in space *or* connected in the asset catalog *or* building-wide bursts. Be generous so that unrelated alarms naturally appear, but cap the list at ~5–8 to respect Jev's distractor weakness.
- **Floor lookup** for the category.

### Stage 3 — State writer (code)
Short, neutral text. Example:

```
Alarm: server room temperature 27.8 °C (limit 27 °C)
Location: floor 1, server room
Duration: 6 min; trend: rising 0.4 °C/min for the last 6 min
History of this point: no alarm in the last 90 days
Occupancy: unoccupied (Saturday 02:14)
Other alarms in this building in the last 30 minutes:
- CRAC-2 cooling unit (serves server room): compressor fault, 6 min ago
- Lighting controller LC-S (server room): offline, 4 min ago
- AHU-4 (roof): filter pressure high, 12 min ago
Open tickets for this equipment: none
```

### Stage 4 — One Jev request, six questions
| # | Question | Type | Options / output |
|---|----------|------|------------------|
| 1 | Severity | Choice | critical now / urgent this shift / routine / informational |
| 2 | Responsible team | Choice | HVAC / electrical / plumbing / IT / security / elevator vendor / … |
| 3 | Is this consistent with this alarm's own history? | Noul | P(yes) |
| 4 | Is this caused by the same issue as one of these alarms? | Choice | each candidate id + "none of these" |
| 5 | Can it safely wait until the next business day? | Noul | P(yes) |
| 6 | Potential impact on people, equipment or operations | Score | 1–5 |

Write question criteria in plain language. Design them so "none of these" and low-severity answers are genuinely reachable.

### Stage 5 — Policy (code)
```
dflt = BMS default rank (High→urgent, Medium→routine, Low→informational)
if Jev unavailable: rank = dflt; team = facilities desk
else:
  rank = jev.severity
  if rank < dflt:
      if jev.conf >= 0.90 and P(consistent with history) >= 0.85: allow downgrade
      else: rank = dflt
  if jev.conf < 0.60 and rank < review: rank = review      # uncertainty goes up
  team = jev.team if jev.team_conf >= 0.70 else facilities desk
if floor: rank = max(rank, floor)
if escalated and rank < current: keep current                # never undo escalation
grouping:
  if jev.same_cause.p >= 0.80 and target is a candidate:
      if alarm has a floor: show on its own, with a "probably caused by" link
      else: group under the target (target alarm or incident)
  else: show on its own
```
Ranks: critical 4, urgent 3, needs review 2, routine 1, informational 0.

Explanations shown to operators are assembled by code from facts and answers (Jev produces no text), e.g. "Rising 0.4 °C/min · cooling fault 6 min ago · critical (0.93) · IT (0.88)". Avoid the middle-dot style in the final UI if it looks cluttered; any clear separator is fine.

### Stage 6 — Action / channels
critical → page on-call; urgent → notify on-shift team; needs review → top of queue; routine → operator queue; informational → morning digest.

### Stage 7 — Timing
- **Floors decide immediately.** Everything else waits a **settling window** (default 30 s) when it arrives, so related alarms can arrive and bursts can be seen.
- **Re-evaluation** (rebuild facts, ask Jev again) when: a new candidate alarm appears, the condition persists past its typical duration, the trend worsens, or at scheduled checks (e.g. 5 and 15 min).
- **Escalation:** urgent + unacknowledged for 5 min → critical, by fixed rule.
- **Fallback** on any Jev error or timeout.

### Stage 8 — Feedback
Log acknowledgments, resolutions and "marked as nuisance". Use them later to check calibration and tune thresholds (shadow mode in a real deployment).

---

## 5. Data model (SQLite is enough)

- `assets(point_id, label, equipment, location, category, criticality, serves, fed_by, bms_priority)`
- `floors(category, min_rank)`
- `alarm_events(id, point_id, started_at, cleared_at, value, limit, bms_priority, acked_at, resolution)` where resolution ∈ auto_cleared / ticket / nuisance
- `trend_samples(point_id, ts, value)`
- `tickets(id, equipment, opened_at, closed_at, summary)`
- `decisions(id, alarm_id, ts, reason, state_text, jev_request, jev_response, policy_steps, final_rank, team, latency_ms, fallback)` — full audit trail
- `incidents(id, title, created_at)` for schedule-driven groups (e.g. HVAC morning startup)

History facts are plain SQL aggregations, e.g.:

```sql
SELECT COUNT(*) AS occurrences,
       AVG((julianday(cleared_at) - julianday(started_at)) * 1440) AS avg_minutes,
       SUM(resolution = 'auto_cleared') AS auto_cleared,
       SUM(resolution = 'ticket') AS led_to_ticket
FROM alarm_events
WHERE point_id = :point AND started_at > datetime(:now, '-30 days');
```

Seed the database with synthetic history (several weeks) so the history facts are meaningful.

---

## 6. Suggested stack and repo layout

- **Backend:** Python 3.11+, FastAPI, SQLite, `typesafe-sdk`. Async calls to Jev; timeout ~2 s → fallback.
- **Frontend:** adapt `alarm-triage-poc.html` (vanilla JS) to consume the backend over WebSocket or Server-Sent Events. Keep its visual language: grey everywhere, colour reserved for priority (control-room display practice, ISA-101 style).
- **Config:** thresholds and floors in one YAML/JSON file.

```
backend/
  app.py              # FastAPI: scenario control, event stream, ack endpoint
  simulator.py        # replays scenarios on a simulated clock
  facts.py            # history queries, trends, candidate search
  state_writer.py     # neutral state text
  jev_client.py       # question definitions, SDK calls, fallback
  policy.py           # deterministic rules (port from the PoC)
  scheduler.py        # settling window, re-evaluation, escalation timers
  db.py, seed.py      # schema + synthetic history
  scenarios/*.yaml    # scenario scripts with ground-truth labels
eval/
  run_eval.py         # rules vs LLM vs Jev on the labeled set
  baselines.py        # hand-written rule set + LLM baseline
frontend/
  index.html
config.yaml
```

---

## 7. Default thresholds

| Name | Value |
|------|-------|
| Downgrade: min severity confidence | 0.90 |
| Downgrade: min P(consistent with history) | 0.85 |
| Send to review below confidence | 0.60 |
| Route to team: min team confidence | 0.70 |
| Group under same cause: min probability | 0.80 |
| Settling window | 30 s |
| Escalate unacknowledged urgent after | 5 min |
| Candidate lookback | 30 min |
| Max candidates per question | 8 |

---

## 8. Scenarios (simulator)

Each scenario is a script of timed events (alarm, clear, trend updates) on a simulated clock, plus **ground-truth labels** written by the team (correct priority, team, true cause or "none"). Do not use an LLM to create labels.

1. **Monday startup (06:00, 10 alarms in the first minute).** Startup transients should go to digest/queue based on history; a VAV box that never alarms at startup should go to review; an air handler still active after 5 min (longer than any previous startup) should be escalated on re-evaluation; a server-room temperature blip is decided immediately (floor) and later clears.
2. **Power dip (14:32, 43 alarms in about a minute).** Most alarms should link to the switchboard voltage dip; elevators stay separate (floor); fire panel trouble passes through; one air handler that fails to restart is pulled out on re-evaluation.
3. **Server room at night (02:08).** CRAC-2 compressor fault, then 6 min later the room heats up. **Add a decoy:** a lighting controller in the server room goes offline at the same time. Expected: temperature alarm links to CRAC-2, not to the lighting controller; one critical page.
4. **Water leak.** Basement leak sensor, silent for 12 months, triggers three times. Includes a toggle to inject a wrong Jev answer (routine, 0.72) to demonstrate that the downgrade bar, the floor and the escalation timer still keep it safe.

Add for evaluation:
- **Decoys-only cases:** candidate list contains no true cause; correct answer is "none of these".
- **Coincidences:** same room, same minute, unrelated equipment.
- **Non-local causes:** network switch failure → communication alarms on several floors; chiller trip → temperature deviations across the building.
- **Sensor fault:** impossible value.

Target 30–50 labeled cases.

---

## 9. Evaluation (the answer to "why AI?")

Systems compared on identical inputs:
1. **Rules:** the best rule set the team can write in a few hours (including "group everything in the same zone within 30 min").
2. **LLM:** same state text and questions, one fast model and one frontier model, structured output.
3. **Jev.**

Metrics:
- recall on alarms that truly need attention (must be high);
- false alarms that reach a person, expressed per week (alarm-fatigue metric, relatable for judges);
- linking accuracy, with separate reporting of decoys-only cases ("none of these" rate);
- calibration: accuracy per confidence bucket (reliability chart);
- automation rate: share handled without a person at the chosen thresholds, and its accuracy;
- latency p50/p95 and cost per 1,000 decisions.

Honesty rules: if rules match Jev on a category, say so. The decoys-only test is the key proof that Jev is judging rather than echoing the state.

---

## 10. Demo script (non-technical judges)

1. **The problem:** run the power-dip scenario. Left column floods with 43 alarms; right column shows a handful of items that need a person.
2. **Why each decision:** click an item and show facts → Jev's six answers → policy steps.
3. **Jev rejects a false link:** server-room scenario with the lighting-controller decoy.
4. **Safety when Jev is wrong:** water-leak scenario with the injected mistake; floor + escalation keep it safe.
5. **Outage:** toggle "Jev offline" → system behaves exactly like today.
6. **Numbers:** latency, cost, and the evaluation results vs rules and LLM.

---

## 11. Known issues in the current PoC to fix

- State text contains leading phrases ("Related alarms: …", "part of a burst that began when…", "part of a burst that started with the voltage dip"). Replace with neutral fact lists and add decoys.
- Jev answers are scripted. Replace with live calls via the backend; keep a "replay recorded answers" mode so the demo still works offline.
- Incident groups for bursts are pre-defined in scenarios; in the real build, schedule-based incidents (e.g. 06:00 startup) may be created by code from the building schedule, but causal grouping must come from Jev's question 4.
- The "Acknowledge" control is nested inside a button element; restructure for accessibility.

---

## 12. Out of scope / future work

- **Operator notes** with guardrails: owner + expiry date or linked ticket; can never lower a floor; periodic Jev consistency check ("is this note still consistent with observed behavior?") that flags stale notes for review.
- **Real BMS connectors:** e.g. Niagara Alarms Service API, BACnet gateways; tagging via Haystack/Brick.
- **Triage of FDD output** (fault detection and diagnostics lists) with the same layer.
- **Shadow mode** deployment and threshold calibration from operator feedback.
- Other building types and care-home monitoring (same architecture, higher safety bar).

---

## 13. Background and sources used in planning

- EEMUA 191 / ISA-18.2 / IEC 62682 alarm-rate benchmarks (≈1 alarm per 10 min per operator; >10 in 10 min = flood).
- HFM Magazine (ASHE), "Taming building automation system alarm fatigue", 2019.
- ABB alarm rationalization factsheet (Milford Haven, Texas City as alarm-management failures).
- University of Iowa FDD program (onboarding effort, dedicated prioritization team).
- Layered control: Brooks (1986) subsumption; Gat (1998) three-layer architectures; Michon (1985) strategic/tactical/operational driver model. Jev sits in the tactical layer: slower than control loops, faster and cheaper than LLM deliberation.
- Community catalog of Jev projects: github.com/cobanov/awesome-jev (no existing BMS alarm-triage project found).

## 14. Open questions for the team

- Hackathon duration and team size (determines how much of section 9 is feasible; minimum: Jev vs rules vs LLM on 30 cases + live decoy and mistake demos).
- Jev access route (early-access key, Vercel AI Gateway, OpenRouter).
- Who owns: simulator + data, fact builder + Jev client, policy + scheduler, frontend, evaluation.
