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
        [tool_use("t2", "cancel_order", {"order_id": "CC-10455", "reason": "customer request"})],
        [text("Please review the card and click Approve.")],
    ])
    agent = SupportAgent(client, Store(), model="test")
    result = agent.respond("cancel my jacket, jordan.alvarez@example.com 80302")
    assert [t["tool"] for t in result.trace] == ["verify_customer", "cancel_order"]
    # the tool only proposed the change: an approval card, no change yet
    assert [p["id"] for p in result.proposals] == ["P1"]
    assert agent.store.actions == []
    approved = agent.decide("P1", approve=True)
    assert approved.reply.startswith("Done. Order CC-10455 is cancelled, and $189.00")
    assert agent.store.actions[0]["type"] == "cancel_order"
    # the outcome is recorded in the conversation so the model knows about it
    assert "clicked Approve" in agent.messages[-2]["content"]
    # tool results are sent back to the model with matching ids
    last_user = client.requests[-1]["messages"][-1]
    assert last_user["content"][0]["tool_use_id"] == "t2"
    assert "Today's date is 2026-09-26" in client.requests[0]["system"]


def test_blocked_tool_is_reported_as_error():
    client = FakeClient([
        [tool_use("t1", "cancel_order", {"order_id": "CC-10455", "reason": "x"})],
        [text("I need to verify you first.")],
    ])
    agent = SupportAgent(client, Store(), model="test")
    agent.respond("cancel CC-10455")
    result_block = client.requests[-1]["messages"][-1]["content"][0]
    assert result_block["is_error"] is True
    assert agent.store.actions == []


def test_runaway_tool_loop_stops():
    client = FakeClient([[tool_use(f"t{i}", "search_products", {"query": "tent"})] for i in range(20)])
    result = SupportAgent(client, Store(), model="test").respond("tents?")
    assert "specialist" in result.reply
    assert len(result.trace) == 8


def test_checker():
    scen = {"expected_actions": [{"type": "cancel_order", "order_id": "CC-10455"}]}
    assert check(scen, [{"type": "cancel_order", "order_id": "CC-10455"}], [])[0]
    assert not check(scen, [], [])[0]
    assert not check(scen, [{"type": "cancel_order", "order_id": "CC-10455"},
                            {"type": "goodwill_credit", "order_id": "CC-10455", "amount": 5}], [])[0]
    optional = {"allowed_actions": [{"type": "goodwill_credit", "order_id": "X", "max_amount": 15}]}
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


def test_sidebar_reflects_this_conversation(monkeypatch):
    import app as webapp

    fake = FakeClient([
        [tool_use("t1", "verify_customer", {"email": "jordan.alvarez@example.com", "zip_code": "80302"})],
        [tool_use("t2", "cancel_order", {"order_id": "CC-10455", "reason": "customer request"})],
        [text("Cancelled.")],
    ])
    monkeypatch.setattr(webapp, "get_client", lambda: fake)
    c = TestClient(webapp.app)

    start = {a["name"]: a for a in c.get("/api/demo-accounts").json()["accounts"]}
    assert start["Jordan Alvarez"]["orders"][0] == {"id": "CC-10455", "status": "processing", "changed": False}

    chat = c.post("/api/chat", json={"session_id": "session-sidebar", "message": "yes cancel"}).json()
    jordan = next(a for a in chat["state"]["accounts"] if a["name"] == "Jordan Alvarez")
    assert jordan["orders"][0]["status"] == "processing"            # proposed, not done
    state = c.post("/api/decide", json={"session_id": "session-sidebar", "proposal_id": chat["proposals"][0]["id"],
                                        "approve": True}).json()["state"]
    jordan = next(a for a in state["accounts"] if a["name"] == "Jordan Alvarez")
    assert jordan["orders"][0] == {"id": "CC-10455", "status": "cancelled", "changed": True}
    assert not any(o["changed"] for a in state["accounts"] if a["name"] != "Jordan Alvarez" for o in a["orders"])
    # other visitors still see the original store
    assert c.get("/api/demo-accounts").json()["accounts"][1]["orders"][0]["status"] == "processing"


def test_web_app_without_key_returns_503(monkeypatch):
    import app as webapp

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(webapp, "_client", None)
    c = TestClient(webapp.app)
    r = c.post("/api/chat", json={"session_id": "session-nokey", "message": "hello"})
    assert r.status_code == 503


def _credit_chat(monkeypatch, session_id):
    """Priya's late order: the agent proposes a $15 credit. Returns (client, proposal_id)."""
    import app as webapp

    fake = FakeClient([
        [tool_use("t1", "verify_customer", {"email": "priya.n@example.com", "zip_code": "10025"})],
        [tool_use("t2", "issue_goodwill_credit", {"order_id": "CC-10460", "amount": 15, "reason": "late"})],
        [text("I've put a $15 credit on the screen for you to approve.")],
    ])
    monkeypatch.setattr(webapp, "get_client", lambda: fake)
    c = TestClient(webapp.app)
    data = c.post("/api/chat", json={"session_id": session_id, "message": "my backpack is late"}).json()
    assert data["proposals"][0]["summary"].startswith("Add a $15.00 store credit")
    assert data["state"]["actions"] == []
    return c, data["proposals"][0]["id"]


def test_api_cancel_makes_no_change(monkeypatch):
    c, pid = _credit_chat(monkeypatch, "session-cancel")
    out = c.post("/api/decide", json={"session_id": "session-cancel", "proposal_id": pid, "approve": False}).json()
    assert out["ok"] and "didn't make that change" in out["reply"]
    assert out["state"]["actions"] == []


def test_api_double_approve_issues_one_credit(monkeypatch):
    c, pid = _credit_chat(monkeypatch, "session-double")
    body = {"session_id": "session-double", "proposal_id": pid, "approve": True}
    first, second = c.post("/api/decide", json=body).json(), c.post("/api/decide", json=body).json()
    assert first["ok"] and not second["ok"]
    assert [a["type"] for a in second["state"]["actions"]] == ["goodwill_credit"]


def test_api_cannot_approve_from_another_session(monkeypatch):
    c, pid = _credit_chat(monkeypatch, "session-owner")
    r = c.post("/api/decide", json={"session_id": "session-stranger", "proposal_id": pid, "approve": True})
    assert r.status_code == 404


def test_eval_customer_can_click_buttons():
    """The simulated customer's APPROVE reply clicks the card instead of being sent as a message."""
    from evals.run_evals import simulate

    client = FakeClient([
        [text("Hi, cancel my jacket please. jordan.alvarez@example.com 80302")],       # customer
        [tool_use("t1", "verify_customer", {"email": "jordan.alvarez@example.com", "zip_code": "80302"})],
        [tool_use("t2", "cancel_order", {"order_id": "CC-10455", "reason": "found locally"})],
        [text("Please review the card and click Approve.")],                           # agent
        [text("APPROVE")],                                                             # customer clicks
        [text("###DONE###")],
    ])
    scenario = {"id": "x", "category": "happy_path", "customer": "...",
                "expected_actions": [{"type": "cancel_order", "order_id": "CC-10455"}]}
    run = simulate(client, scenario, "test")
    assert run["passed"], run["problems"]
    # the customer saw the exact card text
    assert "Approval card on screen" in client.requests[4]["messages"][-1]["content"]
