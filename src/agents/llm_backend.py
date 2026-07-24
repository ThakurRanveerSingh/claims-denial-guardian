#!/usr/bin/env python3
"""
LLMBackend — the pluggable interface every LLM-backed agent in this codebase
talks to. Design: docs/architecture/lld-sprint2.md §3; the split between
"our own loop" and "delegate to claude -p" is decision 0004; the explicit
`supports_delegated_investigation` flag (rather than a caught exception) is
decision 0005. Read both before changing this file's shape.

Three backends, selected via `.env`'s `LLM_BACKEND=claude_code|anthropic|ollama`:

  - ClaudeCodeBackend  — shells out to the `claude` CLI. `complete()` is a
    thin, tool-free, single-turn wrapper (narration only). `investigate()`
    is the REAL mechanism: one `claude -p --mcp-config ...` subprocess call
    delegates an entire multi-turn investigation to Claude Code's own agent
    harness (decision 0004 — this is the only backend that already IS an
    agent, so it's the only one worth delegating to rather than looping
    around).
  - AnthropicBackend   — direct `anthropic` SDK calls. `complete()` is a
    real, working Design-A completion. `investigate()` is NOT implemented
    (inherited from the base class) — there's no agent harness to delegate
    to; a future Investigator (Slice 3) runs its own turn-by-turn loop
    against this backend's `complete()` instead (decision 0005).
  - OllamaBackend      — interface-only stub. `ollama` isn't installed on
    this machine and no working local-model integration is in scope this
    sprint (an already-approved cut, not a gap). `complete()` raises
    immediately; no network call of any kind is attempted.

What THIS slice explicitly does and does not build: `investigate()`'s
mechanism (the subprocess construction, JSON parsing, error classification)
is built generically and completely here, and tested with fake/generic
`mcp_config_path`/`allowed_tools` values — there is no DataHub-specific
content anywhere in this file. Investigator (Slice 3, not built yet) is what
will call `investigate()` with the real `investigator_mcp_config.json` and
`mcp__datahub__*` tool names. This module has zero knowledge of DataHub,
`healthcare.db`, or any investigation-specific prompt/tool shape — same
"generic, reusable backend abstraction" boundary decision 0005 draws
explicitly (its second rejected alternative explains why that boundary
matters: a future agent, e.g. Remediator, should be able to reuse this file
untouched for a completely different tool set).

Also not built here (explicitly out of scope): nothing in this file is wired
into src/agents/sentinel.py's `narrate_fn` seam. That parameter still exists
and is still unused, exactly as Slice 1 left it — wiring an LLM-backed
narrator into Sentinel is a separate, later decision, not a consequence of
this backend interface existing.
"""

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

# Same load_dotenv() convention as src/datahub/add_lineage.py, add_metadata.py,
# register_ml_model.py, and src/agents/sentinel.py — one .env-loading pattern
# across the whole repo.
load_dotenv()

# --- Implementation-time defaults (named explicitly, not buried — same
# convention src/datahub/generate_denials.py/score_claims.py use for their
# own tunables). ---
DEFAULT_MAX_TOKENS = 4096  # LLD §5: "a starting default... not deeply tuned, easy to raise if truncation shows up"
DEFAULT_COMPLETE_TIMEOUT_S = 60.0  # complete() is a single turn; a bare API/CLI call hanging for a full minute is already unusual
DEFAULT_INVESTIGATE_TIMEOUT_S = 240.0  # 4 minutes — within LLD §5's suggested "e.g. 3-5 minutes" wall-clock backstop for Design B


# ---------------------------------------------------------------------------
# Exception hierarchy — LLD §6 needs these to stay distinguishable, not one
# generic "the backend call failed" exception a caller has to string-match.
# ---------------------------------------------------------------------------


class LLMBackendError(Exception):
    """Base class for every error this module raises. Callers (Investigator,
    Slice 3; Sentinel's narration seam, if it's ever wired up) can catch this
    alone to mean "something about the LLM call didn't work," or catch one of
    the specific subclasses below to react differently depending on which."""


class BackendNotAvailableError(LLMBackendError):
    """The backend's underlying tool/credential isn't usable at all — the
    `claude` CLI isn't on PATH, or ANTHROPIC_API_KEY isn't set/is rejected.
    LLD §6 failure mode 2's case: "fail fast... pointing at falling back to
    LLM_BACKEND=anthropic if an API key is configured" — this is the
    exception that carries that actionable message. A caller seeing this
    should consider switching LLM_BACKEND, not retrying the same backend."""


