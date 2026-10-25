"""TMDB identity, configuration, attribution and credential boundaries."""
import io
import logging

import httpx
from PIL import Image
import pytest

from backend import tmdb_posters
from backend.posters import PosterError, validate_source
from backend.tmdb_posters import TMDBPosterProvider, configured_provider

TOKEN = "synthetic-test-token-01234567890123456789"


def photo(width=780, height=1170):
    out = io.BytesIO()
    Image.new("RGB", (width, height), "navy").save(out, "JPEG")
    return out.getvalue()


def movie(id=535167, title="流浪地球", year=2019, **changes):
    return {"id": id, "title": title, "original_title": title, "adult": False,
            "release_date": f"{year}-02-05", "poster_path": "/Poster0123.jpg", **changes}


@pytest.fixture(autouse=True)
def reset_backoff(monkeypatch):
    monkeypatch.setattr(tmdb_posters, "_NOT_BEFORE", 0.0)


def provider(movies=None, *, aliases=None, handle=None, raw=None):
    calls = []
    entries = [movie()] if movies is None else movies
    def fetch(request):
        calls.append(request)
        assert not request.headers.get("Cookie") and "api_key" not in request.url.params
        if request.url.host == "api.themoviedb.org":
            assert request.headers["Authorization"] == "Bearer " + TOKEN
        else:
            assert request.url.host == "image.tmdb.org"
            assert not request.headers.get("Authorization") and TOKEN not in str(request.url)
        if handle:
            result = handle(request)
            if result is not None:
                return result
        if request.url.path.endswith("search/movie"):
            return httpx.Response(200, json={"page": 1, "total_pages": 1 if entries else 0,
                                            "total_results": len(entries), "results": entries})
        if request.url.path.endswith("alternative_titles"):
            id = int(request.url.path.split("/")[-2])
            return httpx.Response(200, json={"id": id, "titles": [{"title": value} for value in (aliases or {}).get(id, [])]})
        if request.url.path.endswith("configuration"):
            return httpx.Response(200, json={"images": {"secure_base_url": tmdb_posters.IMAGE_BASE,
                                                       "poster_sizes": ["w500", "w780", "original"]}})
        return httpx.Response(200, content=photo() if raw is None else raw, headers={"Content-Type": "image/jpeg"})
    return TMDBPosterProvider(access_token=TOKEN, transport=httpx.MockTransport(fetch)), calls


def test_chinese_movie_download_has_own_source_and_never_sends_key_to_image_host():
    value, calls = provider()
    try:
        result = value.find("流浪地球", "2026-10-24T19:30")
        assert result.content == photo() and result.provider == "tmdb"
        assert result.source_page == "https://www.themoviedb.org/movie/535167"
        assert result.source_file == "/Poster0123.jpg"
        assert result.license_name == result.author == ""
        assert len(calls) == 3 and calls[0].url.params["language"] == "zh-CN"
        assert calls[-1].url.path == "/t/p/w780/Poster0123.jpg"
    finally:
        value.close()
    assert value.client.is_closed and not value.access_token and not value.availability()["available"]


@pytest.mark.parametrize("title", ["Example：Part Two", "Example: Part Two", "EXAMPLE - PART TWO"])
def test_small_punctuation_changes_do_not_require_alias_maps(title):
    value, _ = provider([movie(title="Example: Part Two")])
    try:
        assert value.find(title) is not None
    finally:
        value.close()


@pytest.mark.parametrize("title", ["Example (2D)", "Example（2D）", "Example ( 2d )", "Example (3D)"])
def test_ticket_projection_suffix_is_removed_from_search_and_identity(title):
    value, calls = provider([movie(title="Example")])
    try:
        assert value.find(title) is not None
        assert calls[0].url.params["query"] == "Example"
    finally:
        value.close()


def test_projection_suffix_preserves_release_year_and_remake_ambiguity():
    value, calls = provider([movie(1, "Example", 1999), movie(2, "Example", 2020)])
    try:
        assert value.find("Example (2D)") is None
        assert value.find("Example (1999)（2D）").source_page.endswith("/1")
        assert calls[-3].url.params["query"] == "Example"
        assert value.find("Example (2020) (2D)", "2010-01-01T19:30") is None
    finally:
        value.close()


@pytest.mark.parametrize("title", ["(2D)", "（3D）", "E" * 116 + " (2D)"])
def test_projection_suffix_does_not_bypass_empty_or_oversized_title_checks(title):
    value, calls = provider([movie(title="E" * 116)])
    try:
        assert value.find(title) is None
        assert calls == []
    finally:
        value.close()


