"""Tools the agent can call.

Read-only tools (verify, look up orders, search products) run immediately.

Tools that would change something (cancel, change address, start a return,
issue credit) never change anything themselves. They run check_policy() and,
if it passes, create a *proposal*: an exact description of the change that the
customer sees on an Approve / Cancel card. The change happens only in
`decide()`, after the customer clicks Approve, and only if check_policy()
passes again at that moment.

Escalating to a human is the one write that happens right away: it moves no
money and changes no order, it only opens a ticket.
"""

from __future__ import annotations

from datetime import date

from .policy import RETURN_REASONS, PolicyViolation, check_policy
from .store import Store

ESCALATION_CATEGORIES = ("refund_over_limit", "lost_package", "customer_request", "other")
CHANGE_ACTIONS = ("cancel_order", "update_shipping_address", "start_return", "issue_goodwill_credit")

_PROPOSES = ("Does NOT make the change: it shows the customer an approval card with the exact change and "
             "Approve / Cancel buttons. Nothing happens unless they click Approve.")

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "verify_customer",
        "description": "Verify the customer's identity using the email address and ZIP code on their account. "
                       "Must succeed before any order can be looked up or changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "zip_code": {"type": "string", "description": "5-digit ZIP code on the account"},
            },
            "required": ["email", "zip_code"],
        },
    },
    {
        "name": "list_orders",
        "description": "List the verified customer's orders with status and totals.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_order",
        "description": "Get full details of one of the verified customer's orders: items (with line_ids), "
                       "status, dates, address, tracking, and any open returns.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "e.g. CC-10421"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "search_products",
        "description": "Search the product catalog by keyword. Returns price, stock, description, and whether "
                       "the item is final sale. Does not require verification.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "cancel_order",
        "description": "Propose cancelling an entire order that has not shipped yet. " + _PROPOSES,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "update_shipping_address",
        "description": "Propose changing the shipping address of an order that has not shipped yet. "
                       "Get the full new address first. " + _PROPOSES,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "new_address": {"type": "string", "description": "Full address: street, city, state, ZIP"},
            },
            "required": ["order_id", "new_address"],
        },
    },
    {
        "name": "start_return",
        "description": "Propose a return for one or more delivered items on an order. " + _PROPOSES,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "line_ids": {"type": "array", "items": {"type": "string"},
                             "description": "line_ids of the items being returned (from get_order)"},
                "reason": {"type": "string", "enum": list(RETURN_REASONS)},
            },
            "required": ["order_id", "line_ids", "reason"],
        },
    },
    {
        "name": "issue_goodwill_credit",
        "description": "Propose a one-time store credit (max $15) on an order that is past its estimated "
                       "delivery date. " + _PROPOSES,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "amount", "reason"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Open a ticket for a human specialist (takes effect immediately). Use for refunds over "
                       "$500, packages marked delivered but not received, explicit requests for a human, or "
                       "anything outside policy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(ESCALATION_CATEGORIES)},
                "summary": {"type": "string", "description": "What the customer needs, for the specialist"},
                "order_id": {"type": "string"},
            },
            "required": ["category", "summary"],
        },
    },
]


class ToolError(Exception):
    """A validation failure, reported back to the model."""


