"""
Tests for src/agents/llm_backend.py (docs/architecture/lld-sprint2.md §3,
decisions 0004/0005).

No live calls anywhere in this file: `subprocess.run` is mocked for every
ClaudeCodeBackend test, and the `anthropic` SDK's `messages.create` is
mocked for every AnthropicBackend test. Nothing here shells out to the real
`claude` CLI or hits the real Anthropic API, and OllamaBackend's tests
actively assert no network call is attempted at all.

`investigate()`'s tests use fake, generic `mcp_config_path`/`allowed_tools`
values ("fake_mcp_config.json", "tool_a"/"tool_b") — deliberately, per this
slice's explicit scope: this file proves the generic subprocess-construction
/ JSON-parsing / error-classification MECHANISM is correct. Investigator
(Slice 3, not built yet) is what will call investigate() with the real
investigator_mcp_config.json and mcp__datahub__* tool names — there is
nothing DataHub-specific to test here because there is nothing
DataHub-specific in llm_backend.py at all.

Layout mirrors the module's own sections: ClaudeCodeBackend (complete, then
investigate), AnthropicBackend, OllamaBackend, then the get_backend() factory.
"""

import json
import subprocess
from dataclasses import dataclass

import pytest

import agents.llm_backend as llm_backend
from agents.llm_backend import (
    AnthropicBackend,
    BackendCallError,
    BackendNotAvailableError,
    BackendTimeoutError,
    BudgetExhaustedError,
    ClaudeCodeBackend,
    CompletionResult,
    OllamaBackend,
    ToolCall,
    get_backend,
)


# ===========================================================================
# Test doubles / fixtures shared across sections.
# ===========================================================================


@dataclass
class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess — only the fields
    _run_claude()/callers actually read (stdout) matter here."""

    stdout: str
    returncode: int = 0
    stderr: str = ""


def _fake_run_returning(stdout_dict_or_str):
    """Build a fake subprocess.run() replacement that returns a fixed
    response, and records every call it received (cmd, kwargs) on
    `.calls` for assertions on how the command was constructed."""
    stdout = stdout_dict_or_str if isinstance(stdout_dict_or_str, str) else json.dumps(stdout_dict_or_str)
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeCompletedProcess(stdout=stdout)

    _run.calls = calls
    return _run


def _flag_value(cmd: list[str], flag: str) -> str:
    """Pull the value following a --flag out of a constructed argv list —
    used to assert investigate()'s subprocess command was built correctly
    without hardcoding the whole list's exact order."""
    return cmd[cmd.index(flag) + 1]


SUCCESSFUL_CLAUDE_JSON = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2167,
    "num_turns": 1,
    "result": "This is the completion text.",
    "total_cost_usd": 0.0234,
    "usage": {"input_tokens": 120, "output_tokens": 45},
}

# The EXACT shape confirmed live in lld-sprint2.md §0's own smoke test.
BUDGET_EXHAUSTED_CLAUDE_JSON = {
    "type": "result",
    "subtype": "error_max_budget_usd",
    "is_error": True,
    "terminal_reason": "budget_exhausted",
    "duration_ms": 4200,
    "total_cost_usd": 0.05,
    "result": None,
}

# A DIFFERENT is_error=True shape — must NOT be misreported as budget-exhausted.
OTHER_ERROR_CLAUDE_JSON = {
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "error",
    "total_cost_usd": 0.01,
    "result": "Something else went wrong",
}


# ===========================================================================
# ClaudeCodeBackend — construction + complete()
# ===========================================================================


class TestClaudeCodeBackendConstruction:
    def test_fails_clearly_when_cli_missing(self, monkeypatch):
        monkeypatch.setattr(llm_backend.shutil, "which", lambda cmd: None)
        with pytest.raises(BackendNotAvailableError, match="claude.*CLI.*not found"):
            ClaudeCodeBackend()

    def test_succeeds_when_cli_present(self, monkeypatch):
        monkeypatch.setattr(llm_backend.shutil, "which", lambda cmd: "/usr/local/bin/claude")
        backend = ClaudeCodeBackend()
        assert backend.name == "claude_code"
        assert backend.supports_delegated_investigation is True