@pytest.mark.parametrize("title", ["Example 2D Adventure", "Example (Part 2)", "Example (2DX)", "Example2"])
def test_meaningful_title_text_is_not_discarded_as_projection_metadata(title):
    value, _ = provider([movie(title="Example")])
    try:
        assert value.find(title) is None
    finally:
        value.close()


def test_verified_translated_alias_can_match_without_accepting_search_rank():
    value, _ = provider([movie(title="The Wandering Earth", original_title="流浪地球")], aliases={535167: ["流浪地球：流浪者"]})
    try:
        assert value.find("流浪地球：流浪者") is not None
        assert value.find("流浪地球：另一个电影") is None
    finally:
        value.close()


def test_partial_title_is_not_a_match_and_exact_title_beats_partial_search_result():
    value, _ = provider([movie(), movie(842675, "流浪地球2", 2023)])
    try:
        assert value.find("流浪地球").source_page.endswith("/535167")
        assert value.find("流浪") is None
    finally:
        value.close()


def test_remakes_are_ambiguous_even_if_only_one_has_a_poster_or_latest_is_popular():
    value, calls = provider([movie(1, "Example", 1999), movie(2, "Example", 2020, poster_path=None, popularity=999)])
    try:
        assert value.find("Example", "2010-01-01T19:30") is None
        assert len(calls) == 1
        assert value.find("Example (1999)") is not None
        assert value.find("Example（1999）") is not None
        assert value.find("Example (2020)", "2010-01-01T19:30") is None
    finally:
        value.close()


def test_same_year_different_id_and_matching_alternative_title_are_still_ambiguous():
    value, _ = provider([movie(1, "Example"), movie(2, "Alias")], aliases={2: ["Example"]})
    try:
        assert value.find("Example (2019)") is None
    finally:
        value.close()


@pytest.mark.parametrize("changes", [{"release_date": ""}, {"release_date": "2020-02-30"},
                                     {"poster_path": "https://evil.test/a.jpg"}, {"poster_path": "/../a.jpg"},
                                     {"poster_path": "/a.jpg?secret=yes"}, {"poster_path": "/a.svg"}, {"adult": True}])
def test_untrusted_movie_metadata_never_creates_a_cover(changes):
    value, calls = provider([movie(**changes)])
    try:
        assert value.find("流浪地球") is None
        assert all(request.url.host == "api.themoviedb.org" for request in calls)
    finally:
        value.close()


def test_showtime_year_only_rejects_future_releases_not_preview_screening_same_year():
    value, _ = provider()
    try:
        assert value.find("流浪地球", "2019-01-01T19:30") is not None
        assert value.find("流浪地球", "2018-12-31T19:30") is None
    finally:
        value.close()


def test_unread_pages_are_never_assumed_unique_and_page_identity_is_checked():
    def truncated(request):
        if request.url.path.endswith("search/movie"):
            return httpx.Response(200, json={"page": 1, "total_pages": 6, "total_results": 101, "results": [movie()]})
    value, calls = provider(handle=truncated)
    try:
        assert value.find("流浪地球") is None and len(calls) == 1
    finally:
        value.close()


def test_all_bounded_result_pages_checked_for_ambiguous_ids():
    def pages(request):
        if request.url.path.endswith("search/movie"):
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"page": page, "total_pages": 2, "total_results": 2,
                                            "results": [movie(page)]})
    value, calls = provider(handle=pages)
    try:
        assert value.find("流浪地球") is None
        assert len(calls) == 2
    finally:
        value.close()


@pytest.mark.parametrize("status", [301, 302, 401, 403, 500])
def test_rejected_api_responses_have_safe_errors_without_credentials(status):
    value, _ = provider(handle=lambda request: httpx.Response(status, text=TOKEN,
                                                            headers={"Location": "https://evil.test"}))
    try:
        with pytest.raises(PosterError) as error:
            value.find("流浪地球")
        assert TOKEN not in str(error.value) and "http" not in str(error.value)
    finally:
        value.close()


def test_rate_limit_sets_bounded_cooldown_and_does_not_leak_provider_body():
    value, calls = provider(handle=lambda request: httpx.Response(429, text=TOKEN, headers={"Retry-After": "60"}))
    try:
        with pytest.raises(PosterError, match="source_unavailable"):
            value.find("流浪地球")
        with pytest.raises(PosterError, match="rate_limited"):
            value.find("流浪地球")
        assert len(calls) == 1
    finally:
        value.close()