class BudgetExhaustedError(LLMBackendError):
    """The backend's own budget/quota cap was hit mid-call. LLD §6 failure
    mode 3's specific, distinguishable case — the fix (wait, or switch
    LLM_BACKEND) is completely different from "the model couldn't find a
    confident answer," so this must never be conflated with BackendCallError
    below."""


class BackendTimeoutError(LLMBackendError):
    """Wall-clock timeout backstop (LLD §5) — the call hung or took too
    long, independent of whether it was actually spending budget. Distinct
    from BudgetExhaustedError: a hang that isn't spending money is a
    different failure than one that ran out of an explicit cap."""


class BackendCallError(LLMBackendError):
    """Catch-all: the call failed for some other real reason — non-zero
    exit / non-JSON stdout from `claude -p`, an Anthropic API error that
    isn't an auth or timeout problem, etc. A genuine failure that isn't one
    of the three specific, actionable cases above."""


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One tool-use request from a completion — Anthropic's Messages API
    shape (id/name/input), used as-is rather than inventing a parallel
    vocabulary, since AnthropicBackend is the one backend that produces
    these for real this sprint."""

    id: str
    name: str
    input: dict


@dataclass
class CompletionResult:
    """complete()'s return value — one request/response turn."""

    text: str  # concatenated text content, convenience accessor over raw content blocks
    stop_reason: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # backend-reported, best-effort (token counts, cost where known)
    raw: Any = None  # the raw backend-specific response object/dict, for debugging/audit


@dataclass
class InvestigationResult:
    """investigate()'s return value — the outcome of one delegated,
    multi-turn investigation (Design B, ClaudeCodeBackend only, this sprint).

    result_text is the model's raw final-answer text, NOT a parsed
    InvestigatorFinding — Investigator (Slice 3) is what parses its own
    fenced JSON block out of this string (decision 0004's named "known
    accepted risk": Design B has no structured-output guarantee the way
    Design A's submit_finding tool call does). This module stays agnostic
    to what that inner content even means.
    """

    result_text: str
    is_error: bool = False
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    turns: Optional[int] = None  # best-effort; LLD §2.3 notes this is meaningfully different from Design A's tracked turn count
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The interface.
# ---------------------------------------------------------------------------


class LLMBackend(ABC):
    """Interface every backend implements — LLD §3.

    An ABC (abc.ABC + @abstractmethod), not typing.Protocol: an ABC actively
    refuses to instantiate a subclass that forgot to implement complete() —
    a TypeError at construction time. Protocol's structural typing is a
    purely static (mypy-only) check, and this project doesn't run a type
    checker in CI — the runtime enforcement is the one that actually
    protects a future contributor who adds a fourth backend and forgets a
    method, rather than a silent duck-typing gap discovered only when
    something calls the missing method at runtime anyway.
    """

    name: str
    supports_delegated_investigation: bool = False

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: Optional[float] = None,
    ) -> CompletionResult:
        """One request/response turn. Every backend implements this for
        real. Used by Design A's loop (Investigator, Slice 3, against
        AnthropicBackend/OllamaBackend) and by Sentinel's optional narration
        call (src/agents/sentinel.py's `narrate_fn` seam, Slice 1 — still
        unwired; nothing in this slice changes that).

        `messages`: Anthropic Messages API shape — a list of
        `{"role": "user"|"assistant", "content": ...}` dicts, where content
        is either a plain string or a list of content blocks (text,
        tool_use, tool_result). This is deliberately NOT a bare string
        prompt: Investigator's Design-A loop needs real multi-turn,
        tool-call-capable conversations (send a tool_result, get the next
        turn), and AnthropicBackend passes this shape straight through to
        the `anthropic` SDK essentially unmodified. ClaudeCodeBackend's
        complete() accepts the same shape for interface consistency but
        only supports the simple cases it actually needs (see its
        docstring) — the two backends share a type, not identical
        capability, which is the honest state of things given decision 0004
        assigns real tool-loop work to investigate() for that backend
        instead.

        `timeout_s`: not in the LLD's original interface sketch, added here
        deliberately — LLD §5 asks for a wall-clock backstop on "both
        designs," and a `complete()` with no timeout at all could hang
        forever with zero backstop. `None` means "use this backend's own
        default" (each implementation documents its own).
        """
        raise NotImplementedError

    def investigate(
        self,
        task_prompt: str,
        mcp_config_path: Any,
        allowed_tools: Any,
        max_budget_usd: float,
        timeout_s: float = DEFAULT_INVESTIGATE_TIMEOUT_S,
    ) -> InvestigationResult:
        """Delegate an entire multi-turn investigation to the backend's own
        agent harness. Default body here (base class): NOT SUPPORTED — only
        ClaudeCodeBackend overrides this (decision 0004: it's the only
        backend that already IS an agent harness to delegate to).

        Decision 0005: callers (Investigator, Slice 3) check
        `supports_delegated_investigation` BEFORE ever calling this, so in
        the designed path this default body should never actually execute.
        It exists so that if something calls it anyway — a bug, not the
        intended dispatch — the failure is immediate and explicit (a clear
        NotImplementedError with the reason), not an AttributeError from a
        method that silently doesn't exist.
        """
        raise NotImplementedError(
            f"{self.name} does not support delegated investigation "
            f"(supports_delegated_investigation=False, decision 0005) — callers must "
            f"check that flag and dispatch to their own agent loop (Design A) instead "
            f"of calling investigate() directly."
        )


