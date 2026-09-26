"""Google Gemini (Gemini Developer API) implementation of AIProvider.

REST ``POST {base}/v1beta/models/{model}:generateContent`` with structured JSON output
(``responseMimeType=application/json`` + ``responseJsonSchema`` from our Pydantic schema). The
model's JSON is always re-validated against the schema here, so callers never receive
unvalidated text. The API key travels only in the ``x-goog-api-key`` header - never in a URL,
never logged (httpx logs the URL only; this module logs status codes only).

Status: IMPLEMENTED, tested with a mocked HTTP transport. NOT LIVE VERIFIED until a request with
a real GEMINI_API_KEY succeeds.
"""

import copy
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.ai.provider import (
    AIInvalidOutputError,
    AIRefusalError,
    AIResult,
    AITask,
    AITimeoutError,
    AIUnavailableError,
    AIUsage,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
# Headroom for the model's internal reasoning ("thinking" tokens count toward maxOutputTokens).
THINKING_HEADROOM_TOKENS = 1024
_REFUSAL_REASONS = {
    "SAFETY",
    "BLOCKED_SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
}


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON Schema with local $refs inlined and titles dropped (smaller, and avoids
    depending on $ref support in the provider's schema subset)."""
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).rsplit("/", 1)[-1]
                return resolve(copy.deepcopy(defs[name]))
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    out: dict[str, Any] = resolve(raw)
    return out


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        models: dict[AITask, str],
        *,
        thinking_level: str | None = "low",
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._models = models
        self._thinking_level = thinking_level or None
        self._base = base_url.rstrip("/")
        self._transport = transport

    async def generate[T: BaseModel](
        self,
        *,
        task: AITask,
        system: str,
        prompt: str,
        schema: type[T],
        context: BaseModel | None,
        max_tokens: int,
        timeout_seconds: float,
    ) -> AIResult[T]:
        model = self._models[task]
        generation: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": json_schema_for(schema),
            "maxOutputTokens": max_tokens
            + (THINKING_HEADROOM_TOKENS if self._thinking_level else 0),
            "temperature": 0.3,
        }
        if self._thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": self._thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        url = f"{self._base}/v1beta/models/{model}:generateContent"
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, transport=self._transport
            ) as http:
                resp = await http.post(
                    url,
                    json=body,
                    headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise AITimeoutError("gemini request timed out") from exc
        except httpx.HTTPError as exc:
            raise AIUnavailableError(f"gemini network error: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            status = _error_status(resp)
            logger.warning(
                "gemini_api_error",
                extra={"status": resp.status_code, "error": status, "task": task},
            )
            raise AIUnavailableError(f"status {resp.status_code} {status or ''}".strip())

        try:
            data = resp.json()
        except ValueError as exc:
            raise AIInvalidOutputError("non-JSON response from gemini") from exc
        meta = data.get("usageMetadata") or {}
        usage = AIUsage(
            provider=self.name,
            model=str(data.get("modelVersion") or model),
            input_tokens=int(meta.get("promptTokenCount") or 0),
            # Thinking tokens are billed as output: count them against the budget too.
            output_tokens=int(meta.get("candidatesTokenCount") or 0)
            + int(meta.get("thoughtsTokenCount") or 0),
        )
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise AIRefusalError(f"prompt blocked ({block})")
        candidates = data.get("candidates") or []
        if not candidates:
            raise AIInvalidOutputError("no candidates returned")
        cand = candidates[0]
        reason = str(cand.get("finishReason") or "")
        if reason in _REFUSAL_REASONS:
            raise AIRefusalError(f"generation stopped ({reason})")
        if reason == "MAX_TOKENS":
            raise AIInvalidOutputError("output truncated at max tokens")
        text = "".join(
            str(p.get("text", ""))
            for p in (cand.get("content") or {}).get("parts") or []
            if isinstance(p, dict) and not p.get("thought")
        ).strip()
        if not text:
            raise AIInvalidOutputError("empty response")
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            raise AIInvalidOutputError("schema validation failed") from exc
        return AIResult(output=parsed, usage=usage)


def _error_status(resp: httpx.Response) -> str | None:
    """Google's error status (e.g. PERMISSION_DENIED, RESOURCE_EXHAUSTED); bodies aren't logged."""
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        return None
    status = err.get("status") if isinstance(err, dict) else None
    return str(status)[:64] if status else None
