"""Tools the agent can call, plus the code-level guardrails behind them.

Design choice: the policy is written in the system prompt *and* enforced here.
The prompt teaches the model what to do; these checks guarantee that even a
confused or manipulated model can't cancel a shipped order, refund someone
else's order, or hand out a $500 credit. When a check fails, the tool returns
an error with the reason so the model can explain it to the customer.
"""

from __future__ import annotations

from datetime import date

from .store import Store

CHANGED_MIND_WINDOW_DAYS = 30
WARRANTY_WINDOW_DAYS = 365
MAX_AUTO_REFUND = 500.00
MAX_GOODWILL_CREDIT = 15.00
RETURN_REASONS = ("changed_mind", "defective", "wrong_item")
ESCALATION_CATEGORIES = ("refund_over_limit", "lost_package", "customer_request", "other")


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
        "description": "Cancel an entire order that has not shipped yet. Only call after the customer has "
                       "explicitly confirmed.",
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
        "description": "Change the shipping address of an order that has not shipped yet. Only call after "
                       "the customer has explicitly confirmed the full new address.",
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
        "description": "Start a return for one or more delivered items on an order and generate a return "
                       "label. Only call after the customer has explicitly confirmed.",
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
        "description": "Issue a one-time store credit (max $15) on an order that is past its estimated "
                       "delivery date. Only call after the customer has accepted the offer.",
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
        "description": "Open a ticket for a human specialist. Use for refunds over $500, packages marked "
                       "delivered but not received, explicit requests for a human, or anything outside policy.",
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
    """A policy or validation failure, reported back to the model."""


class ToolExecutor:
    """Runs tool calls for one conversation. Holds who has been verified."""

    def __init__(self, store: Store):
        self.store = store
        self.verified_customer_id: str | None = None
        self._failed_verifications = 0

    # ---------- entry point ----------
    def run(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return handler(**args)
        except ToolError as e:
            return {"error": str(e)}
        except TypeError as e:  # wrong/missing arguments from the model
            return {"error": f"Invalid arguments: {e}"}

    # ---------- helpers ----------
    def _require_verified(self) -> str:
        if self.verified_customer_id is None:
            raise ToolError("Customer is not verified. Ask for the account email and ZIP code first.")
        return self.verified_customer_id

    def _own_order(self, order_id: str) -> dict:
        """Return the order only if it belongs to the verified customer.

        Orders belonging to someone else get the same 'not found' message as
        orders that don't exist, so the agent can't leak their existence.
        """
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

    # ---------- tools ----------
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

    def _tool_cancel_order(self, order_id: str, reason: str) -> dict:
        o = self._own_order(order_id)
        if o["status"] != "processing":
            raise ToolError(f"Order {o['id']} is '{o['status']}' and can't be cancelled. "
                            "Only orders that haven't shipped can be cancelled.")
        return self.store.cancel_order(o["id"], reason)

    def _tool_update_shipping_address(self, order_id: str, new_address: str) -> dict:
        o = self._own_order(order_id)
        if o["status"] != "processing":
            raise ToolError(f"Order {o['id']} is '{o['status']}'; the address can only be changed before it ships.")
        if len(new_address.strip()) < 10:
            raise ToolError("Address looks incomplete. Get the full street, city, state and ZIP.")
        return self.store.update_address(o["id"], new_address.strip())

    def _tool_start_return(self, order_id: str, line_ids: list[str], reason: str) -> dict:
        o = self._own_order(order_id)
        if reason not in RETURN_REASONS:
            raise ToolError(f"reason must be one of {RETURN_REASONS}")
        if not line_ids:
            raise ToolError("Specify at least one line_id to return.")
        items = {i["line_id"]: i for i in o["items"]}
        days = self._days_since_delivery(o)
        if days is None:
            raise ToolError(f"Order {o['id']} hasn't been delivered, so nothing on it can be returned yet.")

        refund = 0.0
        for lid in set(line_ids):
            item = items.get(lid)
            if item is None:
                raise ToolError(f"No line {lid} on order {o['id']}.")
            name = self.store.products[item["sku"]]["name"]
            if item["status"] != "delivered":
                raise ToolError(f"{name} ({lid}) is '{item['status']}' and can't be returned again.")
            final_sale = self.store.products[item["sku"]]["final_sale"]
            if reason == "changed_mind":
                if final_sale:
                    raise ToolError(f"{name} is a final-sale item. It can only be returned if defective or wrong.")
                if days > CHANGED_MIND_WINDOW_DAYS:
                    raise ToolError(f"{name} was delivered {days} days ago; changed-mind returns are only "
                                    f"accepted within {CHANGED_MIND_WINDOW_DAYS} days.")
            elif days > WARRANTY_WINDOW_DAYS:
                raise ToolError(f"{name} was delivered {days} days ago, outside the "
                                f"{WARRANTY_WINDOW_DAYS}-day warranty window.")
            refund += item["unit_price"] * item["qty"]

        if reason in ("defective", "wrong_item"):
            already_refunded_shipping = any(
                r["order_id"] == o["id"] and r["reason"] in ("defective", "wrong_item") for r in self.store.returns)
            if not already_refunded_shipping:
                refund += o["shipping_cost"]
        refund = round(refund, 2)

        if refund > MAX_AUTO_REFUND:
            raise ToolError(f"This refund would be ${refund:.2f}, over the ${MAX_AUTO_REFUND:.0f} limit for "
                            "automatic returns. Do not start it; escalate to a human with category "
                            "'refund_over_limit'.")
        return self.store.create_return(o["id"], list(set(line_ids)), reason, refund)

    def _tool_issue_goodwill_credit(self, order_id: str, amount: float, reason: str) -> dict:
        o = self._own_order(order_id)
        eta = date.fromisoformat(o["estimated_delivery"])
        arrived = date.fromisoformat(o["delivered"]) if o.get("delivered") else None
        is_late = (arrived is not None and arrived > eta) or (arrived is None and self.store.today > eta
                                                             and o["status"] != "cancelled")
        if not is_late:
            raise ToolError(f"Order {o['id']} is not late (estimated {o['estimated_delivery']}), "
                            "so it isn't eligible for a goodwill credit.")
        if any(c["order_id"] == o["id"] for c in self.store.credits):
            raise ToolError(f"A goodwill credit was already issued on {o['id']}.")
        if not 0 < amount <= MAX_GOODWILL_CREDIT:
            raise ToolError(f"Goodwill credit must be between $0 and ${MAX_GOODWILL_CREDIT:.0f}.")
        return self.store.add_credit(o["customer_id"], o["id"], round(amount, 2), reason)

    def _tool_escalate_to_human(self, category: str, summary: str, order_id: str | None = None) -> dict:
        if category not in ESCALATION_CATEGORIES:
            raise ToolError(f"category must be one of {ESCALATION_CATEGORIES}")
        if order_id:
            order_id = self._own_order(order_id)["id"] if self.verified_customer_id else None
        ticket = self.store.create_ticket(self.verified_customer_id, category, summary, order_id)
        return {**ticket, "expected_response": "A specialist will reply by email within 2 business days."}
