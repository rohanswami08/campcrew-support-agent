"""CampCrew's support policy, enforced in code.

`check_policy` is the single place that decides whether a change is allowed.
It runs twice for every change:
  1. when the agent proposes it (so the agent can explain a "no" right away), and
  2. again when the customer clicks Approve, because things may have changed in
     between (the order shipped, a credit was already issued, ...).

The model reads the same rules in data/policy.md, but it is never trusted to
follow them on its own: if this function says no, nothing happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .store import Store

CHANGED_MIND_WINDOW_DAYS = 30
WARRANTY_WINDOW_DAYS = 365
MAX_AUTO_REFUND = 500.00
MAX_GOODWILL_CREDIT = 15.00
RETURN_REASONS = ("changed_mind", "defective", "wrong_item")


class PolicyViolation(Exception):
    """The change isn't allowed. The message is a reason the agent can tell the customer."""


@dataclass
class Plan:
    """A change that passed the policy check, described exactly as it will be carried out."""
    action: str
    order_id: str
    summary: str                      # what the customer sees on the approval card
    params: dict = field(default_factory=dict)


def check_policy(store: Store, customer_id: str | None, action: str, args: dict) -> Plan:
    """Decide whether `customer_id` may perform `action`. Returns a Plan, or raises PolicyViolation.

    Enforces:
      1. Identity    - the customer must be verified (email + ZIP).
      2. Ownership   - the order must belong to them. Someone else's order gets the same
                       "not found" as a missing one, so its existence never leaks.
      3. Timing      - cancel or change the address only before the order ships.
      4. Returns     - delivered items only, never twice; "changed mind" within 30 days and
                       never for final-sale items; defective / wrong item within 365 days.
      5. Refund cap  - a return refunding more than $500 needs a human, so the agent must escalate.
      6. Credit cap  - goodwill credit only for late orders, at most $15, once per order.
    """
    order = _owned_order(store, customer_id, args.get("order_id", ""))          # rules 1-2

    if action == "cancel_order":
        _require_not_shipped(order, "cancelled")                                  # rule 3
        total = store.order_total(order)
        return Plan(action, order["id"],
                    f"Cancel order {order['id']} ({_item_names(store, order)}) and refund "
                    f"${total:.2f} to your original payment method",
                    {"reason": args.get("reason", "")})

    if action == "update_shipping_address":
        _require_not_shipped(order, "re-addressed")                               # rule 3
        address = " ".join(str(args.get("new_address", "")).split())
        if len(address) < 10:
            raise PolicyViolation("That address looks incomplete. I need the street, city, state, and ZIP.")
        return Plan(action, order["id"],
                    f"Change the shipping address for order {order['id']} to: {address}",
                    {"address": address})

    if action == "start_return":
        line_ids, reason, refund = _check_return(store, order, args)              # rule 4
        if refund > MAX_AUTO_REFUND:                                              # rule 5
            raise PolicyViolation(
                f"This refund would be ${refund:.2f}, over the ${MAX_AUTO_REFUND:.0f} limit for automatic "
                "returns. Don't start it; escalate to a human with category 'refund_over_limit'.")
        names = ", ".join(_line_name(store, order, lid) for lid in line_ids)
        return Plan(action, order["id"],
                    f"Start a return for {names} on order {order['id']} ({reason.replace('_', ' ')}) and refund "
                    f"${refund:.2f} once we receive it",
                    {"line_ids": line_ids, "reason": reason, "refund": refund})

    if action == "issue_goodwill_credit":
        amount = round(float(args.get("amount", 0)), 2)
        if not _is_late(store, order):                                            # rule 6
            raise PolicyViolation(f"Order {order['id']} isn't late (it was due {order['estimated_delivery']}), "
                                  "so it isn't eligible for a goodwill credit.")
        if any(c["order_id"] == order["id"] for c in store.credits):
            raise PolicyViolation(f"A goodwill credit was already issued on order {order['id']}.")
        if not 0 < amount <= MAX_GOODWILL_CREDIT:
            raise PolicyViolation(f"Goodwill credit must be more than $0 and at most ${MAX_GOODWILL_CREDIT:.0f}.")
        return Plan(action, order["id"],
                    f"Add a ${amount:.2f} store credit to your account for the late delivery of order {order['id']}",
                    {"amount": amount, "reason": args.get("reason", "")})

    raise PolicyViolation(f"Unknown action: {action}")


