Decision Ledger is decision memory for the future of work: it pulls decisions out of meetings and Slack, links each to its evidence, and checks pull requests against them.

Firebase Hosting fronts the public URL, rewriting pages and short API calls to Cloud Run, which serves the app; slow model calls go straight to Cloud Run.

Firebase Authentication signs people in with Google; the API verifies each ID token and gives every account its own workspace.

Firestore holds every ledger and usage counter, so workspaces survive redeploys; clients never touch it directly.

Cloud Run runs the FastAPI app. A signed GitHub webhook returns 202 and checks the pull request in the background for every workspace following the repo.

Gemini (gemini-3.5-flash on Vertex AI, global endpoint) extracts decisions with their sources, reconciles them against earlier decisions, and judges whether a pull request breaks one. The read-only showcase lives in Firestore; if missing, it is rebuilt from recorded responses at no model cost.
