# 0004 — Investigator's agent-loop strategy: our own loop vs. delegating to `claude -p`

Date: 2026-07-23
Status: Accepted

## Context

Investigator needs to run a multi-turn, tool-using investigation: query DataHub
lineage, query `healthcare.db`, reason about what it finds, and land on a
root cause. Two structurally different ways to build that were on the table:

- **(A) Our own agent loop.** We call the selected `LLMBackend` turn-by-turn,
  with tool schemas we define (a DataHub-MCP relay tool, a read-only
  `healthcare.db` query tool, and a `submit_finding` tool for the terminal
  answer). Our Python code owns the loop: send messages, inspect the
  response, dispatch any tool calls, append results, repeat.
- **(B) Delegate the whole investigation to one `claude -p` call.** Attach the
  DataHub MCP server via `--mcp-config`, give it a scoped `--allowedTools`
  list (the MCP tools plus a read-only `sqlite3` command), hand it a
  structured task prompt, and parse the JSON result Claude Code's own
  internal agent loop produces.

Both are real options because `LLM_BACKEND` is pluggable across three
different kinds of things: `claude_code` (a full CLI agent harness with its
own tool loop and MCP client), `anthropic` (a bare chat-completion API, no
loop of its own), and `ollama` (same shape as Anthropic, once wired up). That
asymmetry — one backend already *is* an agent, two are not — turns out to be
the deciding factor, not a side note.

## Verification done before deciding

Two things needed to be resolved with evidence, not assumption, before this
decision could be made honestly:

