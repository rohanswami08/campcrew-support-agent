"""Agent loop, eval checker, and web server, tested with a scripted fake Claude client."""

import copy
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent import Store, SupportAgent
from evals.checker import check


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


class FakeClient:
    """Returns pre-scripted responses in order and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # agent mutates its history later
        return SimpleNamespace(content=self.responses.pop(0))


def test_agent_runs_tools_then_answers():
    client = FakeClient([
        [tool_use("t1", "verify_customer", {"email": "jordan.alvarez@example.com", "zip_code": "80302"})],
        [tool_use("t2", "cancel_order", {"order_id": "FW-10455", "reason": "customer request"})],
        [text("Done! Your order is cancelled.")],
    ])
    agent = SupportAgent(client, Store(), model="test")
    result = agent.respond("cancel my jacket, jordan.alvarez@example.com 80302")
    assert result.reply == "Done! Your order is cancelled."
    assert [t["tool"] for t in result.trace] == ["verify_customer", "cancel_order"]
    assert agent.store.actions[0]["type"] == "cancel_order"
    # tool results are sent back to the model with matching ids
    last_user = client.requests[-1]["messages"][-1]
    assert last_user["content"][0]["tool_use_id"] == "t2"
    assert "Today's date is 2026-09-26" in client.requests[0]["system"]


def test_blocked_tool_is_reported_as_error():
    client = FakeClient([
        [tool_use("t1", "cancel_order", {"order_id": "FW-10455", "reason": "x"})],
        [text("I need to verify you first.")],
    ])
    agent = SupportAgent(client, Store(), model="test")
    agent.respond("cancel FW-10455")
    result_block = client.requests[-1]["messages"][-1]["content"][0]
    assert result_block["is_error"] is True
    assert agent.store.actions == []


def test_runaway_tool_loop_stops():
    client = FakeClient([[tool_use(f"t{i}", "search_products", {"query": "tent"})] for i in range(20)])
    result = SupportAgent(client, Store(), model="test").respond("tents?")
    assert "specialist" in result.reply
    assert len(result.trace) == 8


def test_checker():
    scen = {"expected_actions": [{"type": "cancel_order", "order_id": "FW-10455"}]}
    assert check(scen, [{"type": "cancel_order", "order_id": "FW-10455"}], [])[0]
    assert not check(scen, [], [])[0]
    assert not check(scen, [{"type": "cancel_order", "order_id": "FW-10455"},
                            {"type": "goodwill_credit", "order_id": "FW-10455", "amount": 5}], [])[0]
    optional = {"expected_actions": [{"type": "goodwill_credit", "order_id": "X", "max_amount": 15}], "optional_actions": True}
    assert check(optional, [], [])[0]
    assert not check(optional, [{"type": "goodwill_credit", "order_id": "X", "amount": 20}], [])[0]
    assert not check({"forbidden_in_replies": ["Pearl St"]}, [], ["Ships to 2150 Pearl St"])[0]


def test_web_app(monkeypatch):
    import app as webapp

    fake = FakeClient([[text("Hi there!")], [text("Second reply")]])
    monkeypatch.setattr(webapp, "get_client", lambda: fake)
    c = TestClient(webapp.app)

    assert c.get("/").status_code == 200
    assert len(c.get("/api/demo-accounts").json()["accounts"]) == 6
    r = c.post("/api/chat", json={"session_id": "session-abc", "message": "hello"})
    assert r.status_code == 200 and r.json()["reply"] == "Hi there!"
    r = c.post("/api/chat", json={"session_id": "session-abc", "message": "again"})
    assert len(fake.requests[-1]["messages"]) == 3  # history kept within a session
    assert c.post("/api/chat", json={"session_id": "short", "message": "x"}).status_code == 422
    assert c.post("/api/reset", json={"session_id": "session-abc"}).json()["ok"]


def test_web_app_without_key_returns_503(monkeypatch):
    import app as webapp

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(webapp, "_client", None)
    c = TestClient(webapp.app)
    r = c.post("/api/chat", json={"session_id": "session-nokey", "message": "hello"})
    assert r.status_code == 503
