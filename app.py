"""Web server for the live demo.

Each browser tab gets its own session with a fresh copy of the store, so
visitors can cancel orders and start returns without affecting anyone else.
Abuse limits (per-IP rate limit, per-session cap, global daily cap) keep a
public demo from running up the API bill.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import Store, SupportAgent

SESSION_TTL_SECONDS = 30 * 60
MAX_SESSIONS = 500
MAX_MESSAGES_PER_SESSION = 30
IP_LIMIT, IP_WINDOW_SECONDS = 20, 5 * 60
DAILY_MESSAGE_CAP = int(os.environ.get("DAILY_MESSAGE_CAP", "1000"))

app = FastAPI(title="CampCrew Support Agent")
_client = None
_sessions: dict[str, dict] = {}
_ip_hits: dict[str, deque] = defaultdict(deque)
_daily = {"date": date.today(), "count": 0}
_lock = threading.Lock()


def get_client():
    """Create the Anthropic client lazily so the app (and tests) can start without a key."""
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(503, "The demo isn't configured with an API key yet.")
        import anthropic
        _client = anthropic.Anthropic()
    return _client


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=1000)


class ResetRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "?")


def _check_limits(ip: str) -> None:
    now = time.time()
    with _lock:
        hits = _ip_hits[ip]
        while hits and now - hits[0] > IP_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= IP_LIMIT:
            raise HTTPException(429, "You're sending messages quickly. Please wait a few minutes.")
        if _daily["date"] != date.today():
            _daily.update(date=date.today(), count=0)
        if _daily["count"] >= DAILY_MESSAGE_CAP:
            raise HTTPException(429, "The demo has hit its daily message limit. Please try again tomorrow.")
        hits.append(now)
        _daily["count"] += 1


def _get_session(session_id: str) -> dict:
    now = time.time()
    with _lock:
        for sid in [s for s, v in _sessions.items() if now - v["last_used"] > SESSION_TTL_SECONDS]:
            del _sessions[sid]
        session = _sessions.get(session_id)
        if session is None:
            if len(_sessions) >= MAX_SESSIONS:
                oldest = min(_sessions, key=lambda s: _sessions[s]["last_used"])
                del _sessions[oldest]
            session = {"agent": None, "count": 0, "lock": threading.Lock(), "last_used": now}
            _sessions[session_id] = session
        session["last_used"] = now
        return session


def _accounts(store: Store) -> list[dict]:
    """Demo customers and their orders as they stand in this store.

    `changed` marks orders whose status differs from the starting data, so the
    sidebar can highlight what the agent just did.
    """
    seed = Store()
    by_customer = defaultdict(list)
    for o in store.orders.values():
        original = seed.orders[o["id"]]
        notes = []
        if any(i["status"] == "return_started" != s["status"] for i, s in zip(o["items"], original["items"])):
            notes.append("return started")
        if o["shipping_address"] != original["shipping_address"]:
            notes.append("address updated")
        if any(c["order_id"] == o["id"] for c in store.credits):
            notes.append("credit issued")
        if any(t["order_id"] == o["id"] for t in store.tickets):
            notes.append("escalated")
        by_customer[o["customer_id"]].append({
            "id": o["id"],
            "status": ", ".join([o["status"], *notes]),
            "changed": o["status"] != original["status"] or bool(notes),
        })
    return [{"name": c["name"], "email": c["email"], "zip": c["zip"], "orders": by_customer[c["id"]]}
            for c in store.customers.values()]


def _state_snapshot(agent: SupportAgent) -> dict:
    return {
        "verified_customer": (agent.store.customers[agent.tools.verified_customer_id]["name"]
                              if agent.tools.verified_customer_id else None),
        "actions": agent.store.actions,
        "accounts": _accounts(agent.store),
    }


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    session = _get_session(req.session_id)
    with session["lock"]:
        if session["count"] >= MAX_MESSAGES_PER_SESSION:
            raise HTTPException(429, "This conversation is at its message limit. Hit Reset to start over.")
        _check_limits(_client_ip(request))
        if session["agent"] is None:
            session["agent"] = SupportAgent(get_client())
        agent: SupportAgent = session["agent"]
        session["count"] += 1
        history_len = len(agent.messages)
        try:
            result = agent.respond(req.message.strip())
        except Exception as e:  # API outage, bad key, etc.
            del agent.messages[history_len:]  # roll back the half-finished turn
            raise HTTPException(502, f"The agent couldn't respond ({type(e).__name__}). Please try again.")
        return {"reply": result.reply, "trace": result.trace, "state": _state_snapshot(agent)}


@app.post("/api/reset")
def reset(req: ResetRequest):
    with _lock:
        _sessions.pop(req.session_id, None)
    return {"ok": True}


@app.get("/api/demo-accounts")
def demo_accounts():
    """Sample logins shown in the UI so visitors can try real scenarios."""
    store = Store()
    return {"today": store.today.isoformat(), "accounts": _accounts(store)}


@app.get("/healthz")
def healthz():
    return {"ok": True}


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
