import pytest

from decision_ledger.domain.models import AlertKind, CheckableRule, PullRequestDiff, PullRequestEvent
from decision_ledger.services.deviation import DeviationService, idempotency_key, parse_diff, render_comment
from tests.factories import decision, ev
from tests.fakes import FakeLLM, FakePullRequestSource, FakeRepository, RecordingPublisher

DIFF = """diff --git a/src/report/service.py b/src/report/service.py
--- a/src/report/service.py
+++ b/src/report/service.py
@@ -9,5 +9,8 @@
 from app.api import internal_client
+from app.db import db_client

 def monthly_summary(org_id: str, month: str):
-    rows = internal_client.get_usage(org_id, month)
+    rows = db_client.query("SELECT * FROM usage")
     return summarize(rows)
diff --git a/docs/readme.md b/docs/readme.md
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1,1 +1,1 @@
-old docs
+SECRET_DOCS_LINE
"""


def pr(**kw):
    return PullRequestDiff(repo="acme/app", pr_number=87, head_sha="abc", title="報表加上月度匯總", author="kevin", diff=DIFF, **kw)


def setup(responses=None, publisher=None, limit=20000, decisions=None):
    repo = FakeRepository()
    rule = CheckableRule(description="report-service 不得直連資料庫", path_patterns=["src/report/*"])
    for d in decisions or [decision("23", topic="服務架構", statement="report-service 一律走 internal API", rule=rule)]:
        repo.decisions[d.id] = d
    repo.decisions["23"] = repo.decisions["23"].model_copy(update={"evidence": [ev("report-service 之後一律走 internal API", locator="00:18:20", speaker="Dave")]})
    llm = FakeLLM(responses or {})
    publisher = publisher or RecordingPublisher()
    svc = DeviationService(repo, llm, publisher, FakePullRequestSource(DIFF), diff_char_limit=limit)
    return svc, repo, llm, publisher


VIOLATION = {"DeviationJudgement": {"violations": [
    {"decision_id": "23", "file": "src/report/service.py", "line": 10, "snippet": "from app.db import db_client", "explanation": "直連 DB"}
]}}


def test_violation_creates_alert_and_comment_with_decision_reason_location_and_quote():
    svc, repo, llm, publisher = setup(VIOLATION)
    result = svc.check(pr())
    assert result.check.violated and not result.reused
    alert = repo.alerts[result.check.alert_ids[0]]
    assert alert.kind is AlertKind.DEVIATION and alert.decision_ids == ["23"]
    assert alert.location == "src/report/service.py:10" and alert.pr_check_key == result.check.key
    comment = result.check.comment
    for part in ["#23", "report-service 一律走 internal API", "Rationale", "src/report/service.py:10", "report-service 之後一律走 internal API"]:
        assert part in comment
    assert publisher.published == [(pr(), comment)]


def test_no_violation_comment_and_no_alert():
    svc, repo, llm, _ = setup({"DeviationJudgement": {"violations": []}})
    result = svc.check(pr())
    assert not result.check.violated and repo.alerts == {}
    assert "no violation found" in result.check.comment and "#23" in result.check.comment
    assert "whole diff" not in result.check.comment


def test_unrelated_files_are_not_sent_to_llm():
    svc, _, llm, _ = setup(VIOLATION)
    svc.check(pr())
    prompt = llm.calls[0][2]
    assert "db_client" in prompt and "SECRET_DOCS_LINE" not in prompt


def test_no_matching_files_over_limit_skips_llm():
    """DL-029 US-1.2: the whole-diff fallback is bounded by the diff character limit."""
    svc, repo, llm, _ = setup(limit=40)
    result = svc.check(PullRequestDiff(diff=DIFF.split("diff --git a/docs")[0].replace("src/report", "src/web")))
    assert llm.calls == [] and "no checkable decision relates to this PR" in result.check.comment
    assert repo.alerts == {}


def test_large_diff_is_truncated_and_noted():
    svc, _, llm, _ = setup({"DeviationJudgement": {"violations": []}}, limit=40)
    result = svc.check(pr())
    assert result.check.truncated and "truncated" in result.check.comment
    assert "db_client.query" not in llm.calls[0][2]


