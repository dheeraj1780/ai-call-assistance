"""One controlled real Gemini check (manual; NOT part of the automated tests).

Makes exactly ONE generateContent request (the WhatsApp reply-suggestion prompt and schema, with a
sample customer message and a sample knowledge excerpt) and ONE embedding request, using
GEMINI_API_KEY from backend/.env. Prints model, latency, token counts and the draft - never the
key. Sends nothing to WhatsApp and writes nothing to the database.

    cd backend && uv run python scripts/gemini_smoke.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.embeddings import EMBEDDING_DIM, GeminiEmbeddingProvider
from app.ai.gemini_provider import GeminiProvider
from app.ai.prompts import MESSAGE_ASSIST
from app.ai.provider import AIError, AITask
from app.common.config import get_settings
from app.conversations.assist import MessageAssist, MessageAssistContext, _prompt


async def main() -> int:
    s = get_settings()
    if s.gemini_api_key is None:
        print("GEMINI_API_KEY is not set in backend/.env")
        return 2
    key = s.gemini_api_key.get_secret_value()
    ctx = MessageAssistContext(
        channel="WHATSAPP",
        customer_name="Ravi Kumar",
        customer_organization="ABC Industries",
        company_name="Acme Traders",
        company_products="Inventory software for MSMEs",
        history=[{"from": "customer", "text": "What does the Basic plan cost for 5 branches?"}],
        latest="What does the Basic plan cost for 5 branches?",
        knowledge=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "title": "Price list (sample)",
                "content": "The Basic plan costs Rs 2,500 per month for up to 5 branches.",
                "score": "0.800",
            }
        ],
    )
    llm = GeminiProvider(
        key,
        dict.fromkeys(AITask, s.gemini_model),
        thinking_level=s.gemini_thinking_level or None,
    )
    start = time.perf_counter()
    try:
        result = await llm.generate(
            task=AITask.MESSAGE_ASSIST,
            system=MESSAGE_ASSIST.system,
            prompt=_prompt(ctx, None),
            schema=MessageAssist,
            context=ctx,
            max_tokens=1200,
            timeout_seconds=s.ai_timeout_seconds,
        )
    except AIError as exc:
        print(f"GENERATE FAILED: {exc.code}: {exc}")
        return 1
    ms = int((time.perf_counter() - start) * 1000)
    out = result.output
    print(
        f"generate OK  model={result.usage.model}  {ms} ms  "
        f"prompt={result.usage.input_tokens} output(+thinking)={result.usage.output_tokens} "
        f"tokens  prompt_version={MESSAGE_ASSIST.version}"
    )
    print(f"  draft: {out.reply}")
    print(f"  knowledge_used={out.knowledge_used} cited={out.knowledge_chunk_ids}")

    emb = GeminiEmbeddingProvider(key, s.gemini_embedding_model)
    start = time.perf_counter()
    try:
        [vec] = await emb.embed(["Basic plan price for 5 branches"], "query")
    except Exception as exc:  # report any failure of the one manual call
        print(f"EMBED FAILED: {exc}")
        return 1
    print(
        f"embed OK     model={s.gemini_embedding_model}  "
        f"{int((time.perf_counter() - start) * 1000)} ms  dim={len(vec)} (expected {EMBEDDING_DIM})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
