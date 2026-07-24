# 0005 — `LLMBackend` interface shape: explicit capability flag, not caught exceptions

Date: 2026-07-23
Status: Accepted

## Context

Decision 0004 settled that `ClaudeCodeBackend` runs investigations via
delegation (Design B: one `claude -p --mcp-config` call) while
`AnthropicBackend`/`OllamaBackend` run them via our own turn-by-turn loop
(Design A). That means `LLMBackend` isn't one uniform interface where every
implementation does the same thing — it exposes two genuinely different
operations, and only some backends implement one of them for real:

- `complete(messages, tools) -> CompletionResult` — a single request/response
  turn. Every backend implements this (it's also what Sentinel's optional
  narration step uses, tool-free).
- `investigate(task_prompt, ...) -> InvestigationResult` — delegate an entire
  multi-turn investigation to the backend's own agent harness. Only
  `ClaudeCodeBackend` can do this for real.

Something has to decide, at the call site in `Investigator`, which of the two
strategies to run. That "something" is a real interface design choice, not a
detail that falls out automatically from 0004.

## Decision

`LLMBackend` exposes an explicit, static boolean:
`supports_delegated_investigation`. `Investigator`'s own dispatch code checks
it directly:

```
if backend.supports_delegated_investigation:
    result = backend.investigate(...)      # Design B
else:
    result = run_agent_loop(backend, ...)  # Design A, Investigator's own loop
```

`AnthropicBackend.investigate()` and `OllamaBackend.investigate()` are not
implemented at all — there's nothing to call, because `Investigator` never
calls them; it checks the flag first and runs its own loop instead.

## Alternatives considered

- **Always call `investigate()`, catch `NotImplementedError` from the
  backends that don't support it, fall back to the loop in the `except`
  block.** Rejected. This is a static, known-in-advance property of which
  backend is configured — not a runtime surprise an exception should be
  modeling. Using exceptions for expected, routine branching hides the
  actual decision inside a `try`/`except` instead of stating it plainly at
  the call site. For a codebase meant to be readable by someone learning
  software development, `if backend.supports_delegated_investigation:` says
  exactly what's happening and why; catching an exception to find out
  requires reading `AnthropicBackend`'s source first. Same behavior, worse
  self-documentation.
- **One uniform `run(...)` method on `LLMBackend` that internally picks
  Design A or B per backend, hiding the branch entirely inside
  `llm_backend.py`.** Rejected. Investigator's tool set — the DataHub MCP
  relay, the read-only `healthcare.db` query tool, the `submit_finding`
  schema — is Investigator's own domain knowledge. `llm_backend.py` is meant
  to be a generic, reusable backend abstraction with zero DataHub/
  healthcare.db awareness, so that a future agent (Remediator, a later
  sprint) can reuse it for a completely different tool set without
  `llm_backend.py` accumulating Investigator-specific knowledge. If
  `llm_backend.py` owned the A-vs-B branch, it would need to know
  Investigator's prompts and tool schemas to actually run either path,
  collapsing that boundary.

## Consequences

- `Investigator` (not `llm_backend.py`) owns its own agent-loop
  implementation (message history, tool dispatch, turn counting) — this is
  real code Investigator needs regardless of which backend is active, since
  it's the thing that runs for two of the three backends.
- Adding a backend later just means implementing `complete()` (required) and
  optionally `investigate()` + setting the flag `True` (only if that backend
  ships its own agent harness the way `claude -p` does) — no changes needed
  to `Investigator`'s dispatch logic either way.
- The flag is discoverable by reading `llm_backend.py` alone — no need to
  trace through `Investigator` to learn which backends support what.