def test_resend_is_idempotent_no_duplicate_alert_or_publish():
    svc, repo, llm, publisher = setup(VIOLATION)
    first = svc.check(pr())
    again = svc.check(pr())
    assert again.reused and again.check.key == first.check.key
    assert len(repo.alerts) == 1 and len(publisher.published) == 1 and len(llm.calls) == 1
    assert [a.id for a in again.alerts] == first.check.alert_ids


def test_multiple_violations_in_one_pr_each_get_an_alert():
    rule = CheckableRule(description="snake_case", path_patterns=["src/report/*"])
    extra = decision("18", topic="API 規範", statement="API 欄位一律 snake_case", rule=rule)
    base = decision("23", topic="服務架構", statement="report-service 一律走 internal API",
                    rule=CheckableRule(description="x", path_patterns=["src/report/*"]))
    responses = {"DeviationJudgement": {"violations": [
        {"decision_id": "23", "file": "src/report/service.py", "line": 10, "snippet": "a"},
        {"decision_id": "18", "file": "src/report/service.py", "line": 13, "snippet": "b"},
        {"decision_id": "999", "file": "src/report/service.py", "line": 1, "snippet": "unknown decision is ignored"},
    ]}}
    svc, repo, _, _ = setup(responses, decisions=[base, extra])
    result = svc.check(pr())
    assert sorted(repo.alerts[i].decision_ids[0] for i in result.check.alert_ids) == ["18", "23"]


def test_publish_result_is_recorded_and_dry_run_is_none():
    svc, repo, _, _ = setup(VIOLATION, publisher=RecordingPublisher(result=False))
    result = svc.check(pr())
    assert result.check.comment_published is False
    assert repo.pr_checks[result.check.key].comment_published is False

    svc, repo, _, _ = setup(VIOLATION)
    assert svc.check(pr()).check.comment_published is None


def test_check_webhook_fetches_diff_through_source():
    svc, _, _, _ = setup(VIOLATION)
    event = PullRequestEvent(action="opened", repo="acme/app", pr_number=87, head_sha="abc")
    result = svc.check_webhook(event)
    assert result.check.pr_number == 87 and result.check.violated
    assert svc.pr_source.calls == [event]


def test_idempotency_key_prefers_pr_identity_then_diff_hash():
    assert idempotency_key(pr()) == "acme/app#87@abc"
    local = idempotency_key(PullRequestDiff(diff="x"))
    assert local.startswith("sha256:") and local == idempotency_key(PullRequestDiff(diff="x"))
    assert local != idempotency_key(PullRequestDiff(diff="y"))


def test_parse_diff_tracks_new_line_numbers():
    files = parse_diff(DIFF)
    lines = files["src/report/service.py"]
    added = [(n, t) for n, k, t in lines if k == "+"]
    assert added[0] == (10, "from app.db import db_client")
    assert "docs/readme.md" in files


def test_render_comment_states_it_never_blocks_merge():
    assert "never blocks a merge" in render_comment([], [], truncated=False)


@pytest.mark.parametrize("pattern,path,expected", [
    ("src/report/*", "src/report/service.py", True),
    ("src/report", "src/report/a/b.py", True),
    ("*.sql", "db/migrate.sql", True),
    ("src/report/*", "src/web/app.py", False),
])
def test_path_patterns(pattern, path, expected):
    from decision_ledger.services.deviation import matches

    assert matches(path, [pattern]) is expected


def test_decision_superseded_during_llm_call_gets_no_deviation_alert():
    """Review R1-2 (class sweep): re-validate the decision inside the write transaction."""
    svc, repo, llm, _ = setup()

    def call(prompt, key):
        repo.decisions["23"] = repo.decisions["23"].model_copy(update={"status": "superseded", "superseded_by": "99"})
        return VIOLATION["DeviationJudgement"]

    llm.responses["DeviationJudgement"] = call
    result = svc.check(pr())
    assert repo.alerts == {} and not result.check.violated
    assert "no violation found" in result.check.comment