# ---------- helpers for the rules above ----------

def _owned_order(store: Store, customer_id: str | None, order_id: str) -> dict:
    if customer_id is None:
        raise PolicyViolation("The customer isn't verified yet. Ask for the account email and ZIP code first.")
    order = store.orders.get(str(order_id).strip().upper())
    if order is None or order["customer_id"] != customer_id:
        raise PolicyViolation(f"No order {order_id} found on this customer's account.")
    return order


def _require_not_shipped(order: dict, verb: str) -> None:
    if order["status"] != "processing":
        raise PolicyViolation(f"Order {order['id']} is '{order['status']}', so it can't be {verb}. "
                              "That's only possible before an order ships.")


def _check_return(store: Store, order: dict, args: dict) -> tuple[list[str], str, float]:
    reason = args.get("reason")
    line_ids = sorted(set(args.get("line_ids") or []))
    if reason not in RETURN_REASONS:
        raise PolicyViolation(f"The return reason must be one of {RETURN_REASONS}.")
    if not line_ids:
        raise PolicyViolation("Say which items to return (their line_ids from get_order).")
    if not order.get("delivered"):
        raise PolicyViolation(f"Order {order['id']} hasn't been delivered, so nothing on it can be returned yet.")
    days = (store.today - date.fromisoformat(order["delivered"])).days
    items = {i["line_id"]: i for i in order["items"]}

    refund = 0.0
    for lid in line_ids:
        item = items.get(lid)
        if item is None:
            raise PolicyViolation(f"There's no line {lid} on order {order['id']}.")
        product = store.products[item["sku"]]
        if item["status"] != "delivered":
            raise PolicyViolation(f"{product['name']} is '{item['status']}' and can't be returned again.")
        if reason == "changed_mind":
            if product["final_sale"]:
                raise PolicyViolation(f"{product['name']} is final sale. It can only be returned if it's "
                                      "defective or the wrong item.")
            if days > CHANGED_MIND_WINDOW_DAYS:
                raise PolicyViolation(f"{product['name']} was delivered {days} days ago; changed-mind returns "
                                      f"are only accepted within {CHANGED_MIND_WINDOW_DAYS} days.")
        elif days > WARRANTY_WINDOW_DAYS:
            raise PolicyViolation(f"{product['name']} was delivered {days} days ago, outside the "
                                  f"{WARRANTY_WINDOW_DAYS}-day warranty.")
        refund += item["unit_price"] * item["qty"]

    if reason in ("defective", "wrong_item"):  # we pay shipping on our mistakes, once per order
        shipping_already_refunded = any(r["order_id"] == order["id"] and r["reason"] in ("defective", "wrong_item")
                                        for r in store.returns)
        if not shipping_already_refunded:
            refund += order["shipping_cost"]
    return line_ids, reason, round(refund, 2)


def _is_late(store: Store, order: dict) -> bool:
    due = date.fromisoformat(order["estimated_delivery"])
    if order.get("delivered"):
        return date.fromisoformat(order["delivered"]) > due
    return order["status"] != "cancelled" and store.today > due


def _item_names(store: Store, order: dict) -> str:
    return ", ".join(store.products[i["sku"]]["name"] for i in order["items"])


def _line_name(store: Store, order: dict, line_id: str) -> str:
    item = next(i for i in order["items"] if i["line_id"] == line_id)
    name = store.products[item["sku"]]["name"]
    return f"{item['qty']}x {name}" if item["qty"] > 1 else name
