# CampCrew Support Agent

A customer-support AI agent for **CampCrew**, a fictional outdoor-gear store. It verifies customers, looks up orders, cancels, changes addresses, starts returns, issues goodwill credit, and hands off to a human when it should. It does all of this by calling real tools against a mock store database, under a written support policy.

**[Live demo](https://campcrew-support-agent.onrender.com/)** · **[2-minute walkthrough video](https://youtu.be/9M8S7noJqxM)** · Built with Python, FastAPI, and the Claude API

![screenshot](docs/screenshot.png)

## What makes it more than a chatbot

**1. It takes real actions through tools.** The agent has nine tools (`verify_customer`, `get_order`, `cancel_order`, `start_return`, `escalate_to_human`, …) that work against a per-session copy of the store. The demo's *Agent trace* panel shows each tool call live.

**2. The customer approves every change.** When the agent wants to cancel an order, change an address, start a return, or issue credit, it doesn't do it. It proposes it, and the customer sees a card with the exact change ("Add a $15.00 store credit to your account for the late delivery of order CC-10460") and **Approve** / **Cancel** buttons. The server makes no change until Approve is clicked, then checks ownership and policy *again* before doing anything. Cancel changes nothing, and a proposal can only be approved once, so double-clicking can't issue two credits. The model never sees the buttons and can't click them for the customer.

**3. The policy is enforced in code, not only in the prompt.** The model reads the [support policy](data/policy.md), but every rule is also enforced by one function, `check_policy()` in [`agent/policy.py`](agent/policy.py), which runs when a change is proposed and again when it's approved. Even a confused or manipulated model **cannot**:
- see or change another customer's order (it gets the same "not found" as a nonexistent order, so nothing leaks),
- cancel an order that already shipped,
- return a final-sale item for a non-defect reason, or anything past its window,
- auto-refund more than $500 (it's told to escalate instead),
- issue more than $15 of goodwill credit, or any credit on an on-time order.

Blocked calls come back to the model as errors with a reason, so it can explain the rule to the customer. In the demo they show up red.

**4. It's measured with an eval suite.** [`evals/`](evals/) has 25 scenarios. In each one, a second model plays a customer: happy paths, policy edge cases, escalations, prompt injection, social engineering, and data-leak attempts. Scoring looks at **outcomes, not wording**, in the style of [τ-bench](https://github.com/sierra-research/tau-bench): after each conversation, the list of writes to the store must exactly match what the policy says should have happened. Transcripts are also checked for leaked private data.

Latest full run, with the approval step (Claude Sonnet 5 as the agent, Claude Haiku 4.5 as the customer, 3 trials per scenario). The simulated customer clicks **Approve** or **Cancel** on the approval cards itself: every change in the passing runs happened only after an Approve click, and in the "changes mind" scenario it clicked Cancel and nothing changed.

| Category | Scenarios | Runs passed |
|---|---|---|
| Happy path | 7 | 21 / 21 |
| Policy edge cases | 9 | 27 / 27 |
| Escalation | 3 | 9 / 9 |
| Security (identity / data leaks) | 3 | 9 / 9 |
| Adversarial (injection, pressure, off-topic) | 3 | 9 / 9 |
| **Overall** | **25** | **75 / 75 (pass^1 = 100%, pass^3 = 100%)** |

`pass^k` is the share of scenarios the agent gets right on **all k** repeated runs. It measures reliability, which matters more for support than getting it right once. Getting here took two rounds of fixing the *evals themselves*; see [DESIGN.md](DESIGN.md#what-the-evals-caught). A perfect score mostly means the suite needs harder cases next.

## Architecture

```
Browser (static/)  ──POST /api/chat──▶  FastAPI (app.py)
                                         │  per-session agent + store copy, rate limits
                                         ▼
                                   SupportAgent (agent/core.py)
                                         │  loop: Claude ⇄ tools until a text reply
                          ┌──────────────┴──────────────┐
                    Claude API                 ToolExecutor (agent/tools.py)
               (system prompt + policy)          │ change tools only *propose*
                                                 ▼
                                    check_policy() (agent/policy.py)
                                                 │ approval card shown to customer
Browser ──POST /api/decide (Approve)──▶ check_policy() again ──▶ Store (agent/store.py)
                                                                  seed: data/store.json
```

## Run it locally

```bash
git clone https://github.com/rohanswami08/campcrew-support-agent
cd campcrew-support-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app:app --reload          # open http://localhost:8000
pytest                            # 34 offline tests, no API key needed
python -m evals.run_evals --trials 3
```

Demo logins (all fictional) appear in the sidebar. For example: `jordan.alvarez@example.com` / `80302`.

## Deploy

`render.yaml` deploys to [Render](https://render.com) as a free web service. Set `ANTHROPIC_API_KEY` in the dashboard. The public demo is protected by a per-IP rate limit, a 30-message cap per conversation, and a daily message cap (`DAILY_MESSAGE_CAP`). Also set a monthly spend limit in the Anthropic Console.

## Limits

This is a demo, and a few things are deliberately simpler than production:

- **Identity is email + ZIP, which is a demo check, not real authentication.** Anyone who knows both can act as that customer. A real deployment would sign the customer in (a logged-in session or a one-time code sent to their email) before the agent could touch their account.
- **The customer is the only approver.** A real system would add independent approval for higher-risk changes, such as a human reviewing large refunds before money moves, not just after escalation.
- **The store is in memory and resets per conversation**, and each proposal lives only as long as the conversation.

## Design notes

See [DESIGN.md](DESIGN.md) for the decisions and trade-offs, what the evals caught, and what I'd build next.
