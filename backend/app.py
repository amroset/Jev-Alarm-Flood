"""FastAPI server: scenario control, live state over WebSocket, acknowledgments.

Run from the repo root:  .venv/bin/uvicorn app:app --app-dir backend --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from catalog import ROOT  # noqa: E402
from db import connect  # noqa: E402
from jev_client import JevClient  # noqa: E402
from scheduler import Engine  # noqa: E402

logging.basicConfig(level=logging.INFO)
TICK = 0.25

engine = Engine(connect(), JevClient())
clients: set[WebSocket] = set()


def view() -> dict:
    snap = engine.snapshot()
    for a in snap["alarms"].values():
        a["evals"] = [{k: v for k, v in e.items() if k != "stateRaw"} for e in a["evals"]]
    return snap


async def broadcast(msg: dict):
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:
            clients.discard(ws)


async def ticker():
    sent = -1
    while True:
        await asyncio.sleep(TICK)
        if engine.playing:
            async with engine.lock:
                await engine.advance(engine.now + engine.speed * TICK)
        if engine.version != sent:
            sent = engine.version
            await broadcast(view())


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(ticker())
    yield
    task.cancel()
    engine.jev.save_recordings()


app = FastAPI(title="Jev Alarm Triage", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")


async def handle(msg: dict):
    kind = msg.get("type")
    async with engine.lock:
        if kind == "play":
            engine.playing = not engine.playing and not engine.ended
        elif kind == "step":
            engine.playing = False
            await engine.advance(engine.next_time())
        elif kind == "reset":
            engine.reset()
        elif kind == "scenario":
            engine.mistake = False
            engine.load(msg["id"])
        elif kind == "speed":
            engine.speed = float(msg["value"])
        elif kind == "offline":
            engine.offline = bool(msg["value"])
        elif kind == "mistake":
            engine.mistake = bool(msg["value"])
            engine.reset()
        elif kind == "mode":
            if msg["value"] == "replay" or engine.jev.live_available:
                engine.mode = msg["value"]
        elif kind == "ack":
            engine.ack(msg["id"])
        engine.version += 1


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json(view())
    try:
        while True:
            await handle(await ws.receive_json())
    except WebSocketDisconnect:
        clients.discard(ws)