class TestClaudeCodeBackendComplete:
    @pytest.fixture
    def backend(self, monkeypatch):
        monkeypatch.setattr(llm_backend.shutil, "which", lambda cmd: "/usr/local/bin/claude")
        return ClaudeCodeBackend()

    def test_parses_successful_response(self, backend, monkeypatch):
        fake_run = _fake_run_returning(SUCCESSFUL_CLAUDE_JSON)
        monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)

        result = backend.complete([{"role": "user", "content": "Summarize this finding."}])

        assert isinstance(result, CompletionResult)
        assert result.text == "This is the completion text."
        assert result.usage["cost_usd"] == 0.0234
        assert result.tool_calls == []
        # The prompt string (not a structured messages array) is what
        # actually reaches the `claude` CLI — assert the single plain-string
        # message was passed straight through as the -p argument.
        cmd, _ = fake_run.calls[0]
        assert cmd[:2] == ["claude", "-p"]
        assert cmd[2] == "Summarize this finding."
        assert "--output-format" in cmd and _flag_value(cmd, "--output-format") == "json"
        # complete() never attaches --mcp-config / --allowedTools — §3's
        # "no --mcp-config, no tools" description of this method, checked
        # directly rather than just asserted in prose.
        assert "--mcp-config" not in cmd
        assert "--allowedTools" not in cmd

    def test_rejects_tools(self, backend):
        with pytest.raises(NotImplementedError, match="does not support tools"):
            backend.complete(
                [{"role": "user", "content": "hi"}],
                tools=[{"name": "some_tool", "input_schema": {}}],
            )

    def test_raises_backend_not_available_if_cli_disappears_before_call(self, backend, monkeypatch):
        def _raise_fnf(cmd, **kwargs):
            raise FileNotFoundError("claude")

        monkeypatch.setattr(llm_backend.subprocess, "run", _raise_fnf)
        with pytest.raises(BackendNotAvailableError):
            backend.complete([{"role": "user", "content": "hi"}])

    def test_raises_timeout_on_subprocess_timeout(self, backend, monkeypatch):
        def _raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 60))

        monkeypatch.setattr(llm_backend.subprocess, "run", _raise_timeout)
        with pytest.raises(BackendTimeoutError):
            backend.complete([{"role": "user", "content": "hi"}], timeout_s=5)

    def test_raises_backend_call_error_on_malformed_json(self, backend, monkeypatch):
        monkeypatch.setattr(llm_backend.subprocess, "run", _fake_run_returning("not json at all {{{"))
        with pytest.raises(BackendCallError, match="non-JSON"):
            backend.complete([{"role": "user", "content": "hi"}])

    def test_raises_budget_exhausted_on_exact_shape(self, backend, monkeypatch):
        monkeypatch.setattr(llm_backend.subprocess, "run", _fake_run_returning(BUDGET_EXHAUSTED_CLAUDE_JSON))
        with pytest.raises(BudgetExhaustedError):
            backend.complete([{"role": "user", "content": "hi"}])

    def test_other_error_shape_is_not_misreported_as_budget_exhausted(self, backend, monkeypatch):
        monkeypatch.setattr(llm_backend.subprocess, "run", _fake_run_returning(OTHER_ERROR_CLAUDE_JSON))
        with pytest.raises(BackendCallError) as exc_info:
            backend.complete([{"role": "user", "content": "hi"}])
        # The whole point of the specific-shape check: this must raise the
        # generic BackendCallError, never BudgetExhaustedError, for a
        # differently-shaped is_error response.
        assert not isinstance(exc_info.value, BudgetExhaustedError)

    def test_multi_message_history_is_rendered_as_role_prefixed_text(self, backend, monkeypatch):
        fake_run = _fake_run_returning(SUCCESSFUL_CLAUDE_JSON)
        monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)
        backend.complete(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ]
        )
        cmd, _ = fake_run.calls[0]
        prompt = cmd[2]
        assert "user: first" in prompt
        assert "assistant: second" in prompt
        assert "user: third" in prompt

    def test_rejects_structured_tool_result_content(self, backend):
        with pytest.raises(NotImplementedError, match="only supports plain string"):
            backend.complete(
                [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "y"}]}]
            )


# ===========================================================================
# ClaudeCodeBackend — investigate() (generic mechanism, fake mcp/tool values)
# ===========================================================================


