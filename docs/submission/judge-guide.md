# Judge guide

> Short notes for judges trying Decision Ledger at https://decision-ledger-cup.web.app.

## Start

1. Open the site. The first screen is the read-only **showcase**: a synthetic team's decisions, the alerts the system raised, and the pull request checks. The "Recorded run" badge means the showcase was rebuilt from recorded model responses.
2. Press **Sign in with Google** to get your own empty workspace. Only your Google account can open it, and it is saved in the cloud: sign in again from any browser and it is still there, even after we redeploy. Other judges never see your workspace.

## Import the demo sources

- In your workspace, run the demo script step by step, or press the button that runs the whole script.
- **Each import takes 15 seconds to about 4 minutes, depending on model load.** The model reads the whole meeting or Slack export. Please wait for the pipeline to finish before starting the next step. When the model service is busy, a single import can take 3 to 4 minutes; we measured that on 2026-10-17. The page keeps waiting, so there is no need to reload.
- If you see **"The model took longer than expected. Retry, or wait a moment and refresh."** or the server cannot be reached, wait about 30 seconds and press **Retry**.
- If Retry then says “This content was already imported; no new decisions created”, that is expected: the first attempt finished on the server. Continue with the next step.
- If the model is busy you may see "AI model call failed". Wait a minute and press Retry.

## What your workspace shows

Your workspace uses the live model, so its judgements can differ between runs. In particular, the guest checkout pair (the kickoff allows guest checkout; Slack later requires an account) is sometimes judged a supersession instead of a conflict. To see all three alert types reliably (conflict, premise in doubt and PR deviation), look at the read-only showcase, which is rebuilt from a recorded run.

## Replay the pull requests

After the meetings and Slack export are imported, replay the two demo pull requests. One should be flagged for breaking a recorded decision, with the decision quoted and linked; the other should pass. The pull request card is a dry run: nothing is posted to GitHub.

## Daily limits

- Up to **100 new accounts per day** across the site. When the limit is reached, signing in says "Today's new account limit is reached, try tomorrow"; accounts that already have a workspace are not affected.
- Up to **60 AI model calls per workspace per day**, and a site-wide daily cap. Past a limit the app says "Daily AI usage limit reached" and does not call the model.
- Days are counted in UTC: the counters reset at 00:00 UTC, which is 08:00 in Taiwan.

## Reset

**Reset my workspace** (in the account menu) deletes everything in your workspace only; your workspace itself stays.

## GitHub repository (optional)

In **Workspace settings** you can enter a GitHub repository (`owner/name`). The site verifies every webhook with one signing secret that only its operator holds, so registering a repository only takes effect for repositories the operator has connected; for any other repository the setting is stored but no pull requests arrive. To try deviation detection yourself, use **Check a diff** on your workspace's Pull requests page instead (it is disabled on the read-only showcase).
