"""Commons matching and download boundaries; synthetic bytes, no network."""
from copy import deepcopy
import io
import json
import re
import threading
import time
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from backend import posters
from backend.posters import CommonsPosterProvider, PosterError


def photo(width=620, height=1200, format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "navy").save(output, format=format)
    return output.getvalue()


def page(title="Safety Last", year=1923, *, raw=None, width=620, height=1200):
    raw = raw if raw is not None else photo(width, height)
    return {"pageid": 1, "ns": 6, "title": f"File:{title} ({year}) poster.jpg", "imagerepository": "local",
            "imageinfo": [{"size": len(raw), "width": width, "height": height, "mime": "image/jpeg",
                "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Poster.jpg",
                "extmetadata": {
                    "ImageDescription": {"value": f"Poster for the {year} film <i>{title}</i>."},
                    "Artist": {"value": "<a href='https://example.test'>Studio</a>"},
                    "LicenseShortName": {"value": "Public domain"}, "Copyrighted": {"value": "False"},
                    "License": {"value": "pd"}, "Restrictions": {"value": ""},
                }}]}


def response(pages):
    return {"batchcomplete": True, "query": {"pages": pages}}


@pytest.fixture(autouse=True)
def reset_backoff(monkeypatch):
    monkeypatch.setattr(posters, "_NOT_BEFORE", 0.0)


def provider(pages, *, raw=None, handle=None, identities=None):
    calls = []
    def fetch(request):
        calls.append(request)
        assert request.headers["User-Agent"] == posters.USER_AGENT
        assert not request.headers.get("Authorization") and not request.headers.get("Cookie")
        if handle:
            result = handle(request, len(calls))
            if result is not None:
                return result
        if request.url.host == "commons.wikimedia.org":
            if request.url.params.get("gsrnamespace") == "14":
                categories = identities
                if categories is None:
                    categories = []
                    for item in pages:
                        match = re.fullmatch(r"File:(.+) \((\d{4})\) poster.jpg", item.get("title", ""))
                        if match:
                            categories.append({"ns": 14, "title": f"Category:{match[1]} ({match[2]} film)"})
                return httpx.Response(200, json=response(categories))
            return httpx.Response(200, json=response(pages), headers={"Set-Cookie": "unrelated=value"})
        assert request.url.host == "upload.wikimedia.org"
        return httpx.Response(200, content=raw if raw is not None else photo(), headers={"Content-Type": "image/jpeg"})
    return CommonsPosterProvider(transport=httpx.MockTransport(fetch)), calls


def test_explicit_poster_identity_returns_decoded_content_and_attribution():
    value, calls = provider([page()])
    try:
        result = value.find("Safety Last!", "2026-10-22T19:30")
        assert result.content == photo() and result.mime_type == "image/jpeg"
        assert (result.width, result.height) == (620, 1200)
        assert result.source_file == "File:Safety Last (1923) poster.jpg"
        assert result.source_page == "https://commons.wikimedia.org/wiki/File:Safety_Last_(1923)_poster.jpg"
        assert result.license_name == "Public domain" and result.license_url == result.source_page + "#Licensing"
        assert result.author == "Studio" and result.fetched_at > 0
        assert len(calls) == 3
        assert calls[0].url.params["gsrnamespace"] == "14"
        assert calls[1].url.params["gsrnamespace"] == "6"
        assert calls[1].url.params["gsrsearch"] == 'intitle:"safety last" intitle:poster'
    finally:
        value.close()
    assert value.client.is_closed and not value.availability()["available"]


