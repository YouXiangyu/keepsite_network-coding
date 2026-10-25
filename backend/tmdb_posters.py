"""Server-only TMDB movie lookup with exact identity and latest-release selection."""
from datetime import date, datetime
import io
import json
import os
import re
import threading
import time
import unicodedata
import warnings

import httpx
from fastapi import HTTPException
from PIL import Image

from .media import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, image_info
from .movie_titles import clean_movie_title
from .posters import (CommonsPosterProvider, MAX_METADATA_BYTES, NETWORK_SECONDS,
                      PosterCandidate, PosterError, TOTAL_SECONDS, USER_AGENT,
                      _dimensions, _normal, _retry_delay, _title)

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/"
TERMS_URL = "https://www.themoviedb.org/api-terms-of-use"
POSTER_PATH = re.compile(r"/[A-Za-z0-9]{1,200}\.(?:jpg|png|webp)")
MAX_PAGES = 5
MAX_ALIAS_MOVIES = 8
_NETWORK_SLOT = threading.Lock()
_NOT_BEFORE = 0.0


def configured_provider():
    provider = os.environ.get("KEEPSITE_POSTER_PROVIDER", "commons")
    if provider == "commons":
        return CommonsPosterProvider()
    if provider == "tmdb":
        return TMDBPosterProvider(access_token=os.environ.get("KEEPSITE_TMDB_READ_ACCESS_TOKEN", ""))
    raise ValueError("Unsupported poster provider configuration")


