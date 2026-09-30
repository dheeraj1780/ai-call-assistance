"""One controlled real LLM check through the app's own provider abstraction (manual; NOT part of
the automated tests).

Uses AI_PROVIDER / AI_MODEL / AI_MODEL_REALTIME / GEMINI_MODEL and the matching API key from the
environment or backend/.env (never printed). Sends ONE small copilot request (the same system
prompt and output schema the live copilot uses) with a synthetic three-line transcript and prints
the configured provider, model, latency, token counts and the structured result. Writes nothing to
the database.

    cd backend && uv run python scripts/ai_smoke.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.gateway import build_provider
from app.ai.provider import AIError, AITask
from app.ai.safety import data_block
from app.common.config import get_settings
from app.copilot.engine import COPILOT_SYSTEM, CopilotContext, CopilotDelta


async def main() -> int:
    s = get_settings()
    if s.ai_provider == "mock":
        print("AI_PROVIDER=mock: set AI_PROVIDER=anthropic or gemini (and its API key)")
        return 2
    provider = build_provider(s)
    ctx = CopilotContext(
        objective="Qualify inventory automation need",
        agenda=[{"id": "a1", "title": "Budget", "status": "NOT_STARTED", "question": ""}],
        known_notes=[],
        new_segments=[
            {"speaker": "SALES_REP", "text": "How do you track stock today?"},
            {"speaker": "CUSTOMER", "text": "In Excel, across 5 branches. Budget is about 2 lakh."},
            {"speaker": "UNKNOWN", "text": "Does it work offline?"},
        ],
    )
    prompt = "\n\n".join(
        [
            data_block("call_objective", ctx.objective or ""),
            data_block("agenda", "[id=a1] (NOT_STARTED) Budget"),
            data_block("known_notes", "(none)"),
            data_block(
                "new_transcript",
                "\n".join(f"{x['speaker']}: {x['text']}" for x in ctx.new_segments),
            ),
        ]
    )
    started = time.monotonic()
    try:
        result = await provider.generate(
            task=AITask.COPILOT,
            system=COPILOT_SYSTEM,
            prompt=prompt,
            schema=CopilotDelta,
            context=ctx,
            max_tokens=1500,
            timeout_seconds=s.ai_timeout_seconds,
        )
    except AIError as exc:
        print(f"FAILED: {exc.code} ({type(exc).__name__})")
        return 1
    ms = round((time.monotonic() - started) * 1000)
    print(f"provider={result.usage.provider} model={result.usage.model} latency_ms={ms}")
    print(f"tokens in={result.usage.input_tokens} out={result.usage.output_tokens}")
    print(result.output.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
