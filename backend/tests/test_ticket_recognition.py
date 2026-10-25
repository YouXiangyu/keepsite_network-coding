"""Offline provider contracts; never use a real key, image or paid request."""
import base64
from io import BytesIO
import json
import logging

import httpx
from PIL import Image, ImageCms
import pytest

from backend import ticket_recognition as recognition
from backend.ticket_recognition import RecognitionError

KEY = "synthetic-qwen-test-key"
FIELDS = {"title": "示例电影", "cinema": "示例影院", "hall": "3厅", "seat": "8排12座", "showtime": "2026-10-25T19:30"}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("KEEPSITE_QWEN_API_KEY", KEY)
    monkeypatch.setenv("KEEPSITE_QWEN_BASE_URL", "https://workspace.example/compatible-mode/v1/")
    monkeypatch.setenv("KEEPSITE_QWEN_MODEL", "qwen3.7-flash")


def image(mode="RGB", size=(96, 64), *, exif=None, icc_profile=None):
    output = BytesIO()
    with Image.new(mode, size, (20, 50, 100, 0) if mode == "RGBA" else "navy") as picture:
        options = {}
        if exif is not None:
            options["exif"] = exif
        if icc_profile is not None:
            options["icc_profile"] = icc_profile
        picture.save(output, "PNG", **options)
    return output.getvalue()


class Body(httpx.SyncByteStream):
    def __init__(self, *chunks):
        self.chunks, self.closed, self.reads = chunks, False, 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def envelope(content=None, **changes):
    result = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": content if content is not None else json.dumps(FIELDS, ensure_ascii=False)}}]}
    result.update(changes)
    return json.dumps(result, ensure_ascii=False).encode()


def response(raw=None, *, status=200, headers=None, stream=None):
    return httpx.Response(status, headers=headers or {"Content-Type": "application/json"},
                          stream=stream or Body(raw if raw is not None else envelope()))


def test_request_is_one_bounded_non_thinking_image_call_and_logs_no_sensitive_content(caplog):
    calls = []
    raw = image()
    before = raw[:]
    body = Body(envelope())
    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://workspace.example/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert KEY not in str(request.url) and "Cookie" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3.7-flash"
        assert payload["stream"] is False and payload["enable_thinking"] is False
        assert payload["max_tokens"] == 1024 and payload["response_format"] == {"type": "json_object"}
        assert not {"tools", "tool_choice", "web_search"} & payload.keys()
        assert "图内" in payload["messages"][0]["content"]
        picture_url = payload["messages"][1]["content"][1]["image_url"]["url"]
        assert picture_url.startswith("data:image/jpeg;base64,")
        encoded = base64.b64decode(picture_url.split(",", 1)[1], validate=True)
        assert len(encoded) <= recognition.MAX_PROVIDER_IMAGE_BYTES
        with Image.open(BytesIO(encoded)) as picture:
            assert picture.format == "JPEG" and picture.size == (96, 64)
        return response(stream=body)
    with caplog.at_level(logging.DEBUG):
        assert recognition.recognize(raw, transport=httpx.MockTransport(handler)) == FIELDS
    assert len(calls) == 1 and body.closed and raw == before
    assert KEY not in caplog.text and FIELDS["cinema"] not in caplog.text and "base64," not in caplog.text


@pytest.mark.parametrize("name", ["KEEPSITE_QWEN_API_KEY", "KEEPSITE_QWEN_BASE_URL", "KEEPSITE_QWEN_MODEL"])
def test_each_configuration_is_explicit_and_missing_config_never_makes_a_request(monkeypatch, name):
    monkeypatch.delenv(name)
    assert recognition.availability()["available"] is False
    with pytest.raises(RecognitionError) as caught:
        recognition.recognize(image(), transport=httpx.MockTransport(lambda request: pytest.fail("No configured provider")))
    assert caught.value.code == "not_configured" and caught.value.retryable is False


@pytest.mark.parametrize("base", ["http://workspace.example/v1", "https://key:secret@workspace.example/v1",
                                   "https://workspace.example/v1?api_key=secret", "https://workspace.example/v1#key",
                                   "https://workspace.example:8443/v1", "https://workspace.example/a/../v1",
                                   "https://workspace.example/%2e%2e/v1", "https://workspace.example\\evil/v1"])
def test_bad_base_urls_are_unavailable_without_exposing_values(monkeypatch, base):
    monkeypatch.setenv("KEEPSITE_QWEN_BASE_URL", base)
    status = recognition.availability()
    assert status["available"] is False and "secret" not in status["reason"] and base not in status["reason"]