class TMDBPosterProvider:
    def __init__(self, *, access_token="", transport=None):
        # Authentication is an API-only header; URLs and the image CDN contain
        # no credential, including when httpx request logging is enabled.
        self.access_token = access_token if isinstance(access_token, str) and re.fullmatch(r"[A-Za-z0-9_.-]{32,4096}", access_token) else ""
        self.client = httpx.Client(transport=transport, timeout=httpx.Timeout(NETWORK_SECONDS),
                                  follow_redirects=False, trust_env=False,
                                  headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        self.closed = False

    def availability(self):
        if self.closed:
            return {"available": False, "reason": "海报服务已关闭。"}
        if not self.access_token:
            return {"available": False, "reason": "TMDB 海报服务尚未配置有效的服务端 API 读取令牌。"}
        return {"available": True, "reason": None}

    def close(self):
        if not self.closed:
            self.closed = True
            self.client.close()
            self.access_token = ""

    def _check(self, deadline, stopping):
        if self.closed or stopping():
            raise PosterError("cancelled")
        if time.monotonic() >= deadline:
            raise PosterError("deadline_exceeded")

    def _fetch(self, path, *, params=None, image=False, deadline, stopping):
        global _NOT_BEFORE
        if not self.access_token:
            raise PosterError("not_configured")
        if image:
            if not isinstance(path, str) or not re.fullmatch(r"w780/[A-Za-z0-9]{1,200}\.(?:jpg|png|webp)", path):
                raise PosterError("unapproved_url")
            url, limit = IMAGE_BASE + path, MAX_IMAGE_BYTES
        else:
            if path not in ("/search/movie", "/configuration") and not re.fullmatch(r"/movie/[1-9][0-9]{0,9}/alternative_titles", path):
                raise PosterError("unapproved_url")
            url, limit = API_BASE + path, MAX_METADATA_BYTES
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
                headers = {"Accept": "image/jpeg, image/png, image/webp" if image else "application/json"}
                if not image:
                    headers["Authorization"] = "Bearer " + self.access_token
                with self.client.stream("GET", url, params=params, headers=headers) as response:
                    if response.status_code == 404 and image:
                        return None
                    if response.status_code == 429:
                        delay = _retry_delay(response.headers.get("Retry-After"))
                        _NOT_BEFORE = time.monotonic() + delay
                    elif response.status_code in (502, 503, 504):
                        delay = 1.0
                    elif response.status_code != 200:
                        raise PosterError("source_rejected")
                    else:
                        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        allowed = {"image/jpeg", "image/png", "image/webp"} if image else {"application/json"}
                        if mime not in allowed or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise PosterError("unexpected_content_type")
                        size = response.headers.get("Content-Length")
                        if size is not None and (not size.isdigit() or int(size) > limit):
                            raise PosterError("response_too_large")
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            self._check(deadline, stopping)
                            if len(body) + len(chunk) > limit:
                                raise PosterError("response_too_large")
                            body.extend(chunk)
                        self._check(deadline, stopping)
                        return bytes(body), mime
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

    def _query(self, path, params, deadline, stopping):
        raw, _ = self._fetch(path, params=params, deadline=deadline, stopping=stopping)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("success") is False:
                raise ValueError
            return result
        except (ValueError, TypeError, RecursionError):
            raise PosterError("invalid_metadata") from None

    def _movies(self, query, deadline, stopping):
        movies, pages, total = {}, 1, None
        for page in range(1, MAX_PAGES + 1):
            result = self._query("/search/movie", {"query": query, "language": "zh-CN", "include_adult": "false", "page": page}, deadline, stopping)
            entries = result.get("results")
            count, new_total = result.get("total_pages"), result.get("total_results")
            if (not isinstance(entries, list) or len(entries) > 20 or type(count) is not int or count < 0
                    or type(new_total) is not int or new_total < 0 or type(result.get("page")) is not int
                    or result["page"] != page or count == 0 and (new_total != 0 or entries)):
                raise PosterError("invalid_metadata")
            if page == 1:
                pages, total = count, new_total
                if pages > MAX_PAGES or total > 20 * MAX_PAGES:
                    return None
            elif pages != count or total != new_total:
                return None  # Results changed during pagination: no completeness claim.
            for movie in entries:
                if not isinstance(movie, dict) or type(movie.get("id")) is not int or not 0 < movie["id"] <= 9_999_999_999:
                    raise PosterError("invalid_metadata")
                if movie["id"] in movies:
                    return None
                movies[movie["id"]] = movie
            if page >= pages:
                return list(movies.values()) if len(movies) == total else None
        return None

    def find(self, title, showtime="", *, stopping=lambda: False):
        if isinstance(title, str):
            title = unicodedata.normalize("NFKC", title)
            if len(title) > 120:
                return None
            title = clean_movie_title(title)
        normalized, requested_year = _title(title)
        if not normalized:
            return None
        self._check(time.monotonic() + TOTAL_SECONDS, stopping)
        # Keep the user's punctuation for the API; use normalized punctuation
        # only for evidence comparison (e.g. Chinese ： and English :).
        query = re.sub(r"\s*\(((?:18|19|20)\d{2})(?:\s+film)?\)\s*$", "", title).strip()
        deadline = time.monotonic() + TOTAL_SECONDS
        movies = self._movies(query, deadline, stopping)
        if movies is None:
            return None
        candidates = [movie for movie in movies if movie.get("adult") is False]
        identities = [movie for movie in candidates if any(
            isinstance(name, str) and _normal(name) == normalized
            for name in (movie.get("title"), movie.get("original_title")))]
        # Exact title/original-title matches take precedence over aliases. Only
        # use aliases when there are no exact matches, with a complete budget.
        if not identities:
            if len(candidates) > MAX_ALIAS_MOVIES:
                return None
            for movie in candidates:
                aliases = self._query(f"/movie/{movie['id']}/alternative_titles", {}, deadline, stopping)
                if aliases.get("id") != movie["id"] or not isinstance(aliases.get("titles"), list) or len(aliases["titles"]) > 300:
                    raise PosterError("invalid_metadata")
                if any(isinstance(item, dict) and isinstance(item.get("title"), str)
                       and _normal(item["title"]) == normalized for item in aliases["titles"]):
                    identities.append(movie)
        screening_year = None
        if showtime:
            try:
                screening_year = datetime.strptime(showtime, "%Y-%m-%dT%H:%M").year
            except (ValueError, TypeError):
                return None
        matches = []
        for movie in identities:
            release = movie.get("release_date")
            try:
                if not isinstance(release, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release):
                    return None
                released = date.fromisoformat(release)
            except ValueError:
                return None
            if requested_year is not None and released.year != requested_year:
                continue
            if screening_year is not None and released.year > screening_year:
                continue
            matches.append((movie, released))
        if not matches:
            return None
        newest = max(released for _, released in matches)
        latest = [movie for movie, released in matches if released == newest]
        if len(latest) != 1:
            return None
        movie = latest[0]
        path = movie.get("poster_path")
        if not isinstance(path, str) or POSTER_PATH.fullmatch(path) is None:
            return None
        configuration = self._query("/configuration", {}, deadline, stopping)
        images = configuration.get("images")
        if (not isinstance(images, dict) or images.get("secure_base_url") != IMAGE_BASE
                or not isinstance(images.get("poster_sizes"), list) or "w780" not in images["poster_sizes"]):
            raise PosterError("invalid_configuration")
        downloaded = self._fetch("w780" + path, image=True, deadline=deadline, stopping=stopping)
        if downloaded is None:
            return None
        raw, mime = downloaded
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                        raise ValueError
            info = image_info(raw)
            if info["mimeType"] != mime or not _dimensions(info["width"], info["height"]):
                raise ValueError
        except (HTTPException, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise PosterError("invalid_image") from None
        self._check(deadline, stopping)
        return PosterCandidate(raw, mime, f"https://www.themoviedb.org/movie/{movie['id']}", "", "", "",
                               time.time_ns() // 1_000_000, info["width"], info["height"], path, provider="tmdb")
