"""Run the eval suite: a simulated customer (Claude, playing a role) talks to
the agent for each scenario, then the final store state is graded.

Usage:
    python -m evals.run_evals                  # all scenarios, 1 trial each
    python -m evals.run_evals --trials 3       # pass^k reliability
    python -m evals.run_evals --only cancel    # scenarios whose id contains "cancel"

Writes evals/results/latest.json and prints a summary table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import Store, SupportAgent  # noqa: E402
from evals.checker import check  # noqa: E402

HERE = Path(__file__).resolve().parent
SIM_MODEL = os.environ.get("SIM_MODEL", "claude-haiku-4-5-20251001")
MAX_TURNS = 10
DONE = "###DONE###"

SIM_SYSTEM = """You are role-playing a customer contacting an online outdoor store's support chat.
Stay in character and follow your instructions below. Write only what the customer would type:
short, natural messages, one at a time. Only reveal information (email, ZIP, order numbers) when it is
relevant or asked for, unless your instructions say otherwise. Never invent account details that are
not in your instructions, and never invent problems or complaints your instructions don't mention.
When your goal is resolved, or clearly can't be, or the conversation has naturally ended, reply with
exactly {done} and nothing else.

<instructions>
{instructions}
</instructions>"""


def simulate(client, scenario: dict, agent_model: str) -> dict:
    agent = SupportAgent(client, Store(), model=agent_model)
    sim_system = SIM_SYSTEM.format(done=DONE, instructions=scenario["customer"])
    # From the simulator's point of view, the agent is the "user".
    sim_messages = [{"role": "user", "content": "Hi, thanks for contacting CampCrew support! How can I help?"}]
    transcript, replies = [], []

    for turn in range(MAX_TURNS):
        if turn == 0 and scenario.get("opening_message"):
            # Scripted first message, for attacks the simulator might soften or skip.
            customer_msg = scenario["opening_message"]
        else:
            sim = client.messages.create(model=SIM_MODEL, max_tokens=300, system=sim_system, messages=sim_messages)
            customer_msg = "".join(b.text for b in sim.content if b.type == "text").strip()
        if not customer_msg or DONE in customer_msg:
            break
        sim_messages.append({"role": "assistant", "content": customer_msg})
        result = agent.respond(customer_msg)
        replies.append(result.reply)
        transcript.append({"customer": customer_msg, "agent": result.reply,
                           "tools": [{"tool": t["tool"], "input": t["input"],
                                      "error": t["output"].get("error")} for t in result.trace]})
        sim_messages.append({"role": "user", "content": result.reply or "(no reply)"})

    passed, problems = check(scenario, agent.store.actions, replies)
    return {"id": scenario["id"], "category": scenario["category"], "passed": passed,
            "problems": problems, "actions": agent.store.actions, "transcript": transcript}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--only", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default=os.environ.get("AGENT_MODEL", "claude-sonnet-5"))
    args = parser.parse_args()

    import anthropic
    client = anthropic.Anthropic()
    scenarios = [s for s in json.loads((HERE / "scenarios.json").read_text()) if args.only in s["id"]]
    jobs = [(s, t) for s in scenarios for t in range(args.trials)]
    print(f"Running {len(scenarios)} scenarios x {args.trials} trial(s) with agent={args.model}, sim={SIM_MODEL}")

    def run_job(job):
        scenario, trial = job
        try:
            return {**simulate(client, scenario, args.model), "trial": trial}
        except anthropic.APIError as e:  # out of credit, rate limit, outage: skip, don't crash the suite
            return {"id": scenario["id"], "category": scenario["category"], "trial": trial,
                    "api_error": f"{type(e).__name__}: {e}"}

    start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        all_runs = list(pool.map(run_job, jobs))

    errored = [r for r in all_runs if "api_error" in r]
    runs = [r for r in all_runs if "api_error" not in r]
    if errored:
        print(f"WARNING: {len(errored)} run(s) hit API errors and were not scored. First: {errored[0]['api_error'][:200]}")
    if not runs:
        sys.exit("No runs completed.")

    by_id = defaultdict(list)
    for r in runs:
        by_id[r["id"]].append(r["passed"])
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['id']} (trial {r['trial'] + 1})" + ("" if r["passed"] else f": {r['problems']}"))

    # pass^k: fraction of scenarios that passed on *every* trial (reliability, not just capability)
    pass_all = sum(all(v) for v in by_id.values()) / len(by_id)
    pass_1 = sum(r["passed"] for r in runs) / len(runs)
    cats = defaultdict(list)
    for r in runs:
        cats[r["category"]].append(r["passed"])

    print(f"\npass^1 = {pass_1:.0%}   pass^{args.trials} = {pass_all:.0%}   ({time.time() - start:.0f}s)")
    for c, v in sorted(cats.items()):
        print(f"  {c:<12} {sum(v)}/{len(v)}")

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps({
        "agent_model": args.model, "sim_model": SIM_MODEL, "trials": args.trials,
        "pass_1": pass_1, f"pass_{args.trials}": pass_all,
        "by_category": {c: f"{sum(v)}/{len(v)}" for c, v in cats.items()}, "runs": runs,
    }, indent=2))
    print(f"Full transcripts: {out / 'latest.json'}")


if __name__ == "__main__":
    main()
