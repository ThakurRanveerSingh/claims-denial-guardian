# Token / Usage Ledger

Usage notes appended after each session, sourced from `/status` output the
user pastes. Tracked per `CLAUDE.md`'s Project Manager bookkeeping rule.

| Date | Sprint | Session window | Weekly window | Promo | Equiv. API value | Model | Lines added | Tests | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-23 | Sprint 1 | 29% of 5h | 10% of weekly | +50% promo active through Aug 19 | $12.45 | claude-sonnet-5 | 1156 | 21 green | Includes ML-entity UAT fix detour (MLModel/MLFeatureTable registration) |
| 2026-07-26 | Sprint 2 close | cumulative (Sprint 2) | 3% of weekly (fresh week from Jul 24) | +50% promo active through Aug 19 | $65.93 | claude-sonnet-5 + claude-haiku-4.5 | +7,815 / −158 | 144 green, 2 live (excluded by default), 1 pre-existing fail (unrelated DataHub state) | Sentinel/Investigator/Orchestrator + `guardian` CLI, 5 slices + hands-on UAT punch list (2 real bugs found, both fixed). **92% of usage came from subagent-heavy sessions — Sprint 3 process change**: reserve subagents for distinct roles (design/test), do routine implementation in the main session. |
