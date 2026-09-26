"""GeminiEmbeddingProvider against a mocked HTTP transport (no key, no network)."""

import json
import math
from typing import Any

import httpx
import pytest

from app.ai.embeddings import EMBEDDING_DIM, EmbeddingError, GeminiEmbeddingProvider
from app.common.config import Settings

KEY = "AIza-EMBED-TEST-KEY"


def provider(handler: Any) -> tuple[GeminiEmbeddingProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    return GeminiEmbeddingProvider(KEY, transport=httpx.MockTransport(record)), seen


def vectors_for(request: httpx.Request, scale: float = 3.0) -> httpx.Response:
    n = len(json.loads(request.content)["requests"])
    return httpx.Response(200, json={"embeddings": [{"values": [scale] * EMBEDDING_DIM}] * n})


async def test_request_shape_and_normalisation() -> None:
    p, seen = provider(vectors_for)
    [vec] = await p.embed(["Do you deliver to Pune?"], "query")
    assert len(vec) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)
    [req] = seen
    assert str(req.url).endswith("/v1beta/models/gemini-embedding-001:batchEmbedContents")
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)
    item = json.loads(req.content)["requests"][0]
    assert item["taskType"] == "RETRIEVAL_QUERY"
    assert item["outputDimensionality"] == EMBEDDING_DIM
    assert item["model"] == "models/gemini-embedding-001"
    await p.embed(["chunk"], "document")
    assert json.loads(seen[-1].content)["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"


async def test_batches_above_the_request_limit() -> None:
    p, seen = provider(vectors_for)
    out = await p.embed([f"chunk {i}" for i in range(250)], "document")
    assert len(out) == 250
    assert [len(json.loads(r.content)["requests"]) for r in seen] == [100, 100, 50]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}),
        httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"embeddings": [{"values": [1.0] * 12}]}),  # wrong dimension
        httpx.Response(200, json={"embeddings": []}),  # wrong count
    ],
)
async def test_failures_raise_embedding_error(response: httpx.Response) -> None:
    p, _ = provider(lambda r: response)
    with pytest.raises(EmbeddingError):
        await p.embed(["text"], "document")


async def test_timeout_raises_embedding_error() -> None:
    def slow(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    with pytest.raises(EmbeddingError):
        await provider(slow)[0].embed(["text"], "document")


def test_gemini_embeddings_require_a_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        Settings(_env_file=None, embedding_provider="gemini")


def test_threshold_defaults_per_provider() -> None:
    base = {"_env_file": None}
    assert Settings(**base).knowledge_score_threshold == 0.10  # hashing (dev)
    gemini = Settings(**base, embedding_provider="gemini", gemini_api_key="k")
    assert gemini.knowledge_score_threshold == 0.60
    assert Settings(**base, knowledge_min_score=0.72).knowledge_score_threshold == 0.72
