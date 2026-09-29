"""GeminiProvider against a mocked HTTP transport (no API key, no network), plus the per-task
provider routing. Proves our request/response handling, not Google's behaviour."""

import json
import logging
from typing import Any

import httpx
import pytest

from app.ai.gateway import TaskRoutingProvider, build_provider
from app.ai.gemini_provider import GeminiProvider, json_schema_for
from app.ai.mock_provider import MockAIProvider
from app.ai.provider import (
    AIInvalidOutputError,
    AIRefusalError,
    AITask,
    AITimeoutError,
    AIUnavailableError,
)
from app.common.config import Settings, get_settings
from app.conversations.assist import MessageAssist

KEY = "AIza-TEST-KEY-never-logged"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import gemini_provider

    monkeypatch.setattr(gemini_provider, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))


def answer(payload: dict[str, Any] | str, *, finish: str = "STOP", **extra: Any) -> httpx.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "(reasoning)", "thought": True}, {"text": text}],
                    },
                    "finishReason": finish,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 40,
                "thoughtsTokenCount": 15,
            },
            "modelVersion": "gemini-3.8-flash",
            **extra,
        },
    )


def provider(handler: Any) -> tuple[GeminiProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    p = GeminiProvider(
        KEY, dict.fromkeys(AITask, "gemini-3.8-flash"), transport=httpx.MockTransport(record)
    )
    return p, seen


async def generate(p: GeminiProvider) -> Any:
    return await p.generate(
        task=AITask.MESSAGE_ASSIST,
        system="SYSTEM RULES",
        prompt="<latest_customer_message>Price for 100 units?</latest_customer_message>",
        schema=MessageAssist,
        context=None,
        max_tokens=1200,
        timeout_seconds=5,
    )


REPLY = {
    "reply": "Thank you for your message. I will confirm the price for 100 units shortly.",
    "notes": [{"kind": "REQUIREMENT", "text": "100 units", "confidence": 0.8}],
    "actions": [{"title": "Send price for 100 units", "kind": "FOLLOW_UP", "due_in_days": 1}],
}


async def test_generates_validated_structured_output_and_sends_a_correct_request() -> None:
    p, seen = provider(lambda r: answer(REPLY))
    result = await generate(p)
    assert result.output.reply.startswith("Thank you")
    assert result.output.notes[0].text == "100 units"
    assert result.usage.provider == "gemini"
    assert result.usage.model == "gemini-3.8-flash"
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 55  # thinking tokens count against the budget
    [req] = seen
    assert str(req.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    )
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)  # never in the URL (URLs are logged)
    body = json.loads(req.content)
    assert body["systemInstruction"] == {"parts": [{"text": "SYSTEM RULES"}]}
    assert body["contents"][0]["parts"][0]["text"].startswith("<latest_customer_message>")
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["thinkingConfig"] == {"thinkingLevel": "low"}
    assert cfg["maxOutputTokens"] > 1200
    schema_text = json.dumps(cfg["responseJsonSchema"])
    assert "$ref" not in schema_text
    assert "$defs" not in schema_text
    assert cfg["responseJsonSchema"]["required"] == ["reply"]


def test_schema_inlines_references() -> None:
    schema = json_schema_for(MessageAssist)
    notes = schema["properties"]["notes"]["items"]
    assert notes["properties"]["kind"]["enum"]  # NoteKind enum inlined from $defs
    assert "title" not in schema


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}}), AIUnavailableError),
        (httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}), AIUnavailableError),
        (httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}), AIUnavailableError),
        (httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}}), AIUnavailableError),
        (httpx.Response(404, text="not json"), AIUnavailableError),
        (answer(REPLY, finish="SAFETY"), AIRefusalError),
        (answer(REPLY, promptFeedback={"blockReason": "PROHIBITED_CONTENT"}), AIRefusalError),
        (answer(REPLY, finish="MAX_TOKENS"), AIInvalidOutputError),
        (answer("not json at all"), AIInvalidOutputError),
        (answer({"notes": []}), AIInvalidOutputError),  # required "reply" missing
        (answer({"reply": "x" * 1001}), AIInvalidOutputError),  # violates max_length
        (httpx.Response(200, json={"candidates": []}), AIInvalidOutputError),
    ],
)
async def test_provider_failures_become_ai_errors(response: httpx.Response, error: type) -> None:
    p, _ = provider(lambda r: response)
    with pytest.raises(error):
        await generate(p)


