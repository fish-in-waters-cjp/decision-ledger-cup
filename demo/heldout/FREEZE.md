# Held-out set: freeze record

Synthetic held-out scenario for Decision Ledger (Kestrel team, product Ferryline).
Everything here is invented, including the payment provider name.

- **Status**: frozen on 2026-10-17. The inputs are in this folder; the gold answers are kept apart in
  `demo/eval/heldout-gold.json`.
- **Content hash**: `demo/eval/heldout-gold.sha256` lists the sha256 of the gold file and of every file
  here except this one. `tests/test_heldout_set.py` compares them, so any later edit turns the test red.
- **Second labeller**: not done yet. Claire labels the inputs independently after the freeze; the two
  label sets are then compared and every disagreement is settled in writing.
- **Written and labelled by**: the AI team acting on Ci's behalf (overnight build). Claire has not read
  this set. Until Claire labels it independently, describe it as an AI-written, AI-labelled
  synthetic held-out set, not a human-labelled one.
- **Who may run it**: only after the model, prompts, thresholds and code are frozen for submission.
  No test, recording or prompt tuning may use it before then.
- **After a run**: if any error found on this set is used to change prompts or code, the set becomes a
  development set and must be reported as such.
- **How to run** (later): `uv run python scripts/eval.py --scenario demo/heldout --gold demo/eval/heldout-gold.json`
  with a real backend; the Fake backend has no recorded responses for this set.