# ---------------------------------------------------------------------------
# ClaudeCodeBackend — Design B (decision 0004). complete() is a thin,
# tool-free wrapper; investigate() is the real delegated-investigation
# mechanism, built generically (no DataHub-specific content anywhere below).
# ---------------------------------------------------------------------------


def _run_claude(cmd: list[str], timeout_s: float) -> str:
    """Run a `claude` subprocess, returning raw stdout. Centralizes the two
    failure modes that need to raise OUR specific exception types instead of
    a raw stdlib one leaking out: a wall-clock timeout (LLD §5's blunt
    backstop — Design B has no --max-turns to rely on instead, confirmed
    absent from this CLI version in LLD §0) and the CLI disappearing between
    construction and call (defensive — ClaudeCodeBackend.__init__ already
    checked shutil.which() once, but re-raising the SAME specific exception
    type here closes that TOCTOU gap rather than leaking a bare
    FileNotFoundError past this module's exception contract).
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise BackendTimeoutError(f"claude -p did not complete within {timeout_s}s") from e
    except FileNotFoundError as e:
        raise BackendNotAvailableError(
            "the `claude` CLI was not found when attempting to run it (it may have been "
            "removed from PATH after this backend was constructed)"
        ) from e
    return result.stdout


def _parse_claude_json(stdout: str) -> dict:
    """`--output-format json` should always produce parseable JSON on
    stdout — but "should always" isn't a guarantee worth trusting blindly
    for a subprocess call. A malformed/empty/non-JSON stdout becomes a
    clear BackendCallError with a preview of what was actually returned,
    not an uncaught json.JSONDecodeError leaking out of this module to
    whatever called it.
    """
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise BackendCallError(
            f"claude -p returned non-JSON stdout — cannot parse response. "
            f"First 200 chars: {stdout[:200]!r}"
        ) from e


def _raise_for_errors(data: dict) -> None:
    """Inspect a parsed `claude -p` JSON response for the specific failure
    shape LLD §6 needs distinguished from everything else, then a generic
    catch-all for any other reported error.

    The budget-exhausted check is the EXACT shape confirmed live in
    lld-sprint2.md §0's own smoke test: `is_error=True` AND
    `terminal_reason="budget_exhausted"` AND `subtype="error_max_budget_usd"`
    — all three together, not a bare `is_error is True`. A different real
    failure (an unreachable MCP server, a malformed --mcp-config, a rejected
    --allowedTools entry) also sets `is_error=True` but with a DIFFERENT
    subtype/terminal_reason, and misreporting that as "out of budget" would
    send whoever's debugging it looking in exactly the wrong place — LLD §6
    failure mode 3's whole point.
    """
    if (
        data.get("is_error") is True
        and data.get("terminal_reason") == "budget_exhausted"
        and data.get("subtype") == "error_max_budget_usd"
    ):
        raise BudgetExhaustedError(
            f"claude -p exhausted its --max-budget-usd cap (cost so far: {data.get('total_cost_usd')})"
        )
    if data.get("is_error"):
        raise BackendCallError(
            f"claude -p reported an error (subtype={data.get('subtype')!r}, "
            f"terminal_reason={data.get('terminal_reason')!r}): {data.get('result') or data}"
        )


def _messages_to_prompt(messages: list[dict]) -> str:
    """Render a `messages` list into the single flat prompt string
    `claude -p` actually takes (it has no structured-conversation input the
    way the Anthropic Messages API does).

    Fine for this backend's documented, narrow complete() use case (a
    single short user message — Sentinel's still-unwired narration seam):
    the single-plain-string-message case is handled directly. A short
    multi-turn history is still renderable (role-prefixed, joined) so this
    doesn't silently mangle input if more than one message is ever passed —
    but any message with structured (non-string) content, e.g. a
    tool_result block, raises clearly: that shape means the caller actually
    needs real tool-loop capability, which is investigate()'s job for this
    backend, not complete()'s (see ClaudeCodeBackend.complete()'s docstring).
    """
    if not messages:
        raise ValueError("messages must be non-empty")

    if len(messages) == 1 and messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str):
        return messages[0]["content"]

    parts = []
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, str):
            raise NotImplementedError(
                "ClaudeCodeBackend.complete() only supports plain string message content "
                "(no tool_use/tool_result content blocks). Structured, tool-using "
                "conversations need AnthropicBackend/OllamaBackend (Design A), or "
                "investigate() for delegated work on this backend."
            )
        parts.append(f"{m.get('role', 'user')}: {content}")
    return "\n\n".join(parts)


class ClaudeCodeBackend(LLMBackend):
    """Design B, per decision 0004: `claude -p` IS a complete agent harness
    (its own tool loop + MCP client), not a bare completion endpoint — so
    complete() stays a thin, tool-free, single-turn wrapper, and
    investigate() delegates an ENTIRE multi-turn investigation to one
    subprocess call instead of a caller re-implementing a loop around it
    (decision 0004's cost math: one subprocess spawn pays the measured
    ~$0.11 fixed context-loading overhead once, not once per turn).
    """

    name = "claude_code"
    supports_delegated_investigation = True

    def __init__(self):
        # Fail fast at construction — LLD §6 failure mode 2 — before any
        # real pipeline work has happened, not discovered mid-run.
        if shutil.which("claude") is None:
            raise BackendNotAvailableError(
                "the `claude` CLI was not found on PATH. Install Claude Code, or "
                "switch backends: set LLM_BACKEND=anthropic in .env (requires "
                "ANTHROPIC_API_KEY)."
            )

    def complete(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: Optional[float] = None,
    ) -> CompletionResult:
        # Deliberate design choice, made explicitly here (the LLD names this
        # as an open call for the implementer): REJECT tools outright rather
        # than silently ignoring them or half-supporting them. `claude -p`
        # invoked with no --mcp-config genuinely has no tools it can call —
        # a caller passing tools=[...] here is asking for something this
        # backend cannot do inside a single completion at all. Real
        # tool-using work for this backend is investigate()'s job (Design
        # B), not complete()'s. Failing loudly here beats silently returning
        # a CompletionResult whose tool_calls a caller's loop expected but
        # will never get.
        if tools:
            raise NotImplementedError(
                "ClaudeCodeBackend.complete() does not support tools — it's a thin, "
                "tool-free, single-turn wrapper for narration only (LLD §3/§1.4). "
                "Multi-turn, tool-using work for this backend goes through investigate() "
                "instead (Design B, decision 0004)."
            )

        prompt = _messages_to_prompt(messages)
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        stdout = _run_claude(cmd, timeout_s if timeout_s is not None else DEFAULT_COMPLETE_TIMEOUT_S)
        data = _parse_claude_json(stdout)
        _raise_for_errors(data)

        usage = data.get("usage") or {}
        return CompletionResult(
            text=data.get("result", ""),
            stop_reason="end_turn",
            tool_calls=[],
            usage={
                "cost_usd": data.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
            raw=data,
        )

    def investigate(
        self,
        task_prompt: str,
        mcp_config_path: Any,
        allowed_tools: Any,
        max_budget_usd: float,
        timeout_s: float = DEFAULT_INVESTIGATE_TIMEOUT_S,
    ) -> InvestigationResult:
        """Design B's real mechanism (LLD §2.6): one `claude -p` subprocess
        call. Built GENERICALLY — this method has no idea it will eventually
        be called with DataHub's MCP config and `mcp__datahub__*` tool
        names; that knowledge belongs to Investigator (Slice 3, not built
        yet), which supplies `mcp_config_path`/`allowed_tools` as plain
        arguments. This slice's own tests exercise this method with
        fake/generic values for exactly that reason.

        `allowed_tools`: a list of tool-name strings (joined with commas
        here, matching §2.6's shown invocation shape) or an already-comma-
        joined string — accepting both avoids forcing every caller to
        remember which shape this expects.

        Field-name caveat, stated honestly rather than assumed: `result`
        (final text) and `num_turns` are read from the parsed JSON based on
        Claude Code's documented `--output-format json` shape and this
        session's own confirmed fields (`is_error`, `terminal_reason`,
        `subtype`, `total_cost_usd`, `duration_ms` — LLD §0). `result` and
        `num_turns` specifically were NOT independently re-verified against
        a live call this session (that would spend real, metered quota per
        decision 0004's own note about shared subscription cost) — flagged
        here the same way LLD §9/§10.9 already flag the `--allowedTools`
        string format as an open, cheap-to-resolve verification item for
        whoever wires this into a real investigation (Slice 3), not silently
        assumed correct.
        """
        allowed_tools_arg = allowed_tools if isinstance(allowed_tools, str) else ",".join(allowed_tools)

        cmd = [
            "claude",
            "-p",
            task_prompt,
            "--mcp-config",
            str(mcp_config_path),
            "--strict-mcp-config",
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            allowed_tools_arg,
            "--max-budget-usd",
            str(max_budget_usd),
        ]
        stdout = _run_claude(cmd, timeout_s)
        data = _parse_claude_json(stdout)
        _raise_for_errors(data)

        return InvestigationResult(
            result_text=data.get("result", ""),
            # By the time we reach this line, _raise_for_errors() has already
            # raised on any is_error=True response above — this will
            # therefore normally read False here. Kept as a real field
            # (rather than hardcoded False) as defense-in-depth: if a future
            # `claude -p` version reports an error via a shape
            # _raise_for_errors() doesn't yet recognize, a caller inspecting
            # `.is_error`/`.raw` directly still sees the truth, instead of
            # this method having silently baked in an assumption that's
            # since gone stale.
            is_error=bool(data.get("is_error", False)),
            cost_usd=data.get("total_cost_usd"),
            duration_ms=data.get("duration_ms"),
            turns=data.get("num_turns"),
            raw=data,
        )


# ---------------------------------------------------------------------------
# AnthropicBackend — Design A (decision 0004). Direct SDK calls;
# investigate() is intentionally not implemented (inherits the base class).
# ---------------------------------------------------------------------------

# A real, current-as-of-this-implementation Sonnet model id, matching
# CLAUDE.md's "default Sonnet" rule. Named explicitly and overridable via
# .env's ANTHROPIC_MODEL rather than buried — same "named tunable, not
# deeply tuned" convention src/datahub/generate_denials.py's own constants
# use. If Anthropic ships a newer Sonnet model by the time this runs,
# override via .env rather than editing this file.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"


class AnthropicBackend(LLMBackend):
    """Design A, per decision 0004: a bare chat-completion API, no agent
    harness of its own. complete() is a real, working direct call via the
    `anthropic` SDK (already a project dependency). investigate() is
    intentionally NOT overridden here — it inherits LLMBackend's default,
    which raises NotImplementedError (decision 0005: there's no `claude -p`-
    like harness for this backend to delegate to; Investigator's own loop
    calls complete() turn-by-turn instead).
    """

    name = "anthropic"
    supports_delegated_investigation = False

    def __init__(self, model: Optional[str] = None):
        # Same "fail clearly and immediately if a required credential is
        # missing" pattern src/datahub/add_lineage.py/add_metadata.py/
        # register_ml_model.py already established for DATAHUB_GMS_TOKEN —
        # not a new failure style invented for this one credential.
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendNotAvailableError(
                "ANTHROPIC_API_KEY is not set. Add it to .env (gitignored) — get a key "
                "from https://console.anthropic.com/settings/keys. Or switch backends: "
                "LLM_BACKEND=claude_code (requires the `claude` CLI on PATH)."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)

    def complete(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: Optional[float] = None,
    ) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "timeout": timeout_s if timeout_s is not None else DEFAULT_COMPLETE_TIMEOUT_S,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            # The key was present (checked in __init__) but the API itself
            # rejected it — still "this backend can't run right now,"
            # same category as a missing key, not a generic call failure.
            raise BackendNotAvailableError(f"Anthropic API rejected the configured ANTHROPIC_API_KEY: {e}") from e
        except anthropic.APITimeoutError as e:
            raise BackendTimeoutError(f"Anthropic API call did not complete within {kwargs['timeout']}s") from e
        except anthropic.AnthropicError as e:
            # Catch-all for everything else the SDK can raise (rate limits,
            # connection errors, bad requests, server-side errors, ...) —
            # real failures that aren't one of the two specific cases above.
            #
            # Deliberately NOT mapped to BudgetExhaustedError: that concept
            # is specific to `claude -p`'s own --max-budget-usd cap, a
            # per-subprocess hard stop the CLI enforces itself. A bare
            # Anthropic API call has no equivalent per-call budget cap —
            # LLD §7 assigns Design A's budget enforcement to the CALLER
            # (Investigator/Orchestrator), which sums usage across turns
            # against INVESTIGATOR_MAX_BUDGET_USD itself, not to this
            # method. Mapping e.g. RateLimitError to BudgetExhaustedError
            # here would conflate "Anthropic is rate-limiting this API key
            # right now" with "we deliberately spent our own configured
            # investigation budget" — two different problems with two
            # different fixes, which is exactly the kind of conflation LLD
            # §6 asks this exception hierarchy to avoid.
            raise BackendCallError(f"Anthropic API call failed: {e}") from e

        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        return CompletionResult(
            text="".join(text_parts),
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            raw=response,
        )


# ---------------------------------------------------------------------------
# OllamaBackend — interface-only stub.
# ---------------------------------------------------------------------------


class OllamaBackend(LLMBackend):
    """Interface-only stub, per LLD §3 — `ollama` isn't installed on this
    machine (confirmed, LLD §0) and no working local-model integration is in
    scope this sprint (an already-approved scope cut, not a gap introduced
    here). Present and typed so the 3-backend contract
    (LLM_BACKEND=claude_code|anthropic|ollama) is real and get_backend() can
    route to it — but complete() raises immediately, with no socket/HTTP
    call of any kind attempted first (tests/test_llm_backend.py asserts this
    directly, not just by code inspection).
    """

    name = "ollama"
    supports_delegated_investigation = False

    def __init__(self, host: Optional[str] = None):
        # Stored but never used to open a connection this sprint — present
        # so a future real implementation has an obvious place to read from,
        # without this stub pretending to validate reachability it never
        # actually checks.
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def complete(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: Optional[float] = None,
    ) -> CompletionResult:
        raise NotImplementedError(
            "OllamaBackend.complete() is not implemented this sprint — ollama isn't "
            "installed on this machine and no local-model integration is in scope "
            "(lld-sprint2.md §3, an already-accepted scope cut). Switch backends: "
            "LLM_BACKEND=claude_code or LLM_BACKEND=anthropic for a working backend."
        )

    # investigate() is intentionally NOT overridden — inherits LLMBackend's
    # default, which raises NotImplementedError referencing
    # supports_delegated_investigation (decision 0005). Same reasoning as
    # AnthropicBackend: there's no agent harness here to delegate to,
    # independent of the fact that complete() is also unimplemented — these
    # are two separately-named gaps, not one collapsed into the other.


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------

_BACKEND_CLASSES: dict[str, type] = {
    "claude_code": ClaudeCodeBackend,
    "anthropic": AnthropicBackend,
    "ollama": OllamaBackend,
}


def get_backend(name: Optional[str] = None, **kwargs) -> LLMBackend:
    """Factory, per LLD §3. Reads `LLM_BACKEND` from `.env` if `name` is
    `None` (falling back to `"claude_code"`, §3's stated default, if that's
    not set either). Raises a clear, actionable `ValueError` for an
    unrecognized value — NEVER silently falls back to a working default
    that wasn't what was actually configured. A silent fallback here would
    be exactly the kind of quiet, hard-to-debug surprise decision 0005
    rejected implicit exception-based control flow for, applied to config
    instead of dispatch.

    `**kwargs` are passed straight through to the chosen backend's
    constructor (e.g. `model=` for `AnthropicBackend`) — most callers won't
    need this, but it's here rather than re-exposing every constructor
    parameter as a `get_backend()` parameter too.
    """
    if name is None:
        name = os.environ.get("LLM_BACKEND", "claude_code")

    try:
        backend_cls = _BACKEND_CLASSES[name]
    except KeyError:
        raise ValueError(
            f"Unrecognized LLM_BACKEND value: {name!r}. Valid options: {sorted(_BACKEND_CLASSES)}."
        ) from None

    return backend_cls(**kwargs)
