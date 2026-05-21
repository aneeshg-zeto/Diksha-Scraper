"""DIKSHA REST API client.

Wraps the two main endpoints used by the scraper:
  - POST /api/content/v1/search   — discover textbooks
  - GET  /api/content/v1/read/{id} — fetch full content metadata
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import ScraperConfig
from .logger import get_logger
from .models import ContentItem

log = get_logger(__name__)

# Maximum items per page the DIKSHA API will return
_PAGE_SIZE = 100


class DIKSHAApiClient:
    """Thin wrapper around the DIKSHA content search and read APIs."""

    def __init__(self, session: requests.Session, config: ScraperConfig) -> None:
        self._session = session
        self._config = config

    # ── Search ─────────────────────────────────────────────────────────────

    def search_content(
        self,
        filters: Dict[str, Any],
        limit: int = _PAGE_SIZE,
        offset: int = 0,
        _accumulated: Optional[List[ContentItem]] = None,
    ) -> List[ContentItem]:
        """Search for digital textbooks matching *filters*.

        Handles pagination automatically: keeps calling the API with an
        increasing offset until all matching items have been fetched.

        Parameters
        ----------
        filters:
            Dict with keys ``board``, ``medium``, ``gradeLevel``, ``subject``,
            and optionally ``contentType`` / ``primaryCategory``.
        limit:
            Page size (max 100 per DIKSHA API limits).
        offset:
            Starting index for this page.
        _accumulated:
            Internal — used for recursive pagination accumulation.
        """
        if _accumulated is None:
            _accumulated = []

        payload = self._build_search_payload(filters, limit, offset)

        log.debug(
            "search_content: board=%s medium=%s grade=%s subject=%s offset=%d",
            filters.get("board"),
            filters.get("medium"),
            filters.get("gradeLevel"),
            filters.get("subject"),
            offset,
        )

        try:
            response_data = self._post(self._config.search_url, payload)
        except requests.RequestException as exc:
            log.error("Search API request failed: %s", exc)
            return _accumulated

        result = response_data.get("result", {})
        content_list: List[Dict[str, Any]] = result.get("content", []) or []
        total_count: int = result.get("count", 0)

        for raw_item in content_list:
            item = self._parse_content_item(raw_item)
            if item:
                _accumulated.append(item)

        fetched_so_far = offset + len(content_list)
        log.debug(
            "Fetched %d / %d items for %s/%s/%s/%s",
            fetched_so_far,
            total_count,
            filters.get("board"),
            filters.get("medium"),
            filters.get("gradeLevel"),
            filters.get("subject"),
        )

        # Polite delay between paginated requests
        if fetched_so_far < total_count and len(content_list) == limit:
            time.sleep(self._config.scraper.request_delay_seconds)
            return self.search_content(
                filters,
                limit=limit,
                offset=fetched_so_far,
                _accumulated=_accumulated,
            )

        return _accumulated

    # ── Read ───────────────────────────────────────────────────────────────

    def get_content_detail(self, content_id: str) -> Optional[Dict[str, Any]]:
        """Fetch full metadata for a single content item."""
        url = (
            self._config.diksha.api_base_url
            + self._config.diksha.read_endpoint
            + f"/{content_id}"
        )
        try:
            data = self._get(url)
            return data.get("result", {}).get("content")
        except requests.RequestException as exc:
            log.error("Read API request failed for %s: %s", content_id, exc)
            return None

    # ── Download URL resolution ────────────────────────────────────────────

    def get_download_url(self, content_item: ContentItem) -> Optional[str]:
        """Resolve the best available download URL for *content_item*.

        Priority order:
        1. ``variants`` dict — highest quality variant (largest size).
        2. ``download_url_map`` dict — keyed by format (pdf, epub, …).
        3. ``download_url`` plain string.
        """
        preferred = self._config.download.preferred_format.lower()

        # 1. Variants (quality tiers)
        if content_item.variants:
            url = self._pick_from_variants(content_item.variants, preferred)
            if url:
                return url

        # 2. downloadUrl as dict
        if content_item.download_url_map:
            url = self._pick_from_map(content_item.download_url_map, preferred)
            if url:
                return url

        # 3. Plain string downloadUrl
        if content_item.download_url:
            return content_item.download_url

        return None

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _build_search_payload(
        filters: Dict[str, Any], limit: int, offset: int
    ) -> Dict[str, Any]:
        """Construct the POST body exactly as the DIKSHA Angular app does."""
        api_filters: Dict[str, Any] = {
            "primaryCategory": "Digital Textbook",
        }

        # Map caller-friendly keys → DIKSHA API field names
        field_map = {
            "board": "board",
            "medium": "medium",
            "gradeLevel": "gradeLevel",
            "subject": "subject",
            "contentType": "contentType",
        }
        for caller_key, api_key in field_map.items():
            value = filters.get(caller_key)
            if value is not None:
                api_filters[api_key] = value

        return {
            "request": {
                "filters": api_filters,
                "limit": limit,
                "offset": offset,
                "fields": [
                    "identifier",
                    "name",
                    "board",
                    "medium",
                    "gradeLevel",
                    "subject",
                    "contentType",
                    "primaryCategory",
                    "mimeType",
                    "size",
                    "downloadUrl",
                    "variants",
                ],
                "facets": [],
            }
        }

    @staticmethod
    def _parse_content_item(raw: Dict[str, Any]) -> Optional[ContentItem]:
        """Convert a raw API dict into a ``ContentItem``."""
        identifier = raw.get("identifier")
        if not identifier:
            return None

        # downloadUrl can be a string or a dict
        raw_dl = raw.get("downloadUrl")
        dl_str: Optional[str] = None
        dl_map: Optional[Dict[str, str]] = None
        if isinstance(raw_dl, str):
            dl_str = raw_dl
        elif isinstance(raw_dl, dict):
            dl_map = raw_dl

        return ContentItem(
            identifier=identifier,
            name=raw.get("name", ""),
            board=raw.get("board"),
            medium=raw.get("medium"),
            grade_level=raw.get("gradeLevel"),
            subject=raw.get("subject"),
            content_type=raw.get("contentType"),
            primary_category=raw.get("primaryCategory"),
            mime_type=raw.get("mimeType"),
            size=raw.get("size"),
            download_url=dl_str,
            download_url_map=dl_map,
            variants=raw.get("variants"),
            raw=raw,
        )

    @staticmethod
    def _pick_from_variants(
        variants: Dict[str, Any], preferred: str
    ) -> Optional[str]:
        """Pick the best URL from the ``variants`` dict.

        DIKSHA variants look like::

            {
              "spine": {"ecarUrl": "...", "size": "123"},
              "online": {"ecarUrl": "...", "size": "456"},
              "full": {"ecarUrl": "...", "size": "789"}
            }

        We prefer ``full`` → ``spine`` → ``online`` (spine has the actual
        content package; online is a lightweight streaming version).
        The URL key is ``ecarUrl`` (not ``ecarFile`` — confirmed from live API).
        """
        priority = ["full", "spine", "online"]
        for key in priority:
            variant = variants.get(key, {})
            if not isinstance(variant, dict):
                continue
            # API uses ecarUrl; fall back to ecarFile / downloadUrl just in case
            url = (
                variant.get("ecarUrl")
                or variant.get("ecarFile")
                or variant.get("downloadUrl")
            )
            if url:
                return url
        # Fallback: first variant with any URL-like value
        for variant in variants.values():
            if isinstance(variant, dict):
                url = (
                    variant.get("ecarUrl")
                    or variant.get("ecarFile")
                    or variant.get("downloadUrl")
                )
                if url:
                    return url
        return None

    @staticmethod
    def _pick_from_map(
        url_map: Dict[str, str], preferred: str
    ) -> Optional[str]:
        """Pick from a format-keyed dict, honouring *preferred*."""
        if preferred in url_map:
            return url_map[preferred]
        # Fallback order
        for fmt in ("pdf", "epub", "html"):
            if fmt in url_map:
                return url_map[fmt]
        # Any value
        return next(iter(url_map.values()), None)

    # ── HTTP helpers with retry ────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._session.post(
            url,
            json=payload,
            timeout=self._config.scraper.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _get(self, url: str) -> Dict[str, Any]:
        resp = self._session.get(
            url,
            timeout=self._config.scraper.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()
