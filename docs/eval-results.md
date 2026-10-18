# Evaluation results

Synthetic fixtures; the gold answers were labelled by the AI team on the owner's behalf.
Results describe this test set only; they do not establish production accuracy.

## What was measured

- **Scenario**: the synthetic demo (Atlas team, product TrailCart): three meetings, one Slack export and two pull requests in `src/decision_ledger/demo/fixtures/`.
- **System output**: a replay of the responses recorded from Gemini (`gemini-3.5-flash`, Vertex AI) on 2026-10-17. See `src/decision_ledger/demo/fixtures/llm/RECORDING.md`. These numbers describe that recording, not a live run.
- **Gold answers**: `demo/eval/dev-gold.json`, a development set written from the cases planted in the scenario design. It was written after the first, discarded recording had been inspected, so it is not an independent test.
- **Command**: `uv run python scripts/eval.py --out demo/eval/dev-results.txt`. The raw output is `demo/eval/dev-results.txt`; the table below copies it.

## Counts

| Task | Hit | False positive | Miss | Precision | Recall |
|------|-----|----------------|------|-----------|--------|
| Extraction | 5 | 0 | 1 | 1.00 | 0.83 |
| Conflict | 1 | 0 | 0 | 1.00 | 1.00 |
| Deviation | 1 | 0 | 0 | 1.00 | 1.00 |
| Premise | 1 | 2 | 0 | 0.33 | 1.00 |
| Supersession (extra) | 1 | 0 | 0 | 1.00 | 1.00 |

Verbatim quotes: 18 of 18 quotes in the model's raw extraction and premise responses occur in the source.
N/A means the denominator is zero.

## Why each miss and false positive happened

- **Extraction miss**: "The order store stays on Postgres." was not extracted as a decision.
- **Premise false positives**: two of the three premise alerts are wrong (failure cases 1 and 2). The third, QA growing from two to four testers against the November 17 decision, is the planted premise change and is correct.
- **Conflict**: in this recording Gemini judged the guest checkout reversal a conflict. Across the four recordings made on 2026-10-17 it chose the conflict twice and a supersession twice, so this result is not stable.

## Failure case 1: a premise flagged although it still holds

At the kickoff the team set the beta for November 3, with the premise that a partner fair is on November 10. At the architecture meeting they moved the beta to November 17. The fair date did not change, but the premise watch flagged it. The model's recorded explanation:

> The beta launch is being postponed from November 3 to November 17, which is after the assumed date of the partner fair (November 10). Priya also mentions she will inform the partners of the new November 17 date, making the assumption about the November 10 fair date doubtful.

The premise it challenged:

> The Northfield outdoor partner fair is on November 10.

The new date makes the original plan obsolete; it does not make the fair date untrue. A person resolving this alert would dismiss it, and it still counts as a false positive here.

## Failure case 2: a rule change read as a broken premise

The Slack thread requires an account for every order. The model also flagged the premise behind the guest checkout decision:

> The team has decided that the beta will require an account for every order, which directly contradicts the decision to allow guest checkout and makes the assumption that forcing sign-ups causes cart abandonment doubtful or no longer a priority.

The premise it challenged:

> Forcing users to sign up causes them to abandon the cart.

The thread changes the checkout rule, which the conflict alert already reports, but it says nothing about cart abandonment. The premise is still as true as before.

## Held-out set

Not verified; pending run. The held-out scenario and its gold answers are kept apart and frozen; their numbers will be added only after a run.