def test_preparation_bounds_size_applies_exif_and_white_alpha_without_mutating_source():
    orientation = Image.Exif()
    orientation[274] = 6
    raw = image("RGBA", (3000, 1000), exif=orientation.tobytes())
    prepared = recognition.prepare_image(raw)
    with Image.open(BytesIO(prepared)) as picture:
        assert picture.size == (683, 2048)
        assert picture.mode == "RGB" and picture.getpixel((50, 50)) == (255, 255, 255)
        assert not picture.info.get("exif")
    with Image.open(BytesIO(raw)) as source:
        assert source.size == (3000, 1000) and source.getexif()[274] == 6


def test_valid_srgb_profile_is_normalized_and_not_forwarded_as_metadata():
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    prepared = recognition.prepare_image(image(icc_profile=profile))
    with Image.open(BytesIO(prepared)) as picture:
        assert picture.mode == "RGB" and picture.size == (96, 64) and "icc_profile" not in picture.info


@pytest.mark.parametrize("mode,profiled", [("RGBA", False), ("RGBA", True), ("LA", False)])
def test_alpha_is_preserved_through_color_conversion_then_composited_on_white(mode, profiled):
    output = BytesIO()
    color = (20, 40, 80, 0) if mode == "RGBA" else (20, 0)
    with Image.new(mode, (24, 24), color) as picture:
        options = {"icc_profile": ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()} if profiled else {}
        picture.save(output, "PNG", **options)
    prepared = recognition.prepare_image(output.getvalue())
    with Image.open(BytesIO(prepared)) as picture:
        assert picture.getpixel((12, 12)) == (255, 255, 255)


@pytest.mark.parametrize("raw", [b"", b"not an image", b"x" * (12 * 1024 * 1024 + 1)], ids=["empty", "invalid", "too-large"])
def test_invalid_source_is_rejected_before_call(raw):
    with pytest.raises(RecognitionError) as caught:
        recognition.recognize(raw, transport=httpx.MockTransport(lambda request: pytest.fail("Invalid source was sent")))
    assert caught.value.code == "invalid_image" and caught.value.retryable is False


