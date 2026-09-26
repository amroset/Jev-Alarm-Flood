"""SQLite schema (spec section 5). History tables are reseeded per scenario run; decisions accumulate."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from catalog import BACKEND, building, config

DB_PATH = BACKEND / "triage.db"
TS = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets(point_id TEXT PRIMARY KEY, label TEXT, equipment TEXT, location TEXT,
  category TEXT, criticality TEXT, serves TEXT, fed_by TEXT, bms_priority TEXT);
CREATE TABLE IF NOT EXISTS floors(category TEXT PRIMARY KEY, min_rank INTEGER);
CREATE TABLE IF NOT EXISTS alarm_events(id INTEGER PRIMARY KEY, point_id TEXT, started_at TEXT, cleared_at TEXT,
  value REAL, limit_value REAL, bms_priority TEXT, acked_at TEXT, resolution TEXT, run_id TEXT);
CREATE INDEX IF NOT EXISTS ix_events_point ON alarm_events(point_id, started_at);
CREATE TABLE IF NOT EXISTS trend_samples(point_id TEXT, ts TEXT, value REAL);
CREATE INDEX IF NOT EXISTS ix_trend_point ON trend_samples(point_id, ts);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, equipment TEXT, opened_at TEXT, closed_at TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, run_id TEXT, alarm_id TEXT, ts TEXT, reason TEXT,
  state_text TEXT, jev_request TEXT, jev_response TEXT, policy_steps TEXT, final_rank INTEGER, team TEXT,
  latency_ms INTEGER, fallback INTEGER);
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, title TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, run_id TEXT, alarm_id TEXT, ts TEXT, kind TEXT);
"""


def ts(d: datetime) -> str:
    return d.strftime(TS)


def connect(path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM assets")
    conn.executemany(
        "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)",
        [(a.point, a.label, a.equip, a.loc, a.category, a.criticality, json.dumps(a.serves), json.dumps(a.fed_by), a.bms)
         for a in building()["assets"].values()],
    )
    conn.execute("DELETE FROM floors")
    conn.executemany("INSERT INTO floors VALUES(?,?)", [(k, v["rank"]) for k, v in config()["floors"].items()])
    conn.commit()
    return conn
