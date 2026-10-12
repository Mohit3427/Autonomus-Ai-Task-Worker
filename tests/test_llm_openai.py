"""The OpenAI-compatible adapter round-trips the loop's message format faithfully."""
import json
from types import SimpleNamespace

from worker.llm_openai import _clean_schema, from_openai_response, to_openai_messages


def test_response_parsing_and_verbatim_replay():
    data = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Opening the portal.",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "browser_navigate", "arguments": '{"url": "http://x"}'},
                        "extra_content": {"google": {"thought_signature": "SIG"}}}]}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 20}}}
    r = from_openai_response(data)
    assert r.stop_reason == "tool_use"
    tu = next(b for b in r.content if b.type == "tool_use")
    assert (tu.id, tu.name, tu.input, tu.parse_error) == ("c1", "browser_navigate", {"url": "http://x"}, None)
    assert (r.usage.input_tokens, r.usage.cache_read_input_tokens, r.usage.output_tokens) == (100, 20, 30)

    history = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": r.content},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "page...", "is_error": True},
            {"type": "text", "text": "[harness] note"},
        ]},
    ]
    msgs = to_openai_messages("SYS", history)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    # the provider's message (incl. thought signature) is replayed unchanged
    assert msgs[2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: page..."}
    assert msgs[4]["content"] == "[harness] note"


def test_invalid_tool_json_is_flagged_not_crashed():
    data = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "remember", "arguments": "{bad json"}}]}}]}
    tu = next(b for b in from_openai_response(data).content if b.type == "tool_use")
    assert tu.input == {} and tu.parse_error


def test_schema_cleaning_and_fallback_assistant_conversion():
    assert _clean_schema({"type": "object", "additionalProperties": False,
                          "properties": {"a": {"type": "object", "additionalProperties": False}}}) == \
        {"type": "object", "properties": {"a": {"type": "object"}}}
    blocks = [SimpleNamespace(type="text", text="hi"),
              SimpleNamespace(type="tool_use", id="t1", name="finish", input={"status": "completed"})]
    m = to_openai_messages("S", [{"role": "assistant", "content": blocks}])[1]
    assert m["tool_calls"][0]["function"]["name"] == "finish"
    assert json.loads(m["tool_calls"][0]["function"]["arguments"]) == {"status": "completed"}


import httpx  # noqa: E402
import pytest  # noqa: E402

import worker.llm_openai as lo  # noqa: E402
from worker.llm import LLMError  # noqa: E402

OK = {"model": "", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}]}
DAILY = '{"error":{"code":429,"details":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"},' \
        '{"retryDelay":"3600s"}]}}'


def client_with(handler, chain=("a", "b")):
    seen = []

    def wrapped(request):
        seen.append(json_model(request))
        return handler(seen[-1], len(seen))

    c = lo.OpenAICompatClient(chain[0], "http://llm.test/v1", "k", fallback_models=list(chain[1:]),
                              transport=httpx.MockTransport(wrapped))
    return c, seen


def json_model(request):
    import json as _json
    return _json.loads(request.content)["model"]


@pytest.fixture(autouse=True)
def _reset_quota_memory(monkeypatch):
    lo._exhausted_until.clear()
    monkeypatch.setattr(lo.asyncio, "sleep", lambda s: _noop())


async def _noop():
    return None


async def test_daily_quota_switches_to_next_model_and_is_remembered():
    c, seen = client_with(lambda model, n: httpx.Response(429, text=DAILY) if model == "a" else httpx.Response(200, json=OK))
    r = await c.complete(system="s", tools=[], messages=[{"role": "user", "content": "x"}], effort="low")
    assert r.content[0].text == "hi" and seen == ["a", "b"] and c.model == "b"
    # a new client (next run) skips the exhausted model without spending a request on it
    c2, seen2 = client_with(lambda model, n: httpx.Response(200, json=OK))
    await c2.complete(system="s", tools=[], messages=[{"role": "user", "content": "x"}], effort="low")
    assert seen2 == ["b"]


async def test_persistent_overload_switches_model():
    c, seen = client_with(lambda model, n: httpx.Response(503, text="busy") if model == "a" else httpx.Response(200, json=OK))
    await c.complete(system="s", tools=[], messages=[{"role": "user", "content": "x"}], effort="low")
    assert seen == ["a", "a", "a", "b"]


async def test_all_models_exhausted_gives_clear_error():
    c, _ = client_with(lambda model, n: httpx.Response(429, text=DAILY))
    with pytest.raises(LLMError, match="quota is used up for every configured model"):
        await c.complete(system="s", tools=[], messages=[{"role": "user", "content": "x"}], effort="low")