class ToolExecutor:
    """Runs tool calls for one conversation. Holds who is verified and the pending proposals."""

    def __init__(self, store: Store):
        self.store = store
        self.verified_customer_id: str | None = None
        self._failed_verifications = 0
        self.proposals: dict[str, dict] = {}   # proposal_id -> proposal
        self._next_proposal = 1

    # ---------- entry point for the model ----------
    def run(self, name: str, args: dict) -> dict:
        if name in CHANGE_ACTIONS:
            return self._propose(name, args)
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return handler(**args)
        except (ToolError, PolicyViolation) as e:
            return {"error": str(e)}
        except TypeError as e:  # wrong/missing arguments from the model
            return {"error": f"Invalid arguments: {e}"}

    # ---------- proposals and approval ----------
    def _propose(self, action: str, args: dict) -> dict:
        """Check the policy and, if it passes, create a proposal for the customer to approve."""
        try:
            plan = check_policy(self.store, self.verified_customer_id, action, args)
        except PolicyViolation as e:
            return {"error": str(e)}
        pid = f"P{self._next_proposal}"
        self._next_proposal += 1
        self.proposals[pid] = {
            "id": pid, "action": action, "args": dict(args), "summary": plan.summary,
            "customer_id": self.verified_customer_id, "status": "pending",
        }
        return {
            "status": "awaiting_customer_approval",
            "proposal_id": pid,
            "summary": plan.summary,
            "note": "Nothing has changed yet. The customer now sees this exact change with Approve and Cancel "
                    "buttons. Ask them to review it; do not say it is done.",
        }

    def pending_proposals(self) -> list[dict]:
        return [{"id": p["id"], "summary": p["summary"]} for p in self.proposals.values() if p["status"] == "pending"]

    def decide(self, proposal_id: str, approve: bool) -> dict:
        """Called when the customer clicks Approve or Cancel. The only path that changes an order.

        - A proposal can be decided once. Clicking Approve twice does nothing the second time.
        - On Approve, the policy is checked again against the store as it is *now*, by the
          customer verified *now*, before anything changes.
        """
        p = self.proposals.get(proposal_id)
        if p is None:
            return {"ok": False, "error": "That request wasn't found."}
        if p["status"] != "pending":
            return {"ok": False, "error": f"That request was already {p['status']}.", "summary": p["summary"]}

        if not approve:
            p["status"] = "cancelled"
            return {"ok": True, "status": "cancelled", "summary": p["summary"]}

        p["status"] = "approved"  # claim it before doing anything, so it can never run twice
        try:
            if self.verified_customer_id != p["customer_id"]:
                raise PolicyViolation("The verified customer changed since this was proposed.")
            plan = check_policy(self.store, self.verified_customer_id, p["action"], p["args"])
        except PolicyViolation as e:
            p["status"] = "blocked"
            return {"ok": False, "error": str(e), "summary": p["summary"]}
        result = self._execute(plan)
        return {"ok": True, "status": "approved", "action": p["action"], "summary": p["summary"], "result": result}

    def _execute(self, plan) -> dict:
        s, a = self.store, plan.params
        if plan.action == "cancel_order":
            return s.cancel_order(plan.order_id, a["reason"])
        if plan.action == "update_shipping_address":
            return s.update_address(plan.order_id, a["address"])
        if plan.action == "start_return":
            return s.create_return(plan.order_id, a["line_ids"], a["reason"], a["refund"])
        if plan.action == "issue_goodwill_credit":
            order = s.orders[plan.order_id]
            return s.add_credit(order["customer_id"], plan.order_id, a["amount"], a["reason"])
        raise ValueError(plan.action)

    # ---------- helpers ----------
    def _require_verified(self) -> str:
        if self.verified_customer_id is None:
            raise ToolError("Customer is not verified. Ask for the account email and ZIP code first.")
        return self.verified_customer_id

    def _own_order(self, order_id: str) -> dict:
        """Return the order only if it belongs to the verified customer (same rule as check_policy)."""
        cid = self._require_verified()
        order = self.store.orders.get(order_id.strip().upper())
        if order is None or order["customer_id"] != cid:
            raise ToolError(f"No order {order_id} found on this customer's account.")
        return order

    def _days_since_delivery(self, order: dict) -> int | None:
        if not order.get("delivered"):
            return None
        return (self.store.today - date.fromisoformat(order["delivered"])).days

    def _item_view(self, item: dict) -> dict:
        p = self.store.products[item["sku"]]
        view = {"line_id": item["line_id"], "name": p["name"], "sku": item["sku"], "qty": item["qty"],
                "unit_price": item["unit_price"], "status": item["status"], "final_sale": p["final_sale"]}
        if "size" in item:
            view["size"] = item["size"]
        return view

    # ---------- read-only tools ----------
    def _tool_verify_customer(self, email: str, zip_code: str) -> dict:
        if self._failed_verifications >= 3:
            raise ToolError("Too many failed verification attempts in this conversation. "
                            "Offer to escalate to a human specialist instead.")
        customer = self.store.find_customer(email, zip_code)
        if customer is None:
            self._failed_verifications += 1
            raise ToolError("Verification failed: that email and ZIP code don't match an account.")
        self.verified_customer_id = customer["id"]
        return {"verified": True, "customer_name": customer["name"]}

    def _tool_list_orders(self) -> dict:
        cid = self._require_verified()
        orders = [o for o in self.store.orders.values() if o["customer_id"] == cid]
        return {"orders": [
            {"order_id": o["id"], "placed": o["placed"], "status": o["status"],
             "total": self.store.order_total(o),
             "items": [self.store.products[i["sku"]]["name"] for i in o["items"]]}
            for o in sorted(orders, key=lambda o: o["placed"], reverse=True)
        ]}

    def _tool_get_order(self, order_id: str) -> dict:
        o = self._own_order(order_id)
        today = self.store.today
        late = (o["status"] in ("processing", "shipped")
                and today > date.fromisoformat(o["estimated_delivery"]))
        result = {
            "order_id": o["id"], "status": o["status"], "placed": o["placed"],
            "estimated_delivery": o["estimated_delivery"], "delivered": o["delivered"],
            "days_since_delivery": self._days_since_delivery(o),
            "is_late": late,
            "shipping_address": o["shipping_address"], "shipping_cost": o["shipping_cost"],
            "total": self.store.order_total(o),
            "items": [self._item_view(i) for i in o["items"]],
            "open_returns": [r for r in self.store.returns if r["order_id"] == o["id"]],
            "goodwill_credit_issued": any(c["order_id"] == o["id"] for c in self.store.credits),
            "pending_approvals": [p["summary"] for p in self.proposals.values()
                                  if p["status"] == "pending" and p["args"].get("order_id", "").upper() == o["id"]],
            "today": today.isoformat(),
        }
        if "tracking" in o:
            result["tracking"] = o["tracking"]
        return result

    def _tool_search_products(self, query: str) -> dict:
        words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
        scored = []
        for p in self.store.products.values():
            text = f"{p['name']} {p['category']} {p['description']}".lower()
            score = sum(w.rstrip("s") in text for w in words)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda s: -s[0])
        return {"results": [
            {k: p[k] for k in ("sku", "name", "price", "in_stock", "final_sale", "description")}
            for _, p in scored[:5]
        ]}

    # ---------- the one immediate write ----------
    def _tool_escalate_to_human(self, category: str, summary: str, order_id: str | None = None) -> dict:
        if category not in ESCALATION_CATEGORIES:
            raise ToolError(f"category must be one of {ESCALATION_CATEGORIES}")
        if order_id:
            order_id = self._own_order(order_id)["id"] if self.verified_customer_id else None
        ticket = self.store.create_ticket(self.verified_customer_id, category, summary, order_id)
        return {**ticket, "expected_response": "A specialist will reply by email within 2 business days."}