async def test_timeout_and_network_errors() -> None:
    def timeout(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    def down(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=r)

    with pytest.raises(AITimeoutError):
        await generate(provider(timeout)[0])
    with pytest.raises(AIUnavailableError):
        await generate(provider(down)[0])


async def test_api_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ok, _ = provider(lambda r: answer(REPLY))
    await generate(ok)
    bad, _ = provider(
        lambda r: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
    )
    with pytest.raises(AIUnavailableError):
        await generate(bad)
    assert KEY not in caplog.text


# ---- configuration and routing -------------------------------------------------------------------


def settings(**update: Any) -> Settings:
    return get_settings().model_copy(update=update)


def test_only_message_assist_is_routed_to_gemini() -> None:
    s = settings(
        ai_provider="mock",
        ai_message_assist_provider="gemini",
        gemini_api_key=type(get_settings().jwt_secret)(KEY),
    )
    built = build_provider(s)
    assert isinstance(built, TaskRoutingProvider)
    assert isinstance(built.default, MockAIProvider)
    assert isinstance(built.routes[AITask.MESSAGE_ASSIST], GeminiProvider)
    assert AITask.COPILOT not in built.routes


def test_default_configuration_stays_mock() -> None:
    assert isinstance(build_provider(settings(ai_provider="mock")), MockAIProvider)
    s = settings(ai_provider="mock", ai_message_assist_provider="mock")
    assert isinstance(build_provider(s), MockAIProvider)


def test_gemini_requires_a_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        Settings(_env_file=None, ai_message_assist_provider="gemini")


def _required_are_defined(node: Any, path: str = "$") -> list[str]:
    """Every name in "required" must exist in "properties" (Gemini rejects the schema otherwise)."""
    problems: list[str] = []
    if isinstance(node, dict):
        props = node.get("properties")
        for name in node.get("required", []):
            if not isinstance(props, dict) or name not in props:
                problems.append(f"{path}: required '{name}' is not a property")
        for key, value in node.items():
            problems += _required_are_defined(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            problems += _required_are_defined(value, f"{path}[{i}]")
    return problems


def test_fields_named_title_survive_schema_cleanup() -> None:
    """Regression: Google returned 400 INVALID_ARGUMENT "schema at properties.actions.items
    requires unspecified property 'title'" because title annotations AND a field named "title"
    were both removed."""
    schema = json_schema_for(MessageAssist)
    action = schema["properties"]["actions"]["items"]
    assert "title" in action["properties"]
    assert action["required"] == ["title"]
    assert "title" not in schema  # the annotation is still dropped
    assert "title" not in action
    assert _required_are_defined(schema) == []


@pytest.mark.parametrize("model", ["KnowledgeAnswer", "PostCallAnalysis", "CopilotDelta"])
def test_every_ai_schema_is_self_consistent(model: str) -> None:
    import importlib

    module = {
        "KnowledgeAnswer": "app.knowledge.service",
        "PostCallAnalysis": "app.postcall.service",
        "CopilotDelta": "app.copilot.engine",
    }[model]
    assert (
        _required_are_defined(json_schema_for(getattr(importlib.import_module(module), model)))
        == []
    )


async def test_transient_503_is_retried_and_recovers() -> None:
    """Google returned 503 UNAVAILABLE for gemini-3.8-flash under load; a retry succeeds."""
    responses = iter(
        [httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}}), answer(REPLY)]
    )
    p, seen = provider(lambda r: next(responses))
    result = await generate(p)
    assert result.output.reply.startswith("Thank you")
    assert len(seen) == 2


async def test_gives_up_after_bounded_retries() -> None:
    p, seen = provider(lambda r: httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}}))
    with pytest.raises(AIUnavailableError):
        await generate(p)
    assert len(seen) == 3  # 1 + 2 retries, never unbounded


async def test_request_errors_are_not_retried() -> None:
    p, seen = provider(
        lambda r: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}})
    )
    with pytest.raises(AIUnavailableError):
        await generate(p)
    assert len(seen) == 1