class TestClaudeCodeBackendInvestigate:
    @pytest.fixture
    def backend(self, monkeypatch):
        monkeypatch.setattr(llm_backend.shutil, "which", lambda cmd: "/usr/local/bin/claude")
        return ClaudeCodeBackend()

    def test_builds_expected_subprocess_command(self, backend, monkeypatch):
        fake_run = _fake_run_returning(SUCCESSFUL_CLAUDE_JSON)
        monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)

        backend.investigate(
            task_prompt="Investigate this fabricated anomaly.",
            mcp_config_path="fake_mcp_config.json",  # generic, NOT investigator_mcp_config.json — Slice 3's job
            allowed_tools=["tool_a", "tool_b"],  # generic, NOT mcp__datahub__* — Slice 3's job
            max_budget_usd=0.75,
            timeout_s=30,
        )

        cmd, kwargs = fake_run.calls[0]
        assert cmd[:2] == ["claude", "-p"]
        assert cmd[2] == "Investigate this fabricated anomaly."
        assert _flag_value(cmd, "--mcp-config") == "fake_mcp_config.json"
        assert "--strict-mcp-config" in cmd
        assert _flag_value(cmd, "--output-format") == "json"
        assert _flag_value(cmd, "--permission-mode") == "bypassPermissions"
        assert _flag_value(cmd, "--allowedTools") == "tool_a,tool_b"
        assert _flag_value(cmd, "--max-budget-usd") == "0.75"
        assert kwargs.get("timeout") == 30

    def test_allowed_tools_accepts_already_joined_string(self, backend, monkeypatch):
        fake_run = _fake_run_returning(SUCCESSFUL_CLAUDE_JSON)
        monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)
        backend.investigate("task", "fake.json", "tool_a,tool_b,tool_c", max_budget_usd=1.0)
        cmd, _ = fake_run.calls[0]
        assert _flag_value(cmd, "--allowedTools") == "tool_a,tool_b,tool_c"

    def test_parses_successful_investigation_result(self, backend, monkeypatch):
        monkeypatch.setattr(llm_backend.subprocess, "run", _fake_run_returning(SUCCESSFUL_CLAUDE_JSON))
        result = backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=1.0)

        assert result.result_text == "This is the completion text."
        assert result.is_error is False
        assert result.cost_usd == 0.0234
        assert result.duration_ms == 2167
        assert result.turns == 1

    def test_raises_budget_exhausted_on_exact_shape(self, backend, monkeypatch):
        monkeypatch.setattr(llm_backend.subprocess, "run", _fake_run_returning(BUDGET_EXHAUSTED_CLAUDE_JSON))
        with pytest.raises(BudgetExhaustedError):
            backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=0.05)

    def test_raises_timeout(self, backend, monkeypatch):
        def _raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(llm_backend.subprocess, "run", _raise_timeout)
        with pytest.raises(BackendTimeoutError):
            backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=1.0, timeout_s=1)

    def test_uses_default_investigate_timeout_when_not_specified(self, backend, monkeypatch):
        fake_run = _fake_run_returning(SUCCESSFUL_CLAUDE_JSON)
        monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)
        backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=1.0)
        _, kwargs = fake_run.calls[0]
        assert kwargs.get("timeout") == llm_backend.DEFAULT_INVESTIGATE_TIMEOUT_S


# ===========================================================================
# AnthropicBackend
# ===========================================================================


class _FakeAnthropicUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _FakeAnthropicMessage:
    def __init__(self, content, stop_reason="end_turn", input_tokens=50, output_tokens=20):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeAnthropicUsage(input_tokens, output_tokens)


class TestAnthropicBackendConstruction:
    def test_fails_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(BackendNotAvailableError, match="ANTHROPIC_API_KEY"):
            AnthropicBackend()

    def test_succeeds_with_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        backend = AnthropicBackend()
        assert backend.name == "anthropic"
        assert backend.supports_delegated_investigation is False

    def test_default_model_is_overridable_via_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-some-other-model")
        backend = AnthropicBackend()
        assert backend.model == "claude-some-other-model"

    def test_explicit_model_argument_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-model")
        backend = AnthropicBackend(model="claude-explicit-model")
        assert backend.model == "claude-explicit-model"


