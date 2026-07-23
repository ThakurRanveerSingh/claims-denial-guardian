# Claims Denial Guardian — build rules
- The user is LEARNING software development. Explain every non-trivial
  decision inline: what you chose, why, and what you rejected.
- After each sprint, write docs/walkthroughs/sprint-N.md covering what was
  built, why, and trade-offs considered.
- Log architectural decisions in docs/decisions/ (one short file per decision).
- Project Manager agent: append usage notes to docs/token_ledger.md after
  every session (source: /status output the user pastes).
- Models: default Sonnet. Opus only for HLD/LLD review and hard debugging.
  Haiku for tests, docs, scaffolding, bookkeeping.
- Never commit secrets. API keys live in .env (gitignored).
- All DataHub metadata reads MUST go through the DataHub MCP server or SDK —
  never hardcode schemas.