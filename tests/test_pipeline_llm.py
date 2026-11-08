import io
import json
import urllib.error

import pytest

from schemagraph.config import LLMConfig
from schemagraph.pipeline import llm


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(
            {
                "choices": [{"message": {"content": "SELECT 1"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        ).encode()


def test_complete_sends_configured_bearer_key_and_parses_usage(monkeypatch):
    config = LLMConfig(model="test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    result = llm.complete(config, [{"role": "user", "content": "count rows"}])

    assert captured["request"].full_url == "https://api.openai.com/v1/chat/completions"
    assert captured["request"].get_header("Authorization") == "Bearer test-secret"
    assert captured["timeout"] == 60.0
    assert result == ("SELECT 1", 4, 2)


def test_complete_omits_authorization_for_local_endpoint(monkeypatch):
    config = LLMConfig(model="test-model", base_url="http://localhost:8000/v1", api_key_env=None)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    llm.complete(config, [{"role": "user", "content": "count rows"}])

    assert captured["request"].get_header("Authorization") is None


def _http_error(code, body: bytes):
    return urllib.error.HTTPError("https://example.test/v1/chat/completions", code, "err", {}, io.BytesIO(body))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (json.dumps({"error": {"message": "The model `x` does not exist", "type": "invalid_request_error"}}).encode(),
         "The model `x` does not exist"),
        (json.dumps([{"error": {"code": 404, "message": "models/gemini-9 is not found", "status": "NOT_FOUND"}}]).encode(),
         "models/gemini-9 is not found"),
        (b"<html>Not Found</html>", "<html>Not Found</html>"),
        (json.dumps({"error": "plain string error"}).encode(), "plain string error"),
        (b"", ""),
    ],
)
def test_error_detail_reads_the_services_explanation(body, expected):
    assert llm.error_detail(body) == expected


def test_error_detail_is_clipped():
    assert len(llm.error_detail(b"x" * 5000)) == 300


def test_http_errors_say_why_and_where(monkeypatch):
    config = LLMConfig(model="gemini-9", base_url="https://example.test/v1", api_key_env=None)
    body = json.dumps([{"error": {"message": "models/gemini-9 is not found"}}]).encode()

    def fake_urlopen(request, timeout):
        raise _http_error(404, body)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMError) as caught:
        llm.complete(config, [{"role": "user", "content": "q"}])
    message = str(caught.value)
    assert message.startswith("chat service returned HTTP 404: models/gemini-9 is not found")
    assert "POST https://example.test/v1/chat/completions" in message and "model 'gemini-9'" in message


def test_http_error_with_an_empty_body_still_names_the_request(monkeypatch):
    config = LLMConfig(model="m", base_url="https://example.test/v1", api_key_env=None)
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(_http_error(500, b"")))
    with pytest.raises(llm.LLMError, match=r"HTTP 500 \(POST https://example.test/v1/chat/completions"):
        llm.complete(config, [{"role": "user", "content": "q"}])


def test_the_api_key_never_appears_in_an_error(monkeypatch):
    config = LLMConfig(model="m", base_url="https://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-very-secret")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(_http_error(401, b'{"error": {"message": "bad key"}}')))
    with pytest.raises(llm.LLMError) as caught:
        llm.complete(config, [{"role": "user", "content": "q"}])
    assert "sk-very-secret" not in str(caught.value) and "bad key" in str(caught.value)