@pytest.mark.parametrize("description", [
    "A film still from the 1923 film Safety Last.", "Logo of the 1923 film Safety Last.",
    "Poster for the 1923 film Safety Last Brother.", "Poster for the 1923 film Safety Last. AI generated artwork.",
    "Safety Last", "A photograph of a poster for the 1923 film Another Movie.",
])
def test_search_rank_category_or_partial_title_is_not_poster_evidence(description):
    candidate = page()
    candidate["categories"] = [{"title": "Category:Film posters"}]
    candidate["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = description
    value, calls = provider([candidate])
    try:
        assert value.find("Safety Last!") is None
        assert len(calls) == 2
    finally:
        value.close()


def test_release_year_ambiguity_is_not_resolved_using_screening_year():
    value, calls = provider([page("The Kid", 1921), page("The Kid", 2000)])
    try:
        assert value.find("The Kid", "2026-10-22T19:30") is None
        assert len(calls) == 1
        assert value.find("The Kid (1921)", "2026-10-22T19:30").source_file == "File:The Kid (1921) poster.jpg"
    finally:
        value.close()


def test_unlicensed_alternative_year_still_prevents_ambiguous_auto_selection():
    other = page("The Kid", 2000)
    other["imageinfo"][0]["extmetadata"]["LicenseShortName"]["value"] = "Fair use"
    value, _ = provider([page("The Kid", 1921), other])
    try:
        assert value.find("The Kid") is None
    finally:
        value.close()


@pytest.mark.parametrize("showtime", ["1922-10-22T19:30", "not-a-date"])
def test_invalid_or_earlier_screening_time_is_no_match(showtime):
    value, _ = provider([page()])
    try:
        assert value.find("Safety Last", showtime) is None
    finally:
        value.close()


def test_unseen_search_continuation_does_not_select_a_first_candidate():
    value, calls = provider([page()], handle=lambda request, _: httpx.Response(200, json={**response([page()]), "continue": {"gsroffset": 8}}))
    try:
        assert value.find("Safety Last") is None and len(calls) == 1
    finally:
        value.close()


@pytest.mark.parametrize("damage", ["missing-author", "missing-license", "copyrighted-pd", "restriction", "fake-license-url", "noncommercial"])
def test_unclear_license_is_rejected_before_downloading(damage):
    candidate = page()
    metadata = candidate["imageinfo"][0]["extmetadata"]
    if damage == "missing-author": metadata.pop("Artist")
    elif damage == "missing-license": metadata.pop("LicenseShortName")
    elif damage == "copyrighted-pd": metadata["Copyrighted"]["value"] = "True"
    elif damage == "restriction": metadata["Restrictions"]["value"] = "permission required"
    elif damage == "fake-license-url": metadata["LicenseUrl"] = {"value": "https://evil.test/license"}
    else: metadata["LicenseShortName"]["value"] = "CC BY-NC 4.0"
    value, calls = provider([candidate])
    try:
        assert value.find("Safety Last") is None and len(calls) == 2
    finally:
        value.close()


@pytest.mark.parametrize("name,url", [("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0"),
    ("CC BY-SA 3.0", "https://creativecommons.org/licenses/by-sa/3.0/"),
    ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/")])
def test_explicit_supported_license_is_preserved(name, url):
    candidate = page()
    metadata = candidate["imageinfo"][0]["extmetadata"]
    metadata["LicenseShortName"]["value"] = name
    metadata["LicenseUrl"] = {"value": url}
    value, _ = provider([candidate])
    try:
        result = value.find("Safety Last")
        assert result.license_name == name and result.license_url == url.rstrip("/") + "/"
        assert result.author == "Studio"
    finally:
        value.close()


@pytest.mark.parametrize("url", ["http://upload.wikimedia.org/wikipedia/commons/a/ab/Poster.jpg",
    "https://evil.test/Poster.jpg", "https://upload.wikimedia.org.evil.test/wikipedia/commons/a/ab/Poster.jpg",
    "https://user:password@upload.wikimedia.org/wikipedia/commons/a/ab/Poster.jpg",
    "https://upload.wikimedia.org:444/wikipedia/commons/a/ab/Poster.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/a/ab/..%2F..%2Fsecret",
    "https://upload.wikimedia.org/wikipedia/commons/a/ab/%252Fsecret.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/a/ab/Poster.jpg?redirect=https://evil.test",
    "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Poster.jpg/1000px-Poster.jpg"])
def test_download_url_is_strictly_allowlisted(url):
    candidate = page()
    candidate["imageinfo"][0]["url"] = url
    value, calls = provider([candidate])
    try:
        assert value.find("Safety Last") is None and len(calls) == 2
    finally:
        value.close()


def test_current_commons_tracking_query_is_not_forwarded():
    candidate = page()
    candidate["imageinfo"][0]["url"] += "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original"
    value, calls = provider([candidate])
    try:
        assert value.find("Safety Last")
        assert not calls[2].url.query
    finally:
        value.close()


@pytest.mark.parametrize("width,height", [(599, 1200), (620, 899), (1200, 620), (100, 10000), (10000, 10000)])
def test_insufficient_or_extreme_dimensions_do_not_download(width, height):
    candidate = page()
    candidate["imageinfo"][0].update(width=width, height=height)
    value, calls = provider([candidate])
    try:
        assert value.find("Safety Last") is None and len(calls) == 2
    finally:
        value.close()


@pytest.mark.parametrize("kind", ["html", "wrong-mime", "size", "dimensions", "redirect", "compressed"])
def test_response_and_decoded_image_are_checked(kind):
    candidate = page()
    def handle(request, _):
        if request.url.host != "upload.wikimedia.org": return None
        if kind == "redirect": return httpx.Response(302, headers={"Location": "https://evil.test"})
        if kind == "compressed": return httpx.Response(200, stream=httpx.ByteStream(b"not-gzip"), headers={"Content-Type": "image/jpeg", "Content-Encoding": "br"})
        raw = b"<html>not-image</html>" if kind == "html" else photo(620, 1100) if kind == "dimensions" else photo()
        if kind == "size": raw += b"x"
        return httpx.Response(200, content=raw, headers={"Content-Type": "text/html" if kind == "wrong-mime" else "image/jpeg"})
    value, calls = provider([candidate], handle=handle)
    try:
        with pytest.raises(PosterError): value.find("Safety Last")
        assert len(calls) == 3
    finally:
        value.close()


def test_stream_limit_is_enforced_without_trusting_content_length(monkeypatch):
    class Stream(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield b"x" * 65536
            yield b"x" * 65536
            pytest.fail("The oversized response must not continue reading")
        def close(self): self.closed = True
    stream = Stream()
    value, _ = provider([page()], handle=lambda request, _: httpx.Response(200, stream=stream, headers={"Content-Type": "image/jpeg"}) if request.url.host == "upload.wikimedia.org" else None)
    monkeypatch.setattr(posters, "MAX_IMAGE_BYTES", 100_000)
    try:
        with pytest.raises(PosterError, match="response_too_large"): value.find("Safety Last")
        assert stream.closed
    finally:
        value.close()


def test_decoder_bomb_limit_is_not_only_metadata_validation(monkeypatch):
    value, _ = provider([page()])
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100_000)
    try:
        with pytest.raises(PosterError, match="invalid_image"): value.find("Safety Last")
    finally:
        value.close()


@pytest.mark.parametrize("stop", [False, True], ids=["deadline", "cancel"])
def test_small_drip_chunks_reach_deadline_or_cancel_without_aggregation(monkeypatch, stop):
    now, stopping = [0.0], [False]
    monkeypatch.setattr(posters, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep, time_ns=time.time_ns))
    class Drip(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield b"x"
            if stop: stopping[0] = True
            else: now[0] = posters.TOTAL_SECONDS + 1
            yield b"y"
            pytest.fail("The second small chunk must trigger the deadline/cancel check")
        def close(self): self.closed = True
    stream = Drip()
    value, _ = provider([page()], handle=lambda request, _: httpx.Response(200, stream=stream, headers={"Content-Type": "image/jpeg"}) if request.url.host == "upload.wikimedia.org" else None)
    try:
        with pytest.raises(PosterError, match="cancelled" if stop else "deadline_exceeded"):
            value.find("Safety Last", stopping=lambda: stopping[0])
        assert stream.closed
    finally:
        value.close()


def test_long_retry_after_stops_without_repeated_requests():
    value, calls = provider([], handle=lambda *_: httpx.Response(429, headers={"Retry-After": "60"}))
    try:
        with pytest.raises(PosterError) as error: value.find("Safety Last")
        assert error.value.retry_after == 60 and len(calls) == 1
        with pytest.raises(PosterError, match="rate_limited"): value.find("Safety Last")
        assert len(calls) == 1
    finally:
        value.close()


def test_zero_retry_after_allows_one_retry_then_succeeds():
    value, calls = provider([page()], handle=lambda _, count: httpx.Response(429, headers={"Retry-After": "0"}) if count == 1 else None)
    try:
        assert value.find("Safety Last") and len(calls) == 4
    finally:
        value.close()


def test_cancelled_lookup_does_not_issue_network_requests():
    value, calls = provider([page()])
    try:
        with pytest.raises(PosterError, match="cancelled"): value.find("Safety Last", stopping=lambda: True)
        assert calls == []
    finally:
        value.close()


def test_persisted_source_requires_canonical_links_and_licence_pair():
    source = {"provider": "wikimedia_commons", "sourcePage": "https://commons.wikimedia.org/wiki/File:Safety_Last_(1923)_poster.jpg",
              "sourceFile": "File:Safety Last (1923) poster.jpg", "licenseName": "Public domain",
              "licenseUrl": "https://commons.wikimedia.org/wiki/File:Safety_Last_(1923)_poster.jpg#Licensing",
              "author": "Studio", "fetchedAt": 1}
    assert posters.validate_source(source)
    for changes in ({"sourcePage": "https://evil.test/File:poster"}, {"sourceFile": "File:Other.jpg"},
                    {"licenseName": "CC BY 4.0"}, {"licenseUrl": "javascript:alert(1)"},
                    {"author": ""}, {"fetchedAt": True}, {"unexpected": "field"}):
        assert not posters.validate_source({**source, **changes})


def test_hidden_duplicate_attribution_and_scripts_are_not_displayed():
    assert posters._plain('Unknown author<span style="display: none;">Unknown author</span><script>bad()</script>') == "Unknown author"


def test_one_free_poster_does_not_hide_another_same_named_film_identity():
    identities = [{"ns": 14, "title": f"Category:The Kid ({year} film)"} for year in (1921, 2000)]
    value, calls = provider([page("The Kid", 1921)], identities=identities)
    try:
        assert value.find("The Kid", "2026-10-22T19:30") is None
        assert len(calls) == 1  # Do not even fetch poster metadata for ambiguous films.
        assert value.find("The Kid (1921)").source_file == "File:The Kid (1921) poster.jpg"
    finally:
        value.close()


@pytest.mark.parametrize("requested", ["Example", "Example (2000)"])
def test_same_year_country_qualified_films_are_not_collapsed_into_one(requested):
    identities = [{"ns": 14, "title": "Category:Example (2000 film)"},
                  {"ns": 14, "title": "Category:Example (2000 Indian film)"}]
    first, second = page("Example", 2000), page("Example", 2000)
    first["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = "Poster for the 2000 American film Example."
    second["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = "Poster for the 2000 Indian film Example."
    value, calls = provider([first, second], identities=identities)
    try:
        assert value.find(requested) is None and len(calls) == 1
    finally:
        value.close()


def test_conflicting_same_year_poster_descriptions_are_not_resolved_by_resolution():
    first, second = page("Example", 2000), page("Example", 2000)
    first["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = "Poster for the 2000 American film Example."
    second["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = "Poster for the 2000 Indian film Example."
    value, calls = provider([first, second])
    try:
        assert value.find("Example (2000)") is None and len(calls) == 2
    finally:
        value.close()


@pytest.mark.parametrize("identities", [[], [{"ns": 14, "title": "Category:Safety Last!"}],
    [{"ns": 14, "title": "Category:Safety Last!", "categories": [{"title": "Category:1923 films"}, {"title": "Category:2000 films"}]}],
    [{"ns": 14, "title": "Category:Safety Last (1923 film)", "categories": [{"title": "Category:2000 films"}]}]])
def test_unknown_or_inconsistent_category_identity_is_no_match(identities):
    value, calls = provider([page()], identities=identities)
    try:
        assert value.find("Safety Last") is None and len(calls) == 1
    finally:
        value.close()


def test_bare_film_category_needs_explicit_release_year_parent():
    identities = [{"ns": 14, "title": "Category:Safety Last!", "categories": [{"title": "Category:1923 films of the United States"}]},
                  {"ns": 14, "title": "Category:Safety Last! (film screenshots)"}]
    value, _ = provider([page()], identities=identities)
    try:
        assert value.find("Safety Last!")
        assert value.find("Safety Last! (2000)") is None
    finally:
        value.close()


def test_explicit_release_year_can_refine_truncated_category_search():
    def handle(request, _):
        if request.url.params.get("gsrnamespace") == "14" and "intitle:1921" not in request.url.params.get("gsrsearch", ""):
            return httpx.Response(200, json={"continue": {"gsroffset": 12}})
        return None
    value, calls = provider([page("The Kid", 1921)], handle=handle)
    try:
        assert value.find("The Kid (1921)")
        assert len(calls) == 4
        assert "intitle:1921" in calls[1].url.params["gsrsearch"]
    finally:
        value.close()


def test_latest_image_history_continuation_is_not_an_unseen_film_candidate():
    def handle(request, _):
        if request.url.params.get("gsrnamespace") == "6":
            return httpx.Response(200, json={**response([page()]), "continue": {"iistart": "old-version", "continue": "||"}})
        return None
    value, calls = provider([page()], handle=handle)
    try:
        assert value.find("Safety Last") and len(calls) == 3
        assert calls[1].url.params["iilimit"] == "1"
    finally:
        value.close()


def test_global_network_slot_serializes_distinct_providers():
    entered, release = threading.Event(), threading.Event()
    def wait(request, _):
        if request.url.host == "commons.wikimedia.org":
            entered.set()
            assert release.wait(2)
        return None
    first, _ = provider([], handle=wait)
    second, calls = provider([])
    errors = []
    def run(value):
        try: value.find("Safety Last")
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=run, args=(first,))
    other = threading.Thread(target=run, args=(second,))
    try:
        thread.start(); assert entered.wait(1)
        other.start(); time.sleep(0.05)
        assert calls == []
        release.set(); thread.join(2); other.join(2)
        assert not thread.is_alive() and not other.is_alive() and not errors
    finally:
        release.set(); thread.join(2); other.join(2)
        first.close(); second.close()
