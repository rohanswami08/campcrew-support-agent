"""Guardrail tests: the policy must hold even if the model tries to break it."""

import pytest

from agent.store import Store
from agent.tools import ToolExecutor


@pytest.fixture
def tools():
    return ToolExecutor(Store())


def login(tools, email, zip_code):
    assert tools.run("verify_customer", {"email": email, "zip_code": zip_code})["verified"]


MAYA = ("maya.chen@example.com", "94110")
JORDAN = ("jordan.alvarez@example.com", "80302")
PRIYA = ("priya.n@example.com", "10025")
SAM = ("sam.okafor@example.com", "60614")
MARCUS = ("marcus.lee@example.com", "30307")
ELENA = ("elena.rossi@example.com", "98103")


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


def test_cancel_processing_ok_shipped_blocked(tools):
    login(tools, *JORDAN)
    assert tools.run("cancel_order", {"order_id": "CC-10455", "reason": "found locally"})["status"] == "cancelled"
    t2 = ToolExecutor(Store())
    login(t2, *ELENA)
    assert "can't be cancelled" in t2.run("cancel_order", {"order_id": "CC-10490", "reason": "x"})["error"]


def test_address_change_only_before_shipping(tools):
    login(tools, *MARCUS)
    ok = tools.run("update_shipping_address", {"order_id": "CC-10501", "new_address": "55 Park Place NE, Atlanta, GA 30303"})
    assert ok["shipping_address"].startswith("55 Park")
    assert "error" in tools.run("update_shipping_address", {"order_id": "CC-10412", "new_address": "55 Park Place NE, Atlanta, GA 30303"})


def test_changed_mind_window(tools):
    login(tools, *MAYA)
    ok = tools.run("start_return", {"order_id": "CC-10421", "line_ids": ["L1"], "reason": "changed_mind"})
    assert ok["refund_amount"] == 329.00
    late = tools.run("start_return", {"order_id": "CC-10388", "line_ids": ["L1"], "reason": "changed_mind"})
    assert "30 days" in late["error"]


def test_defective_under_warranty_refunds_shipping(tools):
    login(tools, *MAYA)
    out = tools.run("start_return", {"order_id": "CC-10388", "line_ids": ["L1"], "reason": "defective"})
    assert out["refund_amount"] == 45.00 + 6.95


def test_final_sale(tools):
    login(tools, *JORDAN)
    assert "final-sale" in tools.run("start_return", {"order_id": "CC-10299", "line_ids": ["L1"], "reason": "changed_mind"})["error"]
    assert tools.run("start_return", {"order_id": "CC-10299", "line_ids": ["L1"], "reason": "defective"})["return_id"]


def test_refund_over_limit_blocked(tools):
    login(tools, *SAM)
    out = tools.run("start_return", {"order_id": "CC-10477", "line_ids": ["L1"], "reason": "changed_mind"})
    assert "refund_over_limit" in out["error"]
    assert tools.store.actions == []


def test_no_double_return(tools):
    login(tools, *MARCUS)
    assert "can't be returned again" in tools.run("start_return", {"order_id": "CC-10412", "line_ids": ["L1"], "reason": "changed_mind"})["error"]


def test_goodwill_credit_rules(tools):
    login(tools, *PRIYA)
    assert "between" in tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 50, "reason": "late"})["error"]
    assert tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 15, "reason": "late"})["amount"] == 15
    assert "already" in tools.run("issue_goodwill_credit", {"order_id": "CC-10460", "amount": 5, "reason": "late"})["error"]
    assert "not late" in tools.run("issue_goodwill_credit", {"order_id": "CC-10402", "amount": 5, "reason": "x"})["error"]


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
    a.run("cancel_order", {"order_id": "CC-10455", "reason": "x"})
    assert b.store.orders["CC-10455"]["status"] == "processing"
