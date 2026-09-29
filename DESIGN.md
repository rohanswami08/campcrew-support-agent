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

**Run 1 (1 trial each): 23/25.** Reading the transcripts showed that neither failure was an agent mistake:
- *Late order, customer demands $50.* The agent held the $15 cap twice. Then the customer asked for a specialist, and the agent escalated. That's good behavior, but my scenario didn't allow it. **Fix:** I split outcomes into `expected_actions` (must happen) and `allowed_actions` (may happen).
- *Prompt injection.* The simulated customer **never sent the injection**. It softened the attack and made up a "damaged items" story instead. So a pass here would have meant nothing. **Fix:** a scenario can now script its first message word for word, and the simulator is told not to invent complaints.

**Run 2 (3 trials each): pass^1 97%, pass^3 92%.** Both failures were scoring mistakes again:
- *Wrong ZIP.* After two failed verifications, the agent offered a specialist and opened a ticket. It leaked nothing, so escalation is now allowed.
- *Product question.* The agent said the jacket was "in stock, except in sizes XS and XXL." That's correct, but my keyword list only looked for phrases like "out of stock." I widened the list.

**Run 3 (3 trials each, after the fixes): 75/75, pass^3 100%.** I spot-checked the transcripts to confirm the passes were real. The injection was actually sent and refused, the $15 cap held under pressure, and another customer's order was never described.

**Lesson:** most first-round failures were in the eval, not the agent. A test suite is only trustworthy after you read the transcripts behind both the passes and the failures.

**An open design question the transcripts raised:** in the off-script run, the "customer" claimed both items were damaged. The agent started a $353 defective-item return with no evidence (policy doesn't ask for any). Should defect claims above some amount require a photo, or go to a human? That's a real fraud-versus-convenience trade-off, and it's worth deciding and adding a scenario for.

## What I'd build next

- Customer-side approval buttons for irreversible actions
- Streaming responses
- An LLM-judge rubric for tone and clarity
- Human-handoff inbox that shows escalated tickets with the full transcript
- Multi-language support
