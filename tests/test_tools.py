"""Guardrail tests: the policy must hold even if the model tries to break it."""

import pytest

from agent.policy import PolicyViolation, check_policy
from agent.store import Store
from agent.tools import ToolExecutor


@pytest.fixture
def tools():
    return ToolExecutor(Store())


def login(tools, email, zip_code):
    assert tools.run("verify_customer", {"email": email, "zip_code": zip_code})["verified"]


def act(tools, name, args):
    """Propose a change and, if the policy allows it, have the customer approve it."""
    proposal = tools.run(name, args)
    if "error" in proposal:
        return proposal
    outcome = tools.decide(proposal["proposal_id"], approve=True)
    return outcome["result"] if outcome["ok"] else {"error": outcome["error"]}


MAYA = ("maya.chen@example.com", "94110")
JORDAN = ("jordan.alvarez@example.com", "80302")
PRIYA = ("priya.n@example.com", "10025")
SAM = ("sam.okafor@example.com", "60614")
MARCUS = ("marcus.lee@example.com", "30307")
ELENA = ("elena.rossi@example.com", "98103")


# ---------- the policy rules ----------

def test_orders_require_verification(tools):
    assert "error" in tools.run("get_order", {"order_id": "CC-10421"})
    assert "error" in tools.run("cancel_order", {"order_id": "CC-10455", "reason": "x"})


def test_wrong_zip_fails_and_locks_after_three(tools):
    for _ in range(3):
        assert "error" in tools.run("verify_customer", {"email": MAYA[0], "zip_code": "00000"})
    out = tools.run("verify_customer", {"email": MAYA[0], "zip_code": MAYA[1]})
    assert "Too many" in out["error"]


def test_cannot_see_other_customers_order(tools):
    login(tools, *MAYA)
    out = tools.run("get_order", {"order_id": "CC-10455"})  # Jordan's
    assert "No order" in out["error"] and "Jordan" not in str(out)
    out = tools.run("cancel_order", {"order_id": "CC-10455", "reason": "x"})
    assert "No order" in out["error"]


def test_cancel_processing_ok_shipped_blocked(tools):
    login(tools, *JORDAN)
    assert act(tools, "cancel_order", {"order_id": "CC-10455", "reason": "found locally"})["status"] == "cancelled"
    t2 = ToolExecutor(Store())
    login(t2, *ELENA)
    assert "can't be cancelled" in t2.run("cancel_order", {"order_id": "CC-10490", "reason": "x"})["error"]


def test_address_change_only_before_shipping(tools):
    login(tools, *MARCUS)
    ok = act(tools, "update_shipping_address", {"order_id": "CC-10501", "new_address": "55 Park Place NE, Atlanta, GA 30303"})
    assert ok["shipping_address"].startswith("55 Park")
    assert "error" in tools.run("update_shipping_address", {"order_id": "CC-10412", "new_address": "55 Park Place NE, Atlanta, GA 30303"})


def test_changed_mind_window(tools):
    login(tools, *MAYA)
    ok = act(tools, "start_return", {"order_id": "CC-10421", "line_ids": ["L1"], "reason": "changed_mind"})
    assert ok["refund_amount"] == 329.00
    late = tools.run("start_return", {"order_id": "CC-10388", "line_ids": ["L1"], "reason": "changed_mind"})
    assert "30 days" in late["error"]


def test_defective_under_warranty_refunds_shipping(tools):
    login(tools, *MAYA)
    out = act(tools, "start_return", {"order_id": "CC-10388", "line_ids": ["L1"], "reason": "defective"})
    assert out["refund_amount"] == 45.00 + 6.95


def test_final_sale(tools):
    login(tools, *JORDAN)
    assert "final sale" in tools.run("start_return", {"order_id": "CC-10299", "line_ids": ["L1"], "reason": "changed_mind"})["error"]
    assert act(tools, "start_return", {"order_id": "CC-10299", "line_ids": ["L1"], "reason": "defective"})["return_id"]


def test_refund_over_limit_blocked(tools):
    login(tools, *SAM)
    out = tools.run("start_return", {"order_id": "CC-10477", "line_ids": ["L1"], "reason": "changed_mind"})
    assert "refund_over_limit" in out["error"]
    assert tools.store.actions == [] and tools.proposals == {}


def test_no_double_return(tools):
    login(tools, *MARCUS)
    assert "can't be returned again" in tools.run("start_return", {"order_id": "CC-10412", "line_ids": ["L1"], "reason": "changed_mind"})["error"]