def test_decoded_source_pixel_and_encoded_payload_limits(monkeypatch):
    monkeypatch.setattr(recognition, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(RecognitionError, match="图片"):
        recognition.prepare_image(image())
    monkeypatch.setattr(recognition, "MAX_IMAGE_PIXELS", 40_000_000)
    monkeypatch.setattr(recognition, "MAX_PROVIDER_IMAGE_BYTES", 1)
    with pytest.raises(RecognitionError) as caught:
        recognition.prepare_image(image())
    assert caught.value.code == "image_too_large"


@pytest.mark.parametrize("fields", [
    {}, {**FIELDS, "extra": "do something"}, {**FIELDS, "title": ["nested"]},
    {**FIELDS, "seat": None}, {**FIELDS, "hall": 1}, {**FIELDS, "title": "x" * 121},
    {**FIELDS, "cinema": "x" * 201}, {**FIELDS, "seat": "line\nfeed"},
    {**FIELDS, "showtime": "10-25 19:30"}, {**FIELDS, "showtime": "2026-02-30T19:30"},
    {**FIELDS, "showtime": "2026-10-25T19:30+08:00"},
])
def test_only_five_bounded_strings_and_complete_showtime_can_be_published(fields):
    with pytest.raises(RecognitionError) as caught:
        recognition.parse_response(envelope(json.dumps(fields)))
    assert caught.value.code == "invalid_response" and caught.value.retryable is False


@pytest.mark.parametrize("title,expected", [
    ("奥德赛（2D 英文版）", "奥德赛"),
    ("奥德赛 (3D) 中文配音", "奥德赛"),
    ("流浪地球2（国语版）", "流浪地球2"),
    ("电影第二部（中文版）", "电影第二部"),
    ("电影第三部 英文版", "电影第三部"),
    ("电影第四部 (2D)", "电影第四部"),
    ("Example Part II", "Example Part II"),
    ("英语课", "英语课"),
    ("英语", "英语"),
])
def test_model_title_cleanup_publishes_the_movie_name_and_preserves_other_fields(title, expected):
    fields = {**FIELDS, "title": title}
    assert recognition.parse_response(envelope(json.dumps(fields))) == {**fields, "title": expected}
    assert fields["title"] == title


def test_presentation_cleanup_cannot_make_an_oversized_model_answer_valid():
    with pytest.raises(RecognitionError):
        recognition.parse_response(envelope(json.dumps({**FIELDS, "title": "E" * 116 + " (2D)"})))


@pytest.mark.parametrize("content", [
    '```json\n{"title":"film"}\n```', '[{"title":"film"}]',
    '{"title":"first","title":"second","cinema":"","hall":"","seat":"","showtime":""}',
    '{"title":NaN,"cinema":"","hall":"","seat":"","showtime":""}', "x" * (8 * 1024 + 1),
], ids=["markdown", "array", "duplicate-field", "nan", "text-too-large"])
def test_model_answer_is_data_not_markdown_or_a_repairable_schema(content):
    with pytest.raises(RecognitionError):
        recognition.parse_response(envelope(content))


def test_unknowns_are_empty_and_output_cannot_trigger_tool_execution():
    blank = dict.fromkeys(FIELDS, "")
    assert recognition.parse_response(envelope(json.dumps(blank))) == blank
    for message in [{"role": "assistant", "content": json.dumps(FIELDS), "tool_calls": [{"function": "execute"}]},
                    {"role": "assistant", "content": json.dumps(FIELDS), "refusal": "refused"}]:
        with pytest.raises(RecognitionError):
            recognition.parse_response(envelope(choices=[{"finish_reason": "stop", "message": message}]))
    with pytest.raises(RecognitionError):
        recognition.parse_response(envelope(choices=[{"finish_reason": "length", "message": {"role": "assistant", "content": json.dumps(FIELDS)}}]))


@pytest.mark.parametrize("raw", [b'{"choices":[],"choices":[]}', b'{"choices":', b'[]', b'{}'])
def test_invalid_envelopes_and_duplicate_keys_are_rejected(raw):
    with pytest.raises(RecognitionError):
        recognition.parse_response(raw)


@pytest.mark.parametrize("status,code,retryable", [
    (400, "configuration_rejected", False), (401, "configuration_rejected", False),
    (403, "configuration_rejected", False), (404, "configuration_rejected", False),
    (402, "quota_exhausted", False), (429, "rate_limited", True),
    (503, "service_unavailable", True), (302, "configuration_rejected", False),
])
def test_upstream_status_classification_closes_response_and_never_adds_hidden_retries(status, code, retryable):
    calls = []
    body = Body(b'{"error":{"message":"sensitive provider message"}}')
    def handler(request):
        calls.append(request)
        return response(status=status, stream=body, headers={"Content-Type": "application/json", "Location": "https://other.example/"})
    with pytest.raises(RecognitionError) as caught:
        recognition.recognize(image(), transport=httpx.MockTransport(handler))
    assert caught.value.code == code and caught.value.retryable is retryable
    assert "sensitive" not in str(caught.value) and len(calls) == 1 and body.closed


@pytest.mark.parametrize("provider_code", ["insufficient_quota", "AllocationQuota.FreeTierOnly", "InsufficientBalance"])
def test_quota_429_is_permanent_not_blindly_retried(provider_code):
    raw = json.dumps({"error": {"code": provider_code, "message": "secret provider details"}}).encode()
    with pytest.raises(RecognitionError) as caught:
        recognition.recognize(image(), transport=httpx.MockTransport(lambda request: response(raw, status=429)))
    assert caught.value.code == "quota_exhausted" and caught.value.retryable is False


@pytest.mark.parametrize("headers,raw", [
    ({"Content-Type": "text/html"}, b"provider secret"),
    ({"Content-Type": "application/json", "Content-Length": "999999"}, b""),
    ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"compressed"),
    ({"Content-Type": "application/json"}, b"x" * (64 * 1024 + 1)),
], ids=["html", "declared-too-large", "compressed", "stream-too-large"])
def test_response_types_compression_and_body_limits_are_bounded(headers, raw):
    body = Body(raw)
    with pytest.raises(RecognitionError) as caught:
        recognition.recognize(image(), transport=httpx.MockTransport(lambda request: response(stream=body, headers=headers)))
    assert caught.value.code == "invalid_response" and body.closed


def test_slow_or_failed_response_is_closed_and_mapped_to_safe_retryable_error():
    body = Body(b'{"choices":', httpx.ReadTimeout("sensitive upstream timeout"))
    with pytest.raises(RecognitionError) as caught:
        recognition.recognize(image(), transport=httpx.MockTransport(lambda request: response(stream=body)))
    assert caught.value.code == "service_unavailable" and caught.value.retryable and body.closed
    assert "sensitive" not in str(caught.value)
