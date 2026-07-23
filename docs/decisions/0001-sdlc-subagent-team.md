# 0001 — SDLC subagent team in .claude/agents

Date: 2026-07-22
Status: Accepted

## Context

Claims Denial Guardian is being built by one person learning software
development, using Claude Code as the main collaborator. We wanted a way to
split work into SDLC roles (requirements, design, implementation at two
skill levels, testing, PM bookkeeping) without paying Sonnet/Opus rates for
mechanical work, and without every role having access to tools it has no
business using (e.g. a requirements reviewer shouldn't be able to edit code).

## Decision

Defined seven Claude Code subagents in `.claude/agents/`, each with its own
system prompt, a restricted tool list matching its responsibilities, and a
model tier:

| Agent | Model | Reasoning |
|---|---|---|
| business-user | Haiku | Read-only stakeholder review; no code/doc authoring |
| business-analyst | Haiku | Requirements drafting is bookkeeping-shaped work |
| solution-architect | Sonnet (Opus on explicit override) | Design is the project's default-Sonnet work; Opus is reserved for HLD/LLD *review* and hard debugging per project policy, invoked via an explicit model override rather than baked into the file |
| senior-dev | Sonnet | Ambiguous/cross-component implementation needs full reasoning |
| mid-dev | Haiku | Implements already-decided specs; escalates instead of guessing |
| tester | Haiku | Test writing/running is mechanical, scoped work |
| project-manager | Haiku | Status tracking and ledger bookkeeping |

This mirrors the model-tiering rule already in `CLAUDE.md` ("default Sonnet;
Opus only for HLD/LLD review and hard debugging; Haiku for tests, docs,
scaffolding, bookkeeping") applied per-role instead of per-task.

Tool access was scoped per role rather than granting all agents the full
toolset: e.g. `business-user` gets `Read, Grep, Glob` only (no `Write`/`Edit`
— it reviews, it doesn't produce artifacts), while `senior-dev`/`mid-dev`
get the full implementation set (`Read, Edit, Write, Bash, Grep, Glob`).

## Alternatives considered

- **One generalist agent for all dev work.** Rejected — loses the
  cost/quality tiering CLAUDE.md already asks for, and blurs the boundary
  between "design decision" and "implementation detail," which matters for
  a learner trying to see those as distinct steps.
- **Giving every agent the full tool set for flexibility.** Rejected — an
  agent that can technically do more than its role implies is more likely
  to blur responsibilities (e.g. a tester silently patching application
  code instead of filing a bug). Restricting tools enforces the role
  boundary instead of just describing it in prose.
- **Encoding "opus-capable" as `model: opus` directly on solution-architect.**
  Rejected — Claude Code subagent frontmatter only supports one fixed model
  per file, and the project default is Sonnet for design work. Opus is a
  deliberate escalation for review/hard-debugging, not the default, so it's
  documented as an invoke-time override instead.

## Consequences

- Routine work (specs, scaffolding, tests, status updates) runs on Haiku,
  keeping cost down.
- Design and non-trivial implementation still get Sonnet-level reasoning.
- Opus is only spent when someone explicitly invokes solution-architect
  with a model override for review/hard-debugging — it's opt-in, not
  automatic.
- Tool restrictions mean a role that outgrows its current scope (e.g.
  business-analyst eventually needing to prototype something) will need
  this file revisited rather than silently working around the limit.
