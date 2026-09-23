# AI

**Status: NOT IMPLEMENTED.** Nothing in Phase 1 calls an LLM. This document records the
approved design constraints (ADR-003, ADR-009) so later phases follow them.

## Providers

- `AIProvider` (initial implementation: Claude / Anthropic), `EmbeddingProvider`,
  `SpeechToTextProvider` — interfaces in `app/ai/` and `app/integrations/` (future).
- Domain code receives our own Pydantic types, never vendor SDK objects.
- Interfaces take a language parameter; MVP language is English / Indian English. Hinglish
  and regional languages are future work and must not require interface changes.
- API keys only via environment variables (`ANTHROPIC_API_KEY`, …).

## Design rules

- Structured, schema-validated output (Pydantic). Malformed output → one repair attempt →
  fall back to "no insight"; never a 500 and never a dropped call.
- Incremental conversation state; never resend the full transcript on every turn.
- Deterministic detectors for low-latency signals; LLM for richer extraction.
- Confidence thresholds, de-duplication keys, per-type cooldowns, a cap on visible cards.
- Knowledge answers must be grounded in retrieved, tenant-scoped chunks; otherwise
  "Information not found in company knowledge."
- Uploaded documents, transcripts and `companies.ai_instructions` are untrusted input:
  delimited as data in prompts; the live copilot model gets no action-taking tools.
- Cost controls: per-call token budgets, request timeouts, bounded retries, rate limits,
  usage records per call.

## Data processing (to complete when providers are chosen)

For each AI/STT provider document: data region, retention, training use, zero-retention
options, sub-processors — **REQUIRES PROVIDER CONFIRMATION** / **REQUIRES LEGAL REVIEW**.
