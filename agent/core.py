"""The agent loop: send the conversation to Claude, run any tools it asks for,
feed the results back, and repeat until it answers the customer in text.

Changes the agent proposes are carried out only by `decide()`, which the web app
calls when the customer clicks Approve or Cancel on the approval card."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .prompt import build_system_prompt
from .store import Store
from .tools import TOOL_SCHEMAS, ToolExecutor

DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_TOOL_ROUNDS = 8


@dataclass
class TurnResult:
    reply: str
    trace: list[dict] = field(default_factory=list)  # tool calls + results, for the UI and evals
    proposals: list[dict] = field(default_factory=list)  # approval cards to show: [{id, summary}]


class SupportAgent:
    """One conversation with one customer."""

    def __init__(self, client, store: Store | None = None, model: str = DEFAULT_MODEL):
        self.client = client
        self.model = model
        self.store = store or Store()
        self.tools = ToolExecutor(self.store)
        self.system = build_system_prompt(self.store)
        self.messages: list[dict] = []

    def respond(self, customer_message: str) -> TurnResult:
        self.messages.append({"role": "user", "content": customer_message})
        trace: list[dict] = []

        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=self.system,
                tools=TOOL_SCHEMAS,
                messages=self.messages,
            )
            content = [_block_to_dict(b) for b in response.content]
            self.messages.append({"role": "assistant", "content": content})

            tool_calls = [b for b in content if b["type"] == "tool_use"]
            if not tool_calls:
                text = "".join(b["text"] for b in content if b["type"] == "text").strip()
                return TurnResult(reply=text, trace=trace, proposals=self._new_proposals(trace))

            results = []
            for call in tool_calls:
                output = self.tools.run(call["name"], call["input"])
                trace.append({"tool": call["name"], "input": call["input"], "output": output})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": json.dumps(output),
                    "is_error": "error" in output,
                })
            self.messages.append({"role": "user", "content": results})

        # Safety valve: the model kept calling tools without answering.
        fallback = ("Sorry, I'm having trouble with that request. I can connect you with a specialist "
                    "if you'd like.")
        self.messages.append({"role": "assistant", "content": fallback})
        return TurnResult(reply=fallback, trace=trace, proposals=self._new_proposals(trace))

    def decide(self, proposal_id: str, approve: bool) -> TurnResult:
        """The customer clicked Approve or Cancel on an approval card.

        The reply is written by code, not the model, so the customer is told exactly what
        happened. The outcome is also added to the conversation so the agent knows about it.
        """
        outcome = self.tools.decide(proposal_id, approve)
        summary = outcome.get("summary", "that request")
        if not outcome["ok"]:
            reply = f"I couldn't do that: {outcome['error']}"
        elif outcome["status"] == "cancelled":
            reply = "No problem, I didn't make that change. Is there anything else I can help with?"
        else:
            reply = _done_message(outcome["action"], outcome["result"])
        clicked = "Approve" if approve else "Cancel"
        note = (f"[The customer clicked {clicked} on proposal {proposal_id} ({summary}). "
                f"Outcome: {json.dumps(outcome)}]")
        self.messages.append({"role": "user", "content": note})
        self.messages.append({"role": "assistant", "content": reply})
        trace = [{"tool": f"customer clicked {clicked}", "input": {"proposal_id": proposal_id, "summary": summary},
                  "output": outcome if outcome["ok"] else {"error": outcome["error"]}}]
        return TurnResult(reply=reply, trace=trace)

    def _new_proposals(self, trace: list[dict]) -> list[dict]:
        ids = {t["output"].get("proposal_id") for t in trace if isinstance(t["output"], dict)}
        return [p for p in self.tools.pending_proposals() if p["id"] in ids]


def _done_message(action: str, r: dict) -> str:
    if action == "cancel_order":
        return (f"Done. Order {r['order_id']} is cancelled, and ${r['refund_amount']:.2f} is being refunded "
                "to your original payment method.")
    if action == "update_shipping_address":
        return f"Done. Order {r['order_id']} will now ship to {r['shipping_address']}."
    if action == "start_return":
        return (f"Done. Your return {r['return_id']} is open, and ${r['refund_amount']:.2f} will be refunded once "
                f"we receive the item. Print your label here: {r['label_url']}")
    if action == "issue_goodwill_credit":
        return f"Done. A ${r['amount']:.2f} store credit has been added to your account."
    return "Done."


def _block_to_dict(block) -> dict:
    """Convert an SDK content block to a plain dict we can resend and serialize."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    # Other block types (e.g. thinking) are passed through as-is.
    return block.model_dump() if hasattr(block, "model_dump") else dict(block)
