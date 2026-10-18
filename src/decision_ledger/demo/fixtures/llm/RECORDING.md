# Recorded demo responses

`demo.json` holds real LLM responses recorded by `scripts/record_demo.py` and replayed by the Fake backend.
The scenario is synthetic: the Atlas team, TrailCart and every person are invented.

- Backend: gemini
- Model: gemini-3.5-flash
- Location: global
- Recorded on: 2026-10-17
- LLM calls in this recording: 17 (recording budget used so far: 72 of 120)
- Summary: {"assumption": 3, "conflict": 1, "decisions": 5, "deviation": 1, "supersession": 1}
- Demo moments: {"assumption": true, "conflict": true, "deviation": true}
- PR violated: {"87": true, "88": false}


## What the recording shows

- Supersession: "move the beta launch from November 3 to November 17" replaces November 3.
- Duplicate: Mika's Slack confirmation of November 17 merges into that decision.
- Conflict: the kickoff allows guest checkout, Slack later says every order needs an account.
  This time Gemini judged it a conflict.
- PR deviation: #87 imports the database client under `src/payments/` and is flagged against
  the payments boundary decision (whole-diff fallback, DL-029); #88 passes.
- Premise change: the weekly sync says QA is four testers now, against the premise
  "QA has two testers until December" behind November 17.
- Two premise alerts are false positives. The new launch date does not make the
  November 10 partner fair untrue, and the Slack thread does not contradict
  "forcing users to sign up causes them to abandon the cart".
- Not extracted: "The order store stays on Postgres."

## Recording history

1. First run (16 calls): conflict and supersession appeared. Services accepted only
   bare decision ids, so a merge and the QA premise finding were dropped. After the
   `decision_ref` fix those responses replayed with answer citations pointing at the
   wrong decisions, so the run was discarded.
2. Second run (17 calls): kept until run 4. Guest checkout judged a supersession; PR #87 not
   flagged because the path pattern missed `src/payments/refunds.ts`.
3. Third run, Slack wording "as agreed at the kickoff": two 504 DEADLINE_EXCEEDED
   errors, then a resumed run that again judged guest checkout a supersession and
   failed on one question with 504. Not kept.
4. Fourth run (17 calls, 2026-10-17, integrated code at feat/v1 c3fa4c6 with the DL-029 path
   fallback, budget raised from 60 to 120 by the commander), kept: the fixtures in this folder.
   All three demo moments appeared in one run; no retries were needed.

Gemini judged the guest checkout reversal a conflict in 2 of 4 runs, so this moment is not stable
across recordings.

## Demo repository

The pull requests live in the private repository `ci-yang/decision-ledger-demo-repo`.
GitHub numbered them #1 (violating, head `f6988fc`) and #2 (compliant, head `1d552e6`);
the scenario calls them #87 and #88. Their diffs match `04-pr-87.diff` and `04-pr-88.diff` line for line.
