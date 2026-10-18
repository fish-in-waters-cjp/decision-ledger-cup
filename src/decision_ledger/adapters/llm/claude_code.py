"""Claude Code backend: runs the local ``claude`` CLI in print mode."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from decision_ledger.domain.errors import LLMTransportError

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


# Prompts carry untrusted transcripts and PR diffs, so the CLI runs as a pure text model:
# no built-in tools, no MCP servers, no user/project settings, in an empty working directory.
SANDBOX_ARGS = ["--tools", "", "--strict-mcp-config", "--restricted"]


def _child_env() -> dict[str, str]:
    """The parent environment minus this app's own secrets (DL_GITHUB_TOKEN, DL_WEBHOOK_SECRET, ...)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("DL_")}


class ClaudeCodeLLM:
    """Sends the prompt plus the JSON schema on stdin and validates the ``result`` text."""

    name = "claude_code"

    def __init__(self, command: str = "claude", timeout: float = 60.0, runner: Callable[..., Any] = subprocess.run):
        self.command = command
        self.timeout = timeout
        self.runner = runner

    def generate_json(self, prompt: str, response_model: type[M], *, key: str) -> M:
        full_prompt = (
            f"{prompt}\n\nRespond with ONLY a JSON object (no prose) matching this JSON schema:\n"
            f"{json.dumps(response_model.model_json_schema(), ensure_ascii=False)}"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="decision-ledger-claude-") as workdir:
                completed = self.runner(
                    [self.command, "-p", "--output-format", "json", *SANDBOX_ARGS],
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=workdir,
                    env=_child_env(),
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LLMTransportError(f"claude CLI failed to run: {exc}") from exc
        if completed.returncode != 0:
            raise LLMTransportError(f"claude CLI exited with {completed.returncode}: {completed.stderr[:200]}")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LLMTransportError("claude CLI did not return a JSON envelope") from exc
        if not isinstance(envelope, dict) or envelope.get("is_error") or not isinstance(envelope.get("result"), str):
            raise LLMTransportError("claude CLI returned an error result")
        return response_model.model_validate_json(_FENCE.sub("", envelope["result"].strip()))
