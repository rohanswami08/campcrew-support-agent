"""In-memory store for one conversation.

Every chat session (and every eval run) gets its own fresh copy of the seed
data, so visitors to the demo can't affect each other and evals are
reproducible. Every write goes through a method here and is recorded in
`self.actions`, which the evals compare against the expected outcome.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_SEED: dict | None = None


def _load_seed() -> dict:
    global _SEED
    if _SEED is None:
        _SEED = json.loads((DATA_DIR / "store.json").read_text())
    return _SEED


def load_policy() -> str:
    return (DATA_DIR / "policy.md").read_text()


class Store:
    def __init__(self, seed: dict | None = None):
        data = copy.deepcopy(seed or _load_seed())
        self.name: str = data["store_name"]
        self.today: date = date.fromisoformat(data["today"])
        self.products: dict[str, dict] = {p["sku"]: p for p in data["products"]}
        self.customers: dict[str, dict] = {c["id"]: c for c in data["customers"]}
        self.orders: dict[str, dict] = {o["id"]: o for o in data["orders"]}
        self.returns: list[dict] = list(data.get("existing_returns", []))
        self.credits: list[dict] = []
        self.tickets: list[dict] = []
        self.actions: list[dict] = []  # every successful write, in order
        self._next_rma = 5002
        self._next_ticket = 9001

    # ---------- reads ----------
    def find_customer(self, email: str, zip_code: str) -> dict | None:
        email = email.strip().lower()
        zip_code = zip_code.strip()[:5]
        for c in self.customers.values():
            if c["email"].lower() == email and c["zip"] == zip_code:
                return c
        return None

    def order_total(self, order: dict) -> float:
        items = sum(i["unit_price"] * i["qty"] for i in order["items"])
        return round(items + order["shipping_cost"], 2)

    # ---------- writes (policy is checked in tools.py before these run) ----------
    def cancel_order(self, order_id: str, reason: str) -> dict:
        order = self.orders[order_id]
        order["status"] = "cancelled"
        for item in order["items"]:
            item["status"] = "cancelled"
        refund = self.order_total(order)
        self.actions.append({"type": "cancel_order", "order_id": order_id, "refund": refund, "reason": reason})
        return {"order_id": order_id, "status": "cancelled", "refund_amount": refund}

    def update_address(self, order_id: str, address: str) -> dict:
        self.orders[order_id]["shipping_address"] = address
        self.actions.append({"type": "update_address", "order_id": order_id, "address": address})
        return {"order_id": order_id, "shipping_address": address}

    def create_return(self, order_id: str, line_ids: list[str], reason: str, refund: float) -> dict:
        rma = {
            "return_id": f"RMA-{self._next_rma}",
            "order_id": order_id,
            "line_ids": sorted(line_ids),
            "reason": reason,
            "refund_amount": refund,
            "status": "awaiting_item",
            "created": self.today.isoformat(),
        }
        self._next_rma += 1
        self.returns.append(rma)
        for item in self.orders[order_id]["items"]:
            if item["line_id"] in line_ids:
                item["status"] = "return_started"
        self.actions.append({"type": "start_return", "order_id": order_id, "line_ids": sorted(line_ids),
                             "reason": reason, "refund": refund})
        return {**rma, "label_url": f"https://returns.fernwick.example/{rma['return_id']}.pdf"}

    def add_credit(self, customer_id: str, order_id: str, amount: float, reason: str) -> dict:
        credit = {"customer_id": customer_id, "order_id": order_id, "amount": amount, "reason": reason}
        self.credits.append(credit)
        self.actions.append({"type": "goodwill_credit", "order_id": order_id, "amount": amount})
        return credit

    def create_ticket(self, customer_id: str | None, category: str, summary: str, order_id: str | None) -> dict:
        ticket = {
            "ticket_id": f"TKT-{self._next_ticket}",
            "customer_id": customer_id,
            "order_id": order_id,
            "category": category,
            "summary": summary,
        }
        self._next_ticket += 1
        self.tickets.append(ticket)
        self.actions.append({"type": "escalate", "category": category, "order_id": order_id})
        return ticket
