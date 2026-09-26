"""Embedding providers (provider-neutral).

- ``HashingEmbeddingProvider`` (default, MOCKED/dev): deterministic feature hashing of word
  unigrams and bigrams into a fixed-size, L2-normalised vector. It captures LEXICAL overlap
  only - good enough for offline development and tests, not a semantic model.
- ``VoyageEmbeddingProvider``: Voyage AI embeddings over HTTPS. IMPLEMENTED, NOT LIVE VERIFIED
  (no API key during development).
- ``GeminiEmbeddingProvider``: Gemini API ``batchEmbedContents`` (default ``gemini-embedding-001``,
  task types RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY, 1024 dimensions, L2-normalised because
  Google requires normalisation below 3072 dimensions). Key in the ``x-goog-api-key`` header only.
  IMPLEMENTED, tested with a mocked transport; NOT LIVE VERIFIED until a real request succeeds.

All vectors have ``EMBEDDING_DIM`` dimensions to match the ``knowledge_chunks.embedding``
column. Changing provider/dimension requires re-embedding (documents store the model name).
"""

import hashlib
import itertools
import math
import re
from typing import Literal, Protocol

import httpx

EMBEDDING_DIM = 1024
InputType = Literal["document", "query"]

_WORD = re.compile(r"[a-z0-9]+")


class EmbeddingError(Exception):
    pass


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    name = "hashing"
    model = "hashing-v1-1024"

    def _vector(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
        vec = [0.0] * EMBEDDING_DIM
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % EMBEDDING_DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class VoyageEmbeddingProvider:
    name = "voyage"

    def __init__(self, api_key: str, model: str = "voyage-3.5", timeout: float = 20.0) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "input": texts,
                        "model": self.model,
                        "input_type": input_type,
                        "output_dimension": EMBEDDING_DIM,
                    },
                )
                resp.raise_for_status()
                data = resp.json()["data"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise EmbeddingError(type(exc).__name__) from exc
        vectors = [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]
        if any(len(v) != EMBEDDING_DIM for v in vectors):
            raise EmbeddingError("unexpected embedding dimension")
        return vectors


class GeminiEmbeddingProvider:
    name = "gemini"
    batch_limit = 100  # Gemini batchEmbedContents accepts up to 100 requests per call

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        timeout: float = 20.0,
        *,
        base_url: str = "https://generativelanguage.googleapis.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._base = base_url.rstrip("/")
        self._transport = transport

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        if not texts:
            return []
        task = "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for i in range(0, len(texts), self.batch_limit):
                batch = texts[i : i + self.batch_limit]
                body = {
                    "requests": [
                        {
                            "model": f"models/{self.model}",
                            "content": {"parts": [{"text": t}]},
                            "taskType": task,
                            "outputDimensionality": EMBEDDING_DIM,
                        }
                        for t in batch
                    ]
                }
                try:
                    resp = await client.post(
                        f"{self._base}/v1beta/models/{self.model}:batchEmbedContents",
                        headers={"x-goog-api-key": self._api_key},
                        json=body,
                    )
                    resp.raise_for_status()
                    items = resp.json()["embeddings"]
                except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                    status = (
                        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    )
                    raise EmbeddingError(
                        f"{type(exc).__name__}{f' {status}' if status else ''}"
                    ) from exc
                if len(items) != len(batch):
                    raise EmbeddingError("unexpected number of embeddings")
                vectors.extend(_normalise(list(map(float, item["values"]))) for item in items)
        if any(len(v) != EMBEDDING_DIM for v in vectors):
            raise EmbeddingError("unexpected embedding dimension")
        return vectors


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        from app.common.config import get_settings

        settings = get_settings()
        if settings.embedding_provider == "gemini":
            if settings.gemini_api_key is None:
                raise RuntimeError("EMBEDDING_PROVIDER=gemini requires GEMINI_API_KEY")
            _provider = GeminiEmbeddingProvider(
                settings.gemini_api_key.get_secret_value(), settings.gemini_embedding_model
            )
        elif settings.embedding_provider == "voyage":
            if settings.voyage_api_key is None:
                raise RuntimeError("EMBEDDING_PROVIDER=voyage requires VOYAGE_API_KEY")
            _provider = VoyageEmbeddingProvider(settings.voyage_api_key.get_secret_value())
        else:
            _provider = HashingEmbeddingProvider()
    return _provider