@pytest.mark.parametrize("raw", [b"invalid", photo(100, 200), photo(780, 300)])
def test_downloaded_image_is_decoded_and_must_meet_portrait_dimensions(raw):
    value, _ = provider(raw=raw)
    try:
        with pytest.raises(PosterError, match="invalid_image"):
            value.find("流浪地球")
    finally:
        value.close()


def test_configuration_cannot_redirect_image_download_or_receive_credentials():
    def evil_configuration(request):
        if request.url.path.endswith("configuration"):
            return httpx.Response(200, json={"images": {"secure_base_url": "https://evil.test/", "poster_sizes": ["w780"]}})
    value, calls = provider(handle=evil_configuration)
    try:
        with pytest.raises(PosterError, match="invalid_configuration"):
            value.find("流浪地球")
        assert all(request.url.host == "api.themoviedb.org" for request in calls)
    finally:
        value.close()


def test_oversized_metadata_is_rejected_before_json_decode():
    value, _ = provider(handle=lambda request: httpx.Response(200, content=b"{}", headers={
        "Content-Type": "application/json", "Content-Length": str(tmdb_posters.MAX_METADATA_BYTES + 1)}))
    try:
        with pytest.raises(PosterError, match="response_too_large"):
            value.find("流浪地球")
    finally:
        value.close()


def test_stopping_cancels_before_network():
    value, calls = provider()
    try:
        with pytest.raises(PosterError, match="cancelled"):
            value.find("流浪地球", stopping=lambda: True)
        assert calls == []
    finally:
        value.close()


def test_explicit_provider_selection_and_missing_private_key(monkeypatch):
    monkeypatch.setenv("KEEPSITE_POSTER_PROVIDER", "tmdb")
    monkeypatch.delenv("KEEPSITE_TMDB_READ_ACCESS_TOKEN", raising=False)
    value = configured_provider()
    try:
        assert isinstance(value, TMDBPosterProvider)
        assert not value.availability()["available"]
        assert "API" in value.availability()["reason"]
        with pytest.raises(PosterError, match="not_configured"):
            value.find("流浪地球")
    finally:
        value.close()
    monkeypatch.setenv("KEEPSITE_POSTER_PROVIDER", "invalid")
    with pytest.raises(ValueError, match="Unsupported poster provider"):
        configured_provider()


def test_read_token_is_absent_from_httpx_logs_and_client_urls(caplog):
    value, _ = provider()
    caplog.set_level(logging.INFO, logger="httpx")
    try:
        assert value.find("流浪地球") is not None
        assert TOKEN not in caplog.text and "api_key" not in caplog.text
        assert "HTTP Request:" in caplog.text
    finally:
        value.close()


def test_bearer_token_is_only_sent_to_canonical_api():
    token = "synthetic-bearer-token-01234567890123456789"
    requests = []
    def fetch(request):
        requests.append(request)
        assert "api_key" not in request.url.params
        if request.url.host == "api.themoviedb.org":
            assert request.headers["Authorization"] == "Bearer " + token
            return httpx.Response(200, json={"page": 1, "total_pages": 0, "total_results": 0, "results": []})
        assert not request.headers.get("Authorization")
        return httpx.Response(200, content=b"image", headers={"Content-Type": "image/jpeg"})
    value = TMDBPosterProvider(access_token=token, transport=httpx.MockTransport(fetch))
    try:
        assert value.find("Missing movie") is None
        value._fetch("w780/Poster0123.jpg", image=True, deadline=tmdb_posters.time.monotonic() + 5, stopping=lambda: False)
        assert len(requests) == 2
        assert value.availability()["available"]
    finally:
        value.close()
    assert value.access_token == ""


def test_tmdb_attribution_does_not_claim_image_authorship_or_a_cc_license():
    source = {"provider": "tmdb", "sourcePage": "https://www.themoviedb.org/movie/535167", "sourceFile": "/Poster0123.jpg",
              "termsUrl": tmdb_posters.TERMS_URL, "fetchedAt": 1}
    assert validate_source(source)
    for changes in ({"sourcePage": "https://www.themoviedb.org/movie/0"}, {"sourceFile": "/../evil.jpg"},
                    {"sourcePage": "https://www.themoviedb.org.evil.test/movie/1"}, {"termsUrl": "https://evil.test"},
                    {"author": "Invented"}, {"licenseName": "Public domain"}, {"fetchedAt": True}):
        assert not validate_source({**source, **changes})