class TestAnthropicBackendComplete:
    @pytest.fixture
    def backend(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        return AnthropicBackend()

    def test_parses_successful_completion(self, backend, monkeypatch):
        fake_message = _FakeAnthropicMessage(content=[_FakeTextBlock("hello from the model")])
        monkeypatch.setattr(backend._client.messages, "create", lambda **kwargs: fake_message)

        result = backend.complete([{"role": "user", "content": "hi"}])

        assert isinstance(result, CompletionResult)
        assert result.text == "hello from the model"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 50, "output_tokens": 20}
        assert result.tool_calls == []

    def test_parses_tool_use_blocks(self, backend, monkeypatch):
        fake_message = _FakeAnthropicMessage(
            content=[
                _FakeTextBlock("let me check that"),
                _FakeToolUseBlock(id="tool_1", name="some_tool", input={"query": "x"}),
            ],
            stop_reason="tool_use",
        )
        monkeypatch.setattr(backend._client.messages, "create", lambda **kwargs: fake_message)

        result = backend.complete(
            [{"role": "user", "content": "hi"}],
            tools=[{"name": "some_tool", "input_schema": {"type": "object"}}],
        )

        assert result.stop_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0] == ToolCall(id="tool_1", name="some_tool", input={"query": "x"})

    def test_maps_authentication_error(self, backend, monkeypatch):
        import httpx
        import anthropic

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(401, request=req)
        err = anthropic.AuthenticationError("invalid x-api-key", response=resp, body=None)

        def _raise(**kwargs):
            raise err

        monkeypatch.setattr(backend._client.messages, "create", _raise)
        with pytest.raises(BackendNotAvailableError):
            backend.complete([{"role": "user", "content": "hi"}])

    def test_maps_timeout_error(self, backend, monkeypatch):
        import httpx
        import anthropic

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        err = anthropic.APITimeoutError(request=req)

        def _raise(**kwargs):
            raise err

        monkeypatch.setattr(backend._client.messages, "create", _raise)
        with pytest.raises(BackendTimeoutError):
            backend.complete([{"role": "user", "content": "hi"}])

    def test_maps_other_anthropic_errors_to_backend_call_error(self, backend, monkeypatch):
        import httpx
        import anthropic

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(429, request=req)
        err = anthropic.RateLimitError("rate limited", response=resp, body=None)

        def _raise(**kwargs):
            raise err

        monkeypatch.setattr(backend._client.messages, "create", _raise)
        with pytest.raises(BackendCallError) as exc_info:
            backend.complete([{"role": "user", "content": "hi"}])
        # A rate limit is explicitly NOT a BudgetExhaustedError — see the
        # module's own inline reasoning: that concept is specific to
        # claude -p's --max-budget-usd cap, not a bare API call.
        assert not isinstance(exc_info.value, BudgetExhaustedError)


class TestAnthropicBackendInvestigate:
    def test_not_implemented(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        backend = AnthropicBackend()
        assert backend.supports_delegated_investigation is False
        with pytest.raises(NotImplementedError, match="does not support delegated investigation"):
            backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=1.0)


# ===========================================================================
# OllamaBackend — stub, no network calls attempted.
# ===========================================================================


class TestOllamaBackend:
    def test_complete_raises_not_implemented(self):
        backend = OllamaBackend()
        with pytest.raises(NotImplementedError, match="not implemented this sprint"):
            backend.complete([{"role": "user", "content": "hi"}])

    def test_investigate_raises_not_implemented(self):
        backend = OllamaBackend()
        with pytest.raises(NotImplementedError, match="does not support delegated investigation"):
            backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=1.0)

    def test_no_network_call_is_ever_attempted(self, monkeypatch):
        """The load-bearing proof for this backend: patch socket.socket
        (the lowest-level primitive any real HTTP call would eventually go
        through, whether via `requests`/`httpx`/stdlib `http.client`) and
        assert it's never constructed, across both complete() and
        investigate() raising. If a future edit accidentally added a real
        HTTP call before the NotImplementedError, this test would catch it
        even though OllamaBackend doesn't import requests/httpx today.
        """
        import socket

        calls = []
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: calls.append((a, kw)))

        backend = OllamaBackend()
        with pytest.raises(NotImplementedError):
            backend.complete([{"role": "user", "content": "hi"}])
        with pytest.raises(NotImplementedError):
            backend.investigate("task", "fake.json", ["tool_a"], max_budget_usd=1.0)

        assert calls == []


# ===========================================================================
# get_backend() factory
# ===========================================================================


class TestGetBackendFactory:
    def test_returns_claude_code_backend(self, monkeypatch):
        monkeypatch.setattr(llm_backend.shutil, "which", lambda cmd: "/usr/local/bin/claude")
        backend = get_backend("claude_code")
        assert isinstance(backend, ClaudeCodeBackend)

    def test_returns_anthropic_backend(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        backend = get_backend("anthropic")
        assert isinstance(backend, AnthropicBackend)

    def test_returns_ollama_backend(self):
        backend = get_backend("ollama")
        assert isinstance(backend, OllamaBackend)

    def test_unknown_name_raises_value_error(self):
        with pytest.raises(ValueError, match="Unrecognized LLM_BACKEND"):
            get_backend("some_backend_that_does_not_exist")

    def test_reads_llm_backend_from_env_when_name_is_none(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        backend = get_backend()
        assert isinstance(backend, OllamaBackend)

    def test_defaults_to_claude_code_when_env_var_is_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        monkeypatch.setattr(llm_backend.shutil, "which", lambda cmd: "/usr/local/bin/claude")
        backend = get_backend()
        assert isinstance(backend, ClaudeCodeBackend)

    def test_kwargs_pass_through_to_backend_constructor(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
        backend = get_backend("anthropic", model="claude-custom-model")
        assert backend.model == "claude-custom-model"
