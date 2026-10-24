"""Conservative, anonymous Commons poster lookup with bounded network access.

One explicit film identity, an actual poster description and a reusable licence
are required. Missing evidence is a no-match; a search rank is never evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import io
import json
import math
import re
import threading
import time
import unicodedata
import warnings
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException
from PIL import Image

from .media import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, image_info

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "keepsite/0.6 (https://keepsite.space)"
MAX_CANDIDATES = 8
MAX_IDENTITIES = 12
MAX_METADATA_BYTES = 1024 * 1024
MIN_WIDTH, MIN_HEIGHT = 600, 900
TOTAL_SECONDS = 15
NETWORK_SECONDS = 3
_NETWORK_SLOT = threading.Lock()
_NOT_BEFORE = 0.0
_MIMES = {"image/jpeg", "image/png", "image/webp"}
_EXCLUDED = re.compile(r"\b(?:lobby cards?|screenshots?|film stills?|logos?|fan art|fan made|ai generated|collage|montage|trading cards?)\b")


class PosterError(RuntimeError):
    """Safe error codes only: never log provider bodies or signed/private data."""

    def __init__(self, code, *, retry_after=0):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


@dataclass(frozen=True)
class PosterCandidate:
    content: bytes
    mime_type: str
    source_page: str
    license_name: str
    license_url: str
    author: str
    fetched_at: int
    width: int
    height: int
    source_file: str
    provider: str = "wikimedia_commons"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        blocked = (bool(self.stack and self.stack[-1][1]) or tag in {"script", "style"}
                   or "hidden" in attributes or attributes.get("aria-hidden") == "true"
                   or bool(re.search(r"display\s*:\s*none", attributes.get("style") or "", re.I)))
        if tag not in {"br", "hr", "img", "input", "meta", "link", "wbr"}:
            self.stack.append((tag, blocked))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


def _plain(value, maximum=10_000):
    if not isinstance(value, str) or len(value) > maximum:
        return ""
    parser = _Text()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def _normal(value):
    return " ".join("".join(char if char.isalnum() else " " for char in unicodedata.normalize("NFKC", value).casefold()).split())


def _field(metadata, key):
    item = metadata.get(key)
    return item.get("value", "") if isinstance(item, dict) else ""


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        return "", None
    match = re.fullmatch(r"(.+?)\s*\(((?:18|19|20)\d{2})(?:\s+film)?\)\s*", value)
    return (_normal(match[1]), int(match[2])) if match else (_normal(value), None)


def _film_identity(description, title):
    text = _normal(description)
    if _EXCLUDED.search(text):
        return None
    # Full-title boundary prevents e.g. The Kid matching The Kid Brother. These
    # two deliberately narrow English descriptions are backed by live samples.
    before = re.search(r"\bposter (?:for|of|from) .{0,100}?\b((?:18|19|20)\d{2}) ((?:[a-z]+ ){0,4})(?:film|movie) (.+)$", text)
    if before and re.match(re.escape(title) + r"(?:$| (?:starring|directed|featuring|released)\b)", before[3]):
        return int(before[1]), before[2].strip()
    after = re.search(r"\bposter (?:for|of|from) ((?:[a-z]+ ){0,4})(?:film|movie) " + re.escape(title)
                      + r" ((?:18|19|20)\d{2})(?:$| (?:starring|directed|featuring|released)\b)", text)
    return (int(after[2]), re.sub(r"^(?:the|an?|original) ", "", after[1].strip())) if after else None


def _source_page(filename):
    if (not isinstance(filename, str) or not filename.startswith("File:") or len(filename) > 300
            or any(ord(char) < 32 for char in filename) or any(char in filename for char in "\\/<>[]{}|")):
        return None
    return "https://commons.wikimedia.org/wiki/" + quote(filename.replace(" ", "_"), safe=":()!,-._")


def _image_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org" or parsed.username
                or parsed.password or parsed.port is not None or parsed.fragment
                or not re.fullmatch(r"/wikipedia/commons/[a-f0-9]/[a-f0-9]{2}/[^/]+", parsed.path)):
            return None
        decoded = unquote(parsed.path.rsplit("/", 1)[-1], errors="strict")
        if not decoded or decoded in {".", ".."} or any(char in decoded for char in "/\\%") or any(ord(char) < 32 for char in decoded):
            return None
        # Current Commons imageinfo appends these public tracking parameters.
        # They are not object identity and are not forwarded to the download.
        allowed = {"utm_source": "commons.wikimedia.org", "utm_campaign": "imageinfo", "utm_content": "original"}
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len(dict(pairs)) != len(pairs) or any(allowed.get(key) != item for key, item in pairs):
            return None
        return urlunsplit(("https", "upload.wikimedia.org", parsed.path, "", ""))
    except (ValueError, UnicodeError):
        return None


def _license(metadata, page):
    name = _plain(_field(metadata, "LicenseShortName"), 100)
    url = _plain(_field(metadata, "LicenseUrl"), 500)
    author = _plain(_field(metadata, "Artist"), 1000)
    if not author or _plain(_field(metadata, "Restrictions"), 1000):
        return None
    if name == "Public domain":
        if str(_field(metadata, "Copyrighted")).casefold() != "false" or _field(metadata, "License") != "pd":
            return None
        if url and url.rstrip("/") != "https://creativecommons.org/publicdomain/mark/1.0":
            return None
        return name, "https://creativecommons.org/publicdomain/mark/1.0/" if url else page + "#Licensing", author
    if name == "CC0 1.0":
        expected = "https://creativecommons.org/publicdomain/zero/1.0/"
    else:
        match = re.fullmatch(r"CC (BY|BY-SA) (2\.0|2\.5|3\.0|4\.0)", name)
        if not match or "unknown" in author.casefold():
            return None
        expected = f"https://creativecommons.org/licenses/{match[1].lower()}/{match[2]}/"
    if url.rstrip("/") != expected.rstrip("/"):
        return None
    return name, expected, author


def validate_source(source):
    """Validate the persisted attribution contract, not a new licence grant."""
    if isinstance(source, dict) and source.get("provider") == "tmdb":
        return (set(source) == {"provider", "sourcePage", "sourceFile", "termsUrl", "fetchedAt"}
                and isinstance(source.get("sourcePage"), str)
                and re.fullmatch(r"https://www\.themoviedb\.org/movie/[1-9][0-9]{0,9}", source["sourcePage"]) is not None
                and isinstance(source.get("sourceFile"), str)
                and re.fullmatch(r"/[A-Za-z0-9]{1,200}\.(?:jpg|png|webp)", source["sourceFile"]) is not None
                and source.get("termsUrl") == "https://www.themoviedb.org/api-terms-of-use"
                and type(source.get("fetchedAt")) is int and 0 <= source["fetchedAt"] <= 2**53 - 1)
    keys = {"provider", "sourcePage", "sourceFile", "licenseName", "licenseUrl", "author", "fetchedAt"}
    if (not isinstance(source, dict) or set(source) != keys or source.get("provider") != "wikimedia_commons"
            or not isinstance(source.get("author"), str) or not 1 <= len(source["author"]) <= 1000
            or any(ord(char) < 32 for char in source["author"])
            or type(source.get("fetchedAt")) is not int or not 0 <= source["fetchedAt"] <= 2**53 - 1):
        return False
    page = _source_page(source.get("sourceFile"))
    if page is None or source.get("sourcePage") != page:
        return False
    name, url = source.get("licenseName"), source.get("licenseUrl")
    if name == "Public domain":
        return url in (page + "#Licensing", "https://creativecommons.org/publicdomain/mark/1.0/")
    if name == "CC0 1.0":
        return url == "https://creativecommons.org/publicdomain/zero/1.0/"
    if not isinstance(name, str):
        return False
    match = re.fullmatch(r"CC (BY|BY-SA) (2\.0|2\.5|3\.0|4\.0)", name)
    return bool(match and url == f"https://creativecommons.org/licenses/{match[1].lower()}/{match[2]}/")


def _dimensions(width, height):
    return (type(width) is int and type(height) is int and width >= MIN_WIDTH and height >= MIN_HEIGHT
            and width * height <= MAX_IMAGE_PIXELS and 0.4 <= width / height <= 0.9)


def _retry_delay(value):
    if value and value.isdigit():
        delay = float(value)
        if not math.isfinite(delay):
            raise PosterError("invalid_retry_after")
        return delay
    if value:
        try:
            stamp = parsedate_to_datetime(value)
            return max(0.0, (stamp - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 1.0


class CommonsPosterProvider:
    def __init__(self, *, transport=None):
        self.client = httpx.Client(transport=transport, timeout=httpx.Timeout(NETWORK_SECONDS),
                                  follow_redirects=False, trust_env=False,
                                  headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        self.closed = False

    def availability(self):
        return {"available": not self.closed, "reason": None if not self.closed else "海报服务已关闭。"}

    def close(self):
        if not self.closed:
            self.closed = True
            self.client.close()

    def _check(self, deadline, stopping):
        if self.closed or stopping():
            raise PosterError("cancelled")
        if time.monotonic() >= deadline:
            raise PosterError("deadline_exceeded")

    def _fetch(self, url, *, params=None, limit, mime, deadline, stopping):
        global _NOT_BEFORE
        if url != API_URL and _image_url(url) != url:
            raise PosterError("unapproved_url")
        for attempt in range(2):
            self._check(deadline, stopping)
            while not _NETWORK_SLOT.acquire(timeout=0.05):
                self._check(deadline, stopping)
            delay = 0.0
            try:
                self._check(deadline, stopping)
                if time.monotonic() < _NOT_BEFORE:
                    raise PosterError("rate_limited", retry_after=_NOT_BEFORE - time.monotonic())
                self.client.cookies.clear()
                with self.client.stream("GET", url, params=params, headers={"Accept": mime}) as response:
                    if response.status_code == 404 and url != API_URL:
                        return None
                    if response.status_code == 429:
                        delay = _retry_delay(response.headers.get("Retry-After"))
                        _NOT_BEFORE = time.monotonic() + delay
                    elif response.status_code in (502, 503, 504):
                        delay = 1.0
                    elif response.status_code != 200:
                        raise PosterError("source_rejected")
                    else:
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if content_type != mime or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise PosterError("unexpected_content_type")
                        length = response.headers.get("Content-Length")
                        if length is not None and (not length.isdigit() or int(length) > limit):
                            raise PosterError("response_too_large")
                        body = bytearray()
                        # Do not aggregate to a target chunk size: a slow stream
                        # must reach the deadline/stop check after each read.
                        for chunk in response.iter_bytes():
                            self._check(deadline, stopping)
                            if len(body) + len(chunk) > limit:
                                raise PosterError("response_too_large")
                            body.extend(chunk)
                        self._check(deadline, stopping)
                        return bytes(body)
            except httpx.HTTPError:
                if attempt:
                    raise PosterError("source_unavailable") from None
                delay = 1.0
            finally:
                self.client.cookies.clear()
                _NETWORK_SLOT.release()
            if attempt or delay > 5 or time.monotonic() + delay >= deadline:
                raise PosterError("source_unavailable", retry_after=delay)
            until = time.monotonic() + delay
            while time.monotonic() < until:
                self._check(deadline, stopping)
                time.sleep(min(0.05, max(0, until - time.monotonic())))
        raise PosterError("source_unavailable")

    def _query(self, params, deadline, stopping, maximum):
        raw = self._fetch(API_URL, params=params, limit=MAX_METADATA_BYTES, mime="application/json", deadline=deadline, stopping=stopping)
        try:
            response = json.loads(raw)
            if not isinstance(response, dict) or "error" in response:
                raise ValueError
            continuation = response.get("continue")
            if continuation is not None:
                # Image upload-history continuation is not another search
                # candidate. Only the latest version was requested. Unseen
                # search results or category parents still make identity unsafe.
                if (not isinstance(continuation, dict) or params.get("prop") != "imageinfo"
                        or set(continuation) - {"continue", "iistart", "iicontinue"}):
                    return None
            pages = response.get("query", {}).get("pages", [])
            if not isinstance(pages, list) or len(pages) > maximum:
                raise ValueError
            return pages
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PosterError("invalid_metadata") from None

    def _identity(self, title, requested_year, deadline, stopping):
        params = {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                  "gsrsearch": f'intitle:"{title}"', "gsrnamespace": 14, "gsrlimit": MAX_IDENTITIES,
                  "prop": "categories", "cllimit": 50}
        pages = self._query(params, deadline, stopping, MAX_IDENTITIES)
        if pages is None and requested_year is not None:
            # A supplied release year can narrow a truncated category search;
            # the screening date is deliberately never used for this query.
            params["gsrsearch"] += f" intitle:{requested_year}"
            pages = self._query(params, deadline, stopping, MAX_IDENTITIES)
        if pages is None:
            return None
        identities = {}
        for page in pages:
            if (not isinstance(page, dict) or page.get("ns") != 14 or not isinstance(page.get("title"), str)
                    or not page["title"].startswith("Category:")):
                continue
            raw_name = page["title"].removeprefix("Category:")
            name = _normal(raw_name)
            qualified = re.fullmatch(r"(.+?)\s*\(([^()]*)\)", raw_name)
            if qualified and _normal(qualified[1]) == title:
                qualifier = _normal(qualified[2])
                if _EXCLUDED.search(qualifier):
                    continue  # A film's screenshots/lobby-card category is not a film identity.
                if "film" in qualifier.split() and not re.fullmatch(r"(?:(?:18|19|20)\d{2} )?film", qualifier):
                    year = re.match(r"((?:18|19|20)\d{2})\b", qualifier)
                    if requested_year is not None and year and int(year[1]) != requested_year:
                        continue
                    return None
            explicit = re.fullmatch(re.escape(title) + r" ((?:18|19|20)\d{2}) film", name)
            if not explicit and name not in {title, title + " film"}:
                continue
            parents = page.get("categories", [])
            if not isinstance(parents, list):
                return None
            parent_years = set()
            for parent in parents:
                if isinstance(parent, dict) and isinstance(parent.get("title"), str):
                    match = re.match(r"Category:((?:18|19|20)\d{2}) films(?:$| )", parent["title"])
                    if match:
                        parent_years.add(int(match[1]))
            if explicit:
                year = int(explicit[1])
                if parent_years and parent_years != {year}:
                    return None
                if requested_year is None or year == requested_year:
                    identities[name] = year
            elif len(parent_years) == 1:
                year = next(iter(parent_years))
                if requested_year is None or year == requested_year:
                    identities[name] = year
            else:
                return None
        # Separate matching category identities are not merged just because
        # their years agree (e.g. two countries releasing same-named films).
        return next(iter(identities.values())) if len(identities) == 1 else None

    def find(self, title, showtime="", *, stopping=lambda: False):
        title, requested_year = _title(title)
        if not title:
            return None
        deadline = time.monotonic() + TOTAL_SECONDS
        identity_year = self._identity(title, requested_year, deadline, stopping)
        if identity_year is None:
            return None
        search = f'intitle:"{title}" intitle:poster'
        if requested_year is not None:
            search += f" intitle:{requested_year}"
        params = {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                  "gsrsearch": search, "gsrnamespace": 6, "gsrlimit": MAX_CANDIDATES,
                  "prop": "imageinfo", "iilimit": 1, "iiprop": "url|size|mime|extmetadata", "iiextmetadatalanguage": "en",
                  "iiextmetadatafilter": "ImageDescription|Artist|LicenseShortName|LicenseUrl|Copyrighted|Restrictions|License"}
        pages = self._query(params, deadline, stopping, MAX_CANDIDATES)
        if pages is None:
            return None
        candidates, identities = [], set()
        for page in pages:
            self._check(deadline, stopping)
            if not isinstance(page, dict) or page.get("ns") != 6 or page.get("imagerepository") != "local":
                continue
            source = _source_page(page.get("title"))
            infos = page.get("imageinfo")
            if not source or not isinstance(infos, list) or len(infos) != 1 or not isinstance(infos[0], dict):
                continue
            info = infos[0]
            metadata = info.get("extmetadata")
            if not isinstance(metadata, dict) or _EXCLUDED.search(_normal(page["title"])):
                continue
            identity = _film_identity(_plain(_field(metadata, "ImageDescription")), title)
            if identity is None:
                continue
            year = identity[0]
            if year != identity_year:
                continue
            identities.add(identity)
            rights = _license(metadata, source)
            url = _image_url(info.get("url"))
            if (not rights or not url or info.get("mime") not in _MIMES or type(info.get("size")) is not int
                    or not 0 < info["size"] <= MAX_IMAGE_BYTES or not _dimensions(info.get("width"), info.get("height"))):
                continue
            candidates.append((info, page["title"], source, rights, year, url))
        if len(identities) != 1:
            return None
        if not candidates:
            return None
        # Screening dates only reject impossible future candidates; they never
        # select a release year or decide between remakes/reissues.
        if showtime:
            try:
                screening_year = datetime.strptime(showtime, "%Y-%m-%dT%H:%M").year
            except (TypeError, ValueError):
                return None
            candidates = [candidate for candidate in candidates if candidate[4] <= screening_year]
            if not candidates:
                return None
        info, filename, source, rights, _year, url = max(candidates, key=lambda item: (item[0]["width"] * item[0]["height"], item[1]))
        content = self._fetch(url, limit=MAX_IMAGE_BYTES, mime=info["mime"], deadline=deadline, stopping=stopping)
        if content is None:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                        raise ValueError
            decoded = image_info(content)
            if (decoded["mimeType"] != info["mime"] or decoded["size"] != info["size"]
                    or (decoded["width"], decoded["height"]) != (info["width"], info["height"])
                    or not _dimensions(decoded["width"], decoded["height"])):
                raise ValueError
        except (HTTPException, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise PosterError("invalid_image") from None
        self._check(deadline, stopping)
        return PosterCandidate(content, decoded["mimeType"], source, rights[0], rights[1], rights[2],
                               time.time_ns() // 1_000_000, decoded["width"], decoded["height"], filename)