def test_goodwill_credit_rules(tools):
    login(tools, *PRIYA)
    assert "at most $15" in tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 50, "reason": "late"})["error"]
    assert act(tools, "issue_goodwill_credit", {"order_id": "CC-10460", "amount": 15, "reason": "late"})["amount"] == 15
    assert "already" in tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 5, "reason": "late"})["error"]
    assert "isn't late" in tools.run("issue_goodwill_credit", {"order_id": "CC-10402", "amount": 5, "reason": "x"})["error"]


def test_check_policy_is_the_single_gate():
    store = Store()
    with pytest.raises(PolicyViolation, match="isn't verified"):
        check_policy(store, None, "cancel_order", {"order_id": "CC-10455"})
    with pytest.raises(PolicyViolation, match="No order"):
        check_policy(store, "C001", "cancel_order", {"order_id": "CC-10455"})  # Maya, Jordan's order
    plan = check_policy(store, "C002", "cancel_order", {"order_id": "cc-10455", "reason": "x"})
    assert plan.order_id == "CC-10455" and "$189.00" in plan.summary


def test_escalation_ticket(tools):
    login(tools, *PRIYA)
    out = tools.run("escalate_to_human", {"category": "lost_package", "summary": "not received", "order_id": "CC-10402"})
    assert out["ticket_id"].startswith("TKT-")
    assert tools.store.actions[-1] == {"type": "escalate", "category": "lost_package", "order_id": "CC-10402"}


def test_product_search_needs_no_login(tools):
    names = [r["name"] for r in tools.run("search_products", {"query": "rain jacket"})["results"]]
    assert "Cascade Rain Jacket" in names


def test_bad_arguments_return_error(tools):
    assert "Invalid arguments" in tools.run("verify_customer", {"email": "x"})["error"]
    assert "Unknown tool" in tools.run("delete_database", {})["error"]


def test_sessions_are_isolated():
    a, b = ToolExecutor(Store()), ToolExecutor(Store())
    login(a, *JORDAN)
    act(a, "cancel_order", {"order_id": "CC-10455", "reason": "x"})
    assert b.store.orders["CC-10455"]["status"] == "processing"


# ---------- the approval step ----------

def test_proposing_changes_nothing(tools):
    login(tools, *JORDAN)
    out = tools.run("cancel_order", {"order_id": "CC-10455", "reason": "found locally"})
    assert out["status"] == "awaiting_customer_approval"
    assert out["summary"] == ("Cancel order CC-10455 (Cascade Rain Jacket) and refund $189.00 "
                              "to your original payment method")
    assert tools.store.orders["CC-10455"]["status"] == "processing"
    assert tools.store.actions == []


def test_cancel_makes_no_change(tools):
    login(tools, *JORDAN)
    pid = tools.run("cancel_order", {"order_id": "CC-10455", "reason": "x"})["proposal_id"]
    assert tools.decide(pid, approve=False)["status"] == "cancelled"
    assert tools.store.orders["CC-10455"]["status"] == "processing"
    assert tools.store.actions == []
    # a cancelled card can't be approved afterwards
    late_approve = tools.decide(pid, approve=True)
    assert not late_approve["ok"] and tools.store.actions == []


def test_approve_twice_issues_one_credit(tools):
    login(tools, *PRIYA)
    pid = tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 15, "reason": "late"})["proposal_id"]
    assert tools.decide(pid, approve=True)["ok"]
    second = tools.decide(pid, approve=True)
    assert not second["ok"] and "already approved" in second["error"]
    assert len(tools.store.credits) == 1


def test_two_credit_cards_approved_issue_one_credit(tools):
    """Two separate proposals for the same credit: the re-check on Approve stops the second."""
    login(tools, *PRIYA)
    p1 = tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 15, "reason": "late"})["proposal_id"]
    p2 = tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 10, "reason": "late"})["proposal_id"]
    assert tools.decide(p1, approve=True)["ok"]
    blocked = tools.decide(p2, approve=True)
    assert not blocked["ok"] and "already issued" in blocked["error"]
    assert len(tools.store.credits) == 1


def test_approval_rechecks_policy_against_current_state(tools):
    login(tools, *JORDAN)
    pid = tools.run("cancel_order", {"order_id": "CC-10455", "reason": "x"})["proposal_id"]
    tools.store.orders["CC-10455"]["status"] = "shipped"   # the order ships before the customer clicks
    out = tools.decide(pid, approve=True)
    assert not out["ok"] and "can't be cancelled" in out["error"]
    assert tools.store.actions == []


def test_approval_rechecks_ownership(tools):
    login(tools, *JORDAN)
    pid = tools.run("cancel_order", {"order_id": "CC-10455", "reason": "x"})["proposal_id"]
    login(tools, *MAYA)                                    # someone else verifies in the same chat
    out = tools.decide(pid, approve=True)
    assert not out["ok"] and tools.store.actions == []


def test_unknown_proposal(tools):
    assert not tools.decide("P99", approve=True)["ok"]