**1. Does `--mcp-config`'s JSON support `${VAR}`-style expansion against the
process environment, the same way `.mcp.json` does?** This matters because
Design B needs the DataHub token to reach a subprocess's environment without
writing secrets into a config file. Verified directly: wrote a minimal MCP
config file with `"MY_TEST_VAR": "${MY_TEST_VAR}"` in a fake stdio server's
`env` block, pointed a fake "server" (a Python script that just dumps its own
`os.environ` to a file and exits) at it, set `MY_TEST_VAR=hello_world_12345`
in the calling shell, and ran
`claude -p "..." --mcp-config <file> --strict-mcp-config --output-format json`.
The dumped file showed the real value, not the literal string
`${MY_TEST_VAR}`. **Confirmed: yes, it expands, exactly like `.mcp.json`.**
This means Design B's MCP config can be a small, static, *committed* file
(same shape as the repo's own `.mcp.json`) — no temp-file-with-substituted-
secrets workaround needed at invocation time.

**2. What does a `claude -p` call actually cost, mechanically, before any
real investigation work happens?** The same smoke test above — a prompt that
did zero tool calls — returned `"total_cost_usd": 0.1130436`, driven by
`"cache_creation_input_tokens": 17894` and `"cache_read_input_tokens": 15912`
against **zero** real output tokens (it hit a deliberately tiny
`--max-budget-usd 0.05` cap and stopped). That token volume is Claude Code's
own system prompt and MCP/tool configuration being loaded into context — a
**fixed cost paid once per subprocess spawn**, independent of how much actual
investigation happens inside it.

That second finding is what actually settled this decision.

## Decision

**Design B for `ClaudeCodeBackend` specifically. Design A for `AnthropicBackend`
and `OllamaBackend`.** Not a compromise between the two — the evidence points
at different answers for different backends, and building one uniform
strategy across all three would mean picking the wrong one for at least one
of them.

| Axis | Design A (our loop) | Design B (delegate to `claude -p`) |
|---|---|---|
| Debuggability | Every turn/tool-call/result is Python we wrote — inspectable, loggable, replayable. | Investigation runs inside a subprocess; only the final JSON (or a `stream-json` transcript) is visible from outside. |
| Portability across the 3 backends | Uniform: same loop, same tool schemas, just a different `complete()` underneath. Works for Anthropic and (eventually) Ollama. | Inherently `claude`-CLI-specific. Cannot run against a bare API key or a local model — not portable by construction. |
| Token/dollar cost | Pays only for real investigation tokens at API price, no fixed per-call system-prompt overhead. | Real, measured fixed floor (~$0.11 from context alone, see above) **paid once per investigation** — one subprocess spawn. |
| Failure isolation | A bad turn (malformed tool call, bad SQL) is caught mid-loop, with turns 1..N-1 already in hand; can retry just that turn or degrade with partial evidence. | A failure surfaces only as the whole call failing/timing out — no partial-turn recovery; everything before the failure is sunk cost. |

**Why not just run Design A against `ClaudeCodeBackend` too, for uniformity?**
This was the real fork, and the cost numbers above resolve it. If
`ClaudeCodeBackend.complete()` were called turn-by-turn inside Design A's
loop, that ~$0.11+ fixed overhead would be paid **once per turn** — a fresh
`claude -p` subprocess spawn per turn, with no context persisted between
calls unless we reconstruct and resend the full conversation every time
(itself more tokens, more cost). A realistic Investigator run is 6-12 turns
(§5 of the LLD estimates this from the actual query sequence). That's
$0.66-$1.32+ in pure fixed overhead before any real content-token cost, to
recreate — worse, and with weaker guarantees — a tool loop that `claude -p`
already runs internally, for free, once. Using Design A here would mean
paying repeatedly to ignore the one thing that makes `ClaudeCodeBackend`
worth having: it's not a bare completion endpoint, it's an already-built
agent with its own MCP client and tool loop, running on the repo owner's
existing Pro subscription at no marginal API cost per token.

**Why not run Design B for Anthropic/Ollama too, for uniformity the other
way?** There's no equivalent CLI-with-built-in-agent-harness for a bare
Anthropic API key or a local Ollama model to delegate to. Forcing them
through some invented CLI wrapper would mean not using the `anthropic` SDK
(already a project dependency, already the direct, cheap, well-documented way
to call that API) for no benefit, and inventing a wrapper for Ollama that
doesn't exist. Design A is not a fallback for these two backends — it's the
only shape that makes sense for a bare completion API.

## What this costs in code, and why it's still worth it

`Investigator`'s own dispatch logic has to branch on which strategy applies
(`if backend.supports_delegated_investigation: ... else: ...` — see decision
0005 for why that's an explicit flag rather than a caught exception). This is
one honest `if`/`else` in one place, not scattered special-casing throughout
the codebase. The alternative — a single code path that quietly does
different things depending on hidden backend behavior — would be *harder* to
reason about, not easier, despite looking more "uniform" from the outside.

## Alternatives considered and rejected

- **Design A uniformly for all three backends, including `claude_code`.**
  Rejected — see the cost math above. Multiplies fixed overhead by turn
  count and reimplements (worse) a tool loop `claude -p` already has.
- **Design B uniformly for all three backends.** Rejected — doesn't exist
  for Ollama, and drops the `anthropic` SDK's direct, cheaper, more
  debuggable path for Anthropic for no offsetting benefit.
- **Pick one design and drop true 3-backend pluggability.** Rejected — the
  spec calls for three pluggable backends selected via `.env`'s
  `LLM_BACKEND`. `OllamaBackend` being a thin, untested stub this sprint is
  an already-approved, separate scope cut (Ollama isn't even installed on
  this machine) — it isn't license to also collapse `AnthropicBackend`'s
  real, working Design-A path into a single-backend design.

## Known accepted risk in Design B, named rather than hidden

Design A can force a structured terminal answer via a `submit_finding` tool
call — Anthropic's tool-use API guarantees that call's arguments match the
declared JSON schema. Design B has no equivalent guarantee: `--output-format
json` structures the CLI's *own* metadata (cost, turns, timing), not the
model's answer content — the model's actual finding still has to be prose
that Investigator's task prompt asks it to format as a JSON blob, which our
code then parses as a second, inner JSON document. If that inner text isn't
valid JSON, that's a real (if rare) failure mode — handled as "inconclusive,
raw text preserved as evidence" (LLD §6), not a crash, but it's a genuine,
named trade-off Design B pays that Design A doesn't.

## Consequences

- `Investigator` needs both tool-loop code (for the Anthropic/Ollama path)
  and a `claude -p` invocation wrapper (for the ClaudeCodeBackend path) —
  more surface area than picking one design outright, but each path is
  simple on its own, and the branch point is explicit and small.
- The DataHub MCP config used by Design B (`investigator_mcp_config.json`) is
  safe to commit, same reasoning as the repo's own `.mcp.json` (decision
  0003) — verified this session, not assumed.
- If `OllamaBackend` becomes real in a future sprint, it slots into the
  existing Design-A path with no changes to `Investigator`'s dispatch logic
  or to `ClaudeCodeBackend`'s Design-B path — the split is by backend
  capability, not hardcoded per-backend-name logic.