def test_check_writes_only_into_its_own_project():
    svc, repo, _, _ = setup(VIOLATION)
    result = svc.check(pr())
    assert repo.project_id == "P1"
    assert repo.pr_checks[result.check.key].project_id == "P1"
    assert all(a.project_id == "P1" for a in repo.alerts.values())


# --- DL-029: whole-diff fallback, per-decision dedupe, statement-matching quote ---

GUESSED = CheckableRule(description="report-service must call the internal API", path_patterns=["report-service/**"])


def _repo_with(*decisions):
    repo = FakeRepository()
    for d in decisions:
        repo.decisions[d.id] = d
    return repo


def test_unmatched_globs_fall_back_to_whole_diff_within_limit():
    """DL-029 US-1.1: a guessed glob that matches no file still reaches the model."""
    repo = _repo_with(decision("23", statement="report-service always goes through the internal API", rule=GUESSED))
    llm = FakeLLM(VIOLATION)
    svc = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF))
    result = svc.check(pr())
    assert len(llm.calls) == 1
    prompt = llm.calls[0][2]
    assert "src/report/service.py" in prompt and "db_client.query" in prompt and "#23" in prompt
    assert result.check.violated and len(result.alerts) == 1
    assert repo.alerts[result.check.alert_ids[0]].decision_ids == ["23"]


def test_fallback_prompt_says_paths_are_guesses():
    repo = _repo_with(decision("23", rule=GUESSED))
    llm = FakeLLM({"DeviationJudgement": {"violations": []}})
    DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF)).check(pr())
    assert "whole diff" in llm.calls[0][2].lower() and "guesses" in llm.calls[0][2]


def test_fallback_pass_comment_says_whole_diff_was_compared():
    repo = _repo_with(decision("23", rule=GUESSED))
    llm = FakeLLM({"DeviationJudgement": {"violations": []}})
    result = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF)).check(pr())
    assert "compared the whole diff" in result.check.comment and "#23" in result.check.comment
    assert "no violation found" in result.check.comment


def test_unmatched_globs_over_limit_do_not_call_model():
    """DL-029 US-1.2."""
    repo = _repo_with(decision("23", rule=GUESSED))
    llm = FakeLLM(VIOLATION)
    result = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF), diff_char_limit=40).check(pr())
    assert llm.calls == [] and not result.check.violated
    assert "no checkable decision relates to this PR" in result.check.comment


def test_no_checkable_decisions_never_calls_model():
    """DL-029 US-1.3."""
    repo = _repo_with(decision("23", rule=None))
    llm = FakeLLM(VIOLATION)
    result = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF)).check(pr())
    assert llm.calls == [] and repo.alerts == {}


def test_same_decision_violated_twice_counts_once():
    """DL-029 US-2.1: one alert per decision; the other line goes into the alert summary."""
    responses = {"DeviationJudgement": {"violations": [
        {"decision_id": "23", "file": "src/report/service.py", "line": 10, "snippet": "from app.db import db_client", "explanation": "imports the DB client"},
        {"decision_id": "#23", "file": "src/report/service.py", "line": 13, "snippet": "db_client.query", "explanation": "queries the DB"},
    ]}}
    svc, repo, _, _ = setup(responses)
    result = svc.check(pr())
    assert len(result.alerts) == 1 and len(repo.alerts) == 1
    alert = result.alerts[0]
    assert alert.location == "src/report/service.py:10"
    assert "src/report/service.py:13" in alert.summary
    comment = result.check.comment
    assert "may violate 1 architecture decision**" in comment
    assert comment.count("### Decision #23") == 1
    assert "src/report/service.py:10" in comment and "src/report/service.py:13" in comment


def test_comment_quotes_the_evidence_closest_to_the_statement():
    """DL-029 US-2.2: prefer the decision sentence over the rationale sentence."""
    rationale = ev("Because the billing tables are shared, we cannot risk direct queries.", locator="00:18:10")
    statement_quote = ev("From now on report-service always goes through the internal API.", locator="00:18:20")
    d = decision("23", statement="report-service always goes through the internal API",
                 rule=CheckableRule(description="x", path_patterns=["src/report/*"]))
    d = d.model_copy(update={"evidence": [rationale, statement_quote]})
    repo = _repo_with(d)
    result = DeviationService(repo, FakeLLM(VIOLATION), RecordingPublisher(), FakePullRequestSource(DIFF)).check(pr())
    assert statement_quote.quote in result.check.comment
    assert rationale.quote not in result.check.comment
    assert result.alerts[0].evidence[0] == statement_quote


