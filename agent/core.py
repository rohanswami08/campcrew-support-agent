"""The agent loop: send the conversation to Claude, run any tools it asks for,
feed the results back, and repeat until it answers the customer in text."""

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
                return TurnResult(reply=text, trace=trace)

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
        return TurnResult(reply=fallback, trace=trace)


def _block_to_dict(block) -> dict:
    """Convert an SDK content block to a plain dict we can resend and serialize."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    # Other block types (e.g. thinking) are passed through as-is.
    return block.model_dump() if hasattr(block, "model_dump") else dict(block)
