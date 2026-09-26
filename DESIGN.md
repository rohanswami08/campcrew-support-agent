# Design notes

## Decisions and trade-offs

**Policy in the prompt *and* in code.** The prompt alone isn't a guarantee: a model can be talked into things, or just make a mistake. Code alone makes the agent unhelpful, because it can't explain rules it doesn't know. So the model gets the full policy to reason with, and every write tool re-checks the rules. Tool errors carry a human-readable reason, so a blocked action turns into a clear explanation for the customer instead of a dead end.

**Identity = email + ZIP.** It's simple enough for a demo and still forces the agent through an explicit verification step. After 3 failed attempts in a conversation, verification locks and the agent is told to offer a human. Another customer's order returns the *same* error as an order that doesn't exist, so the agent can't be used to confirm that an order number or person exists.

**Confirmation before actions is prompt-level only.** I considered a `confirmed: true` parameter on write tools. It would be theater, because the model fills in that parameter itself. A real version would put confirmation in the UI (an "Approve" button the customer clicks), which the model can't fake.

**One fresh store per conversation.** Demo visitors can't affect each other, and each eval starts from the same known state, which makes outcome-based grading possible.

**$500 auto-refund limit → escalation.** High-value refunds go to a human. The tool doesn't just refuse; it tells the model to escalate with category `refund_over_limit`, so the customer still gets a path forward.

**Late-order credit is optional, capped at $15.** Policy says the agent *may* offer it. So in those scenarios the eval allows "no credit" or "credit ≤ $15", and anything above $15 fails.

## Evaluation approach

- A simulated customer (a cheaper Claude model) follows a hidden persona script: some cooperative, some pushy, some adversarial.
- Grading compares the store's list of writes to the expected outcome: an exact match, no extra actions. Wording is never graded, so the agent can phrase things however it likes.
- Security scenarios also scan the agent's replies for strings that would indicate a leak (another customer's name or address).
- `pass^k` over repeated trials measures reliability.

**Known limits of the evals:** the simulator can go off-script. String checks for leaks can miss paraphrased leaks. Nothing grades tone or helpfulness when no action is expected. An LLM-judge rubric for tone would be a good addition.

## What the evals caught

_(Fill this in as you iterate. Example: "v1 started a return for both items when the customer only wanted the tent returned. I fixed it by telling the agent to confirm specific items by name.")_

## What I'd build next

- Customer-side approval buttons for irreversible actions
- Streaming responses
- An LLM-judge rubric for tone and clarity
- Human-handoff inbox that shows escalated tickets with the full transcript
- Multi-language support
