# Token / Usage Ledger

Usage notes appended after each session, sourced from `/status` output the
user pastes. Tracked per `CLAUDE.md`'s Project Manager bookkeeping rule.

| Date | Sprint | Session window | Weekly window | Promo | Equiv. API value | Model | Lines added | Tests | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-23 | Sprint 1 | 29% of 5h | 10% of weekly | +50% promo active through Aug 19 | $12.45 | claude-sonnet-5 | 1156 | 21 green | Includes ML-entity UAT fix detour (MLModel/MLFeatureTable registration) |
| 2026-07-26 | Sprint 2 close | cumulative (Sprint 2) | 3% of weekly (fresh week from Jul 24) | +50% promo active through Aug 19 | $65.93 | claude-sonnet-5 + claude-haiku-4.5 | +7,815 / −158 | 144 green, 2 live (excluded by default), 1 pre-existing fail (unrelated DataHub state) | Sentinel/Investigator/Orchestrator + `guardian` CLI, 5 slices + hands-on UAT punch list (2 real bugs found, both fixed). **92% of usage came from subagent-heavy sessions — Sprint 3 process change**: reserve subagents for distinct roles (design/test), do routine implementation in the main session. |
| 2026-07-26 | Sprint 3 WP1 (Scribe) | *user to fill from /status* | *user to fill from /status* | *user to fill from /status* | *user to fill from /status* | claude-sonnet-5 | +1,637 / −11 | 182 green, 3 live (excluded by default), 0 fail | First session run entirely in the main session, no subagent delegation — the process change logged above, applied for the first time. Parts A-E: fixed a real drifted-state bug (Part A), designed and live-verified DataHub writeback (Part B), implemented `scribe.py` (Part C, 2 real bugs found via its own live test), the repo owner's own hands-on UI walkthrough found a 3rd real issue (Part D, ambiguous assertion descriptions), closed out per the new "commit at every Part/slice boundary, push at least daily" rule (added to `CLAUDE.md` this session, prompted by WP1 itself sitting uncommitted across several turns). |
