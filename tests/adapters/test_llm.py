import json
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from decision_ledger.adapters.llm.claude_code import ClaudeCodeLLM
from decision_ledger.adapters.llm.fake import FakeReplayLLM
from decision_ledger.adapters.llm.gemini import GeminiLLM
from decision_ledger.domain.errors import LLMTransportError


class Out(BaseModel):
    value: int = 0
    items: list[str] = []


# --- FakeReplayLLM ----------------------------------------------------------


def test_fake_replays_by_key_and_returns_empty_otherwise(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"k1": {"value": 7}}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"k2": {"value": 8, "items": ["x"]}}), encoding="utf-8")
    llm = FakeReplayLLM(tmp_path)
    assert llm.name == "fake"
    assert llm.generate_json("ignored prompt", Out, key="k1").value == 7
    assert llm.generate_json("p", Out, key="k2").items == ["x"]
    assert llm.generate_json("p", Out, key="missing") == Out()


def test_fake_with_missing_directory_returns_empty(tmp_path):
    assert FakeReplayLLM(tmp_path / "nope").generate_json("p", Out, key="k") == Out()


# --- Gemini -------------------------------------------------------------------


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def gemini(models):
    return GeminiLLM(model="gemini-test", timeout=12, client=SimpleNamespace(models=models))


def test_gemini_passes_schema_and_timeout_and_returns_parsed():
    models = FakeModels(SimpleNamespace(parsed=Out(value=3), text='{"value": 3}'))
    assert gemini(models).generate_json("hello", Out, key="k").value == 3
    call = models.calls[0]
    assert call["model"] == "gemini-test" and call["contents"] == "hello"
    assert call["config"]["response_mime_type"] == "application/json"
    assert call["config"]["response_schema"] is Out
    assert call["config"]["http_options"]["timeout"] == 12000


def test_gemini_falls_back_to_text_and_raises_validation_error_for_bad_json():
    assert gemini(FakeModels(SimpleNamespace(parsed=None, text='{"value": 4}'))).generate_json("p", Out, key="k").value == 4
    with pytest.raises(ValidationError):
        gemini(FakeModels(SimpleNamespace(parsed=None, text='{"value": "x"}'))).generate_json("p", Out, key="k")


def test_gemini_empty_response_and_sdk_errors_are_transport_errors():
    with pytest.raises(LLMTransportError):
        gemini(FakeModels(SimpleNamespace(parsed=None, text=None))).generate_json("p", Out, key="k")
    with pytest.raises(LLMTransportError):
        gemini(FakeModels(error=TimeoutError("deadline"))).generate_json("p", Out, key="k")


def test_gemini_without_sdk_raises_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_genai(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError("no google-genai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_genai)
    with pytest.raises(RuntimeError, match="gemini"):
        GeminiLLM(model="m")


# --- Claude Code --------------------------------------------------------------


class Runner:
    def __init__(self, stdout="", returncode=0, error=None):
        self.stdout, self.returncode, self.error, self.calls = stdout, returncode, error, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(stdout=self.stdout, stderr="err", returncode=self.returncode)


def envelope(result, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": result})


def test_claude_code_sends_prompt_with_schema_on_stdin_and_parses_result():
    runner = Runner(envelope('```json\n{"value": 5}\n```'))
    llm = ClaudeCodeLLM(command="claude", timeout=30, runner=runner)
    assert llm.generate_json("hello", Out, key="k").value == 5
    args, kwargs = runner.calls[0]
    assert args[:4] == ["claude", "-p", "--output-format", "json"]
    assert kwargs["timeout"] == 30 and "hello" in kwargs["input"] and '"value"' in kwargs["input"]


def test_claude_code_invalid_result_json_is_validation_error():
    with pytest.raises(ValidationError):
        ClaudeCodeLLM(runner=Runner(envelope('{"value": "nope"}'))).generate_json("p", Out, key="k")


@pytest.mark.parametrize("runner", [
    Runner(returncode=1),
    Runner(error=subprocess.TimeoutExpired("claude", 1)),
    Runner(error=FileNotFoundError("claude")),
    Runner(stdout="not json"),
    Runner(stdout=envelope("x", is_error=True)),
])
def test_claude_code_failures_are_transport_errors(runner):
    with pytest.raises(LLMTransportError):
        ClaudeCodeLLM(runner=runner).generate_json("p", Out, key="k")


def test_claude_code_runs_without_tools_in_an_isolated_directory(monkeypatch):
    """Review R2-1: untrusted transcripts/diffs must not be able to drive tools or read the project."""
    import os

    monkeypatch.setenv("DL_GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("DL_WEBHOOK_SECRET", "secret-hook")
    runner = Runner(envelope('{"value": 1}'))
    ClaudeCodeLLM(runner=runner).generate_json("p", Out, key="k")
    args, kwargs = runner.calls[0]
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and "--restricted" in args
    assert kwargs["cwd"] != os.getcwd() and not os.path.exists(kwargs["cwd"])  # temp dir, removed after the call
    assert "DL_GITHUB_TOKEN" not in kwargs["env"] and "DL_WEBHOOK_SECRET" not in kwargs["env"]
    assert kwargs["env"].get("PATH") == os.environ.get("PATH")



def test_gemini_requests_temperature_zero():
    """DL-002: deterministic judgements."""
    models = FakeModels(SimpleNamespace(parsed=Out(value=1), text=None))
    gemini(models).generate_json("p", Out, key="k")
    assert models.calls[-1]["config"]["temperature"] == 0



def test_gemini_vertex_client_receives_project_and_location(monkeypatch):
    """DL-002 review R4: the location reaches genai.Client."""
    genai = pytest.importorskip("google.genai")

    seen = {}
    monkeypatch.setattr(genai, "Client", lambda **kwargs: seen.update(kwargs) or SimpleNamespace(models=None))
    GeminiLLM(model="gemini-3.5-flash", vertexai=True, project="p", location="global")
    assert seen == {"vertexai": True, "project": "p", "location": "global"}