def test_best_evidence_ties_keep_first():
    from decision_ledger.services.deviation import best_evidence

    first, second = ev("alpha"), ev("beta")
    d = decision("1", statement="unrelated words").model_copy(update={"evidence": [first, second]})
    assert best_evidence(d) == first


def test_partially_matched_globs_still_send_unanchored_decisions():
    """DL-029 (Gemini run): one decision's broad glob matches, another's guessed glob matches nothing.

    The decision whose glob matched nothing must still reach the model with the whole diff.
    """
    broad = decision("3", statement="API fields use snake_case",
                     rule=CheckableRule(description="snake_case", path_patterns=["**/*.py"]))
    guessed = decision("5", statement="report-service always goes through the internal API", rule=GUESSED)
    repo = _repo_with(broad, guessed)
    responses = {"DeviationJudgement": {"violations": [
        {"decision_id": "5", "file": "src/report/service.py", "line": 10, "snippet": "from app.db import db_client"}
    ]}}
    llm = FakeLLM(responses)
    result = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF)).check(pr())
    prompt = llm.calls[0][2]
    assert "#5" in prompt and "#3" in prompt and "whole diff" in prompt.lower()
    assert result.check.violated and repo.alerts[result.check.alert_ids[0]].decision_ids == ["5"]


def test_partially_matched_globs_over_limit_send_only_matched_files():
    broad = decision("3", statement="API fields use snake_case",
                     rule=CheckableRule(description="snake_case", path_patterns=["src/report/*"]))
    guessed = decision("5", statement="report-service always goes through the internal API", rule=GUESSED)
    repo = _repo_with(broad, guessed)
    whole = len(DIFF)
    llm = FakeLLM({"DeviationJudgement": {"violations": []}})
    svc = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF), diff_char_limit=whole // 2)
    svc.check(pr())
    prompt = llm.calls[0][2]
    assert "#3" in prompt and "#5" not in prompt and "SECRET_DOCS_LINE" not in prompt


def test_identical_violations_are_listed_once():
    """Review R1 F-2: a repeated (file, line) for one decision is not listed twice."""
    same = {"decision_id": "23", "file": "src/report/service.py", "line": 10, "snippet": "x", "explanation": "direct DB"}
    svc, _, _, _ = setup({"DeviationJudgement": {"violations": [same, dict(same)]}})
    result = svc.check(pr())
    assert "see also" not in result.alerts[0].summary
    assert result.check.comment.count("src/report/service.py:10") == 1


def test_fallback_boundary_is_inclusive_and_never_truncated():
    """Review R1 F-3: whole diff exactly at the limit is sent untruncated."""
    from decision_ledger.services.deviation import _format_files

    whole = len(_format_files(parse_diff(DIFF)))
    repo = _repo_with(decision("23", rule=GUESSED))
    llm = FakeLLM({"DeviationJudgement": {"violations": []}})
    result = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF), diff_char_limit=whole).check(pr())
    assert len(llm.calls) == 1 and not result.check.truncated
    assert "SECRET_DOCS_LINE" in llm.calls[0][2]


def test_matched_path_keeps_relevant_header():
    """Review R1 F-3: the whole-diff header is only used on the fallback path."""
    svc, _, llm, _ = setup(VIOLATION)
    svc.check(pr())
    assert "only files relevant" in llm.calls[0][2] and "whole diff" not in llm.calls[0][2].lower()



def test_resolve_webhook_only_fetches_the_diff():
    """DL-003: the webhook resolves the diff synchronously and checks in the background."""
    svc, _, llm, _ = setup(VIOLATION)
    event = PullRequestEvent(action="opened", repo="acme/app", pr_number=87, head_sha="abc")
    pr_diff = svc.resolve_webhook(event)
    assert pr_diff.diff == DIFF and pr_diff.pr_number == 87 and llm.calls == []



