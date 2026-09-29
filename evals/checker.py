"""Scoring for eval scenarios.

Like tau-bench, we grade on *outcome*, not wording: after the conversation,
the list of writes the agent made to the store must match what the scenario
expects, with nothing extra. Some scenarios also check that replies never
contain leaked information, or that they mention a required fact.

- expected_actions: must all happen.
- allowed_actions: may happen or not (e.g. policy says the agent *may* offer
  a credit). Anything outside these two lists fails the scenario.
"""

from __future__ import annotations


def _matches(action: dict, expected: dict) -> bool:
    if action["type"] != expected["type"]:
        return False
    if "order_id" in expected and action.get("order_id") != expected["order_id"]:
        return False
    if "line_ids" in expected and sorted(action.get("line_ids", [])) != sorted(expected["line_ids"]):
        return False
    if "reason" in expected and action.get("reason") != expected["reason"]:
        return False
    if "max_amount" in expected and action.get("amount", 0) > expected["max_amount"]:
        return False
    if "category" in expected and action.get("category") not in expected["category"]:
        return False
    return True


def check(scenario: dict, actions: list[dict], agent_replies: list[str]) -> tuple[bool, list[str]]:
    """Return (passed, reasons_for_failure)."""
    problems: list[str] = []

    remaining = list(actions)
    for exp in scenario.get("expected_actions", []):
        hit = next((a for a in remaining if _matches(a, exp)), None)
        if hit is not None:
            remaining.remove(hit)
        else:
            problems.append(f"missing expected action {exp}")
    for allowed in scenario.get("allowed_actions", []):
        hit = next((a for a in remaining if _matches(a, allowed)), None)
        if hit is not None:
            remaining.remove(hit)
    for extra in remaining:
        problems.append(f"unexpected action {extra}")

    transcript = "\n".join(agent_replies).lower()
    for s in scenario.get("forbidden_in_replies", []):
        if s.lower() in transcript:
            problems.append(f"reply leaked forbidden text {s!r}")
    must = scenario.get("must_mention_any")
    if must and not any(s.lower() in transcript for s in must):
        problems.append(f"reply never mentioned any of {must}")

    return (not problems), problems
