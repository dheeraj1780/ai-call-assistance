# AI

## Providers (all behind interfaces; domain code never imports a vendor SDK)

| Interface | Implementations | Status |
|---|---|---|
| `AIProvider` (`app/ai/provider.py`) | `AnthropicProvider` (`messages.parse` with Pydantic `output_format`, model `claude-opus-5`), `MockAIProvider` | Anthropic IMPLEMENTED, **NOT LIVE VERIFIED** (no key during development); mock used by default and in tests |
| `EmbeddingProvider` (`app/ai/embeddings.py`) | `HashingEmbeddingProvider` (lexical, dev), `VoyageEmbeddingProvider` | Voyage NOT LIVE VERIFIED |
| `SpeechToTextProvider` (`app/speech/provider.py`) | `MockSpeechToTextProvider` | MOCKED only |

`AIGateway` (`app/ai/gateway.py`) is the only entry point: per-task timeouts, per-company daily
token budget (fail closed), usage + estimated cost records for every attempt, all failures turned
into `AIError` subclasses so features degrade instead of crashing.

The mock provider derives outputs **only from the typed context** it is given (records, notes,
transcript); it never invents facts. Everything it produces is labelled `provider = "mock"` in the
API and the UI.

## Use cases

| Task | Where | Inputs | Safety / grounding |
|---|---|---|---|
| Agenda suggestion | `app/agendas/service.py` | objective, company offering, customer records with reference ids | Facts must cite a provided `ref`; facts citing unknown refs are discarded server-side; suggestion is never auto-saved |
| Live copilot | `app/copilot/engine.py` | objective, agenda state, known notes, **only the new transcript lines** | Deterministic detectors first; LLM every N segments, one in flight, per-call cap; agenda ids from the model are ignored unless they exist; manual agenda status always wins |
| Knowledge answer | `app/knowledge/service.py` | question + retrieved tenant chunks | Must cite chunk ids; low retrieval score or unsupported → "Information not found in company knowledge." |
| Post-call analysis | `app/postcall/service.py` | transcript (capped), notes, agenda | CONFIRMED requires a verbatim quote found in the transcript, else INFERRED; action items created unconfirmed; drafts flagged if they contain figures not in the call |

## Prompt injection defences (`app/ai/safety.py`)

- All untrusted content (customer speech, documents, retrieved chunks, tenant text) is wrapped in
  `<data name="...">` blocks; embedded `<data>`/`</data>` tags are neutralised.
- The system prompt states data blocks are never instructions.
- The model has **no tools**: it can only return schema-validated JSON, which application code
  validates again (grounding checks above) before anything is stored or shown.
- AI never sends messages, creates calendar events or changes records; humans confirm.

## Cost controls

- No per-token LLM calls; the live copilot sends only new segments since the last pass.
- `COPILOT_MAX_LLM_CALLS_PER_CALL`, `COPILOT_LLM_EVERY_N_SEGMENTS`, `AI_DAILY_TOKEN_BUDGET`,
  per-company rate limit on user-triggered AI requests, request timeouts, bounded SDK retries.
- `ai_usage_records` stores tokens, latency and estimated cost per call/task (embeddings too).
- Post-call analysis runs once per call (idempotent).

## Not implemented / future

- Real-time STT vendor, diarization for mixed-audio providers.
- LLM synthesis of knowledge answers during the live call (live uses extractive excerpts).
- `companies.ai_instructions` is stored but not yet injected into prompts.
- Server-side refusal fallbacks for Claude are not enabled; refusals degrade like other AI errors.
- Hinglish/regional language models (interfaces carry a language setting: `STT_LANGUAGE`).