# --- DL-004: product deep links, dry-run only ---

def test_comment_links_each_violated_decision_when_public_url_is_set():
    repo = FakeRepository()
    rule = CheckableRule(description="r", path_patterns=["src/report/*"])
    repo.decisions["23"] = decision("23", statement="report-service always goes through the internal API", rule=rule)
    svc = DeviationService(repo, FakeLLM(VIOLATION), RecordingPublisher(), FakePullRequestSource(DIFF),
                           public_url="https://ledger.example.app")
    result = svc.check(pr())
    comment = result.check.comment
    assert "https://ledger.example.app/#p=P1&d=23" in comment
    assert comment.count("#p=P1&d=") == 1
    assert svc.publisher.published == [(pr(), comment)]


def test_comment_has_no_link_without_public_url():
    svc, _, _, _ = setup(VIOLATION)
    assert "#p=" not in svc.check(pr()).check.comment


def test_passing_comment_has_no_links():
    repo = FakeRepository()
    repo.decisions["23"] = decision("23", rule=CheckableRule(description="r", path_patterns=["src/report/*"]))
    svc = DeviationService(repo, FakeLLM({"DeviationJudgement": {"violations": []}}), RecordingPublisher(),
                           FakePullRequestSource(DIFF), public_url="https://ledger.example.app")
    assert "#p=" not in svc.check(pr()).check.comment



def test_every_violated_decision_gets_its_own_link():
    """DL-004 review F-7: two violated decisions, two links."""
    rule = CheckableRule(description="r", path_patterns=["src/report/*"])
    repo = FakeRepository()
    repo.decisions["23"] = decision("23", statement="report-service goes through the internal API", rule=rule)
    repo.decisions["18"] = decision("18", statement="API fields use snake_case", rule=rule)
    responses = {"DeviationJudgement": {"violations": [
        {"decision_id": "23", "file": "src/report/service.py", "line": 10},
        {"decision_id": "18", "file": "src/report/service.py", "line": 13},
    ]}}
    svc = DeviationService(repo, FakeLLM(responses), RecordingPublisher(), FakePullRequestSource(DIFF),
                           public_url="https://ledger.example.app")
    comment = svc.check(pr()).check.comment
    assert "#p=P1&d=23" in comment and "#p=P1&d=18" in comment



# --- DL-017: replay readiness ---

def _replay_service(requires=()):
    repo = FakeRepository()
    replay = PullRequestDiff(repo="acme/app", pr_number=87, head_sha="r87", title="violating", diff=DIFF)
    llm = FakeLLM(VIOLATION)
    svc = DeviationService(repo, llm, RecordingPublisher(), FakePullRequestSource(DIFF),
                           replays={87: replay}, replay_requires=requires)
    return svc, repo, llm


def test_replay_with_part_of_the_demo_sources_is_not_ready_and_writes_nothing():
    from decision_ledger.domain.errors import ReplayNotReady
    from decision_ledger.domain.models import Source

    svc, repo, llm = _replay_service(requires=("hash-weekly", "hash-architecture"))
    repo.decisions["3"] = decision("3", statement="API fields use snake_case",
                                   rule=CheckableRule(description="r", path_patterns=["src/report/*"]))
    repo.sources["S1"] = Source(id="S1", kind="transcript", title="weekly", content_hash="hash-weekly", segments=[])
    with pytest.raises(ReplayNotReady):
        svc.replay(87)
    assert repo.pr_checks == {} and llm.calls == []


def test_replay_when_all_demo_sources_are_in_runs_the_check():
    from decision_ledger.domain.models import Source

    svc, repo, llm = _replay_service(requires=("hash-weekly",))
    repo.decisions["23"] = decision("23", rule=CheckableRule(description="r", path_patterns=["src/report/*"]))
    repo.sources["S1"] = Source(id="S1", kind="transcript", title="weekly", content_hash="hash-weekly", segments=[])
    result = svc.replay(87)
    assert result.check.violated and result.check.pr_number == 87 and len(llm.calls) == 1
