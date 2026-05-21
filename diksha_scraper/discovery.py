"""Discovery orchestrator.

Strategy
--------
Instead of making one API call per Board×Medium×Class×Subject combination
(11,520 calls, ~2 hours), we fetch the entire DIKSHA Digital Textbook catalogue
in a single paginated sweep — typically ~107 pages of 100 items each, done in
under 3 minutes.

We then filter the results in-memory against the boards/mediums/classes/subjects
configured by the user.  This is far faster and puts less load on the API.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .auth import get_authenticated_session
from .config import ScraperConfig
from .logger import get_logger
from .models import ContentItem

log = get_logger(__name__)

_PAGE_SIZE = 100


class DiscoveryOrchestrator:
    """Drives the full discovery phase."""

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config
        self._session = get_authenticated_session(config)

    # ── Public entry point ─────────────────────────────────────────────────

    def run(self) -> List[ContentItem]:
        """Fetch the full DIKSHA catalogue, filter in-memory, return items.

        Saves results to ``discovered_resources.json`` before returning.
        """
        log.info("Fetching full DIKSHA Digital Textbook catalogue…")

        all_items = self._fetch_all_pages()

        log.info("Fetched %d total items from API.", len(all_items))

        # Filter in-memory against configured boards/mediums/classes/subjects
        filtered = self._apply_filters(all_items)

        log.info(
            "After filtering: %d items match your configured boards/mediums/classes/subjects.",
            len(filtered),
        )

        self._save_discovered(filtered)
        return filtered

    # ── Paginated fetch ────────────────────────────────────────────────────

    def _fetch_all_pages(self) -> List[ContentItem]:
        """Fetch every page of Digital Textbooks from the API."""
        import requests

        url = self._config.search_url
        fields = [
            "identifier", "name", "board", "medium", "gradeLevel",
            "subject", "contentType", "primaryCategory", "mimeType",
            "size", "downloadUrl", "variants",
        ]

        # First call to get total count
        first_payload = self._build_payload(fields, limit=_PAGE_SIZE, offset=0)
        try:
            resp = self._session.post(
                url, json=first_payload,
                timeout=self._config.scraper.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"First API call failed: {exc}")

        result = data.get("result", {})
        total: int = result.get("count", 0)
        content = result.get("content") or []

        if total == 0:
            log.warning("API returned count=0. Check your login session.")
            return []

        total_pages = -(-total // _PAGE_SIZE)  # ceiling division
        log.info("Total textbooks in catalogue: %d (%d pages)", total, total_pages)

        items: Dict[str, ContentItem] = {}
        for raw in content:
            item = self._parse(raw)
            if item:
                items[item.identifier] = item

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )

        with progress:
            task = progress.add_task(
                f"Downloading catalogue… (page 1/{total_pages})",
                total=total_pages,
            )
            progress.advance(task)  # page 1 already done

            offset = _PAGE_SIZE
            page = 2
            while offset < total:
                progress.update(
                    task,
                    description=f"Downloading catalogue… (page {page}/{total_pages})",
                )
                payload = self._build_payload(fields, limit=_PAGE_SIZE, offset=offset)
                try:
                    resp = self._session.post(
                        url, json=payload,
                        timeout=self._config.scraper.request_timeout_seconds,
                    )
                    resp.raise_for_status()
                    page_data = resp.json()
                    page_content = page_data.get("result", {}).get("content") or []
                    for raw in page_content:
                        item = self._parse(raw)
                        if item and item.identifier not in items:
                            items[item.identifier] = item
                except Exception as exc:
                    log.warning("Page %d failed (offset=%d): %s", page, offset, exc)

                progress.advance(task)
                offset += _PAGE_SIZE
                page += 1
                time.sleep(self._config.scraper.request_delay_seconds)

        return list(items.values())

    # ── In-memory filtering ────────────────────────────────────────────────

    def _apply_filters(self, items: List[ContentItem]) -> List[ContentItem]:
        """Keep only items matching the configured filter lists.

        If a filter list is empty, that dimension is not filtered (keep all).
        Comparisons are case-insensitive.
        """
        cfg = self._config.diksha.filters

        boards   = {b.lower() for b in cfg.boards}   if cfg.boards   else set()
        mediums  = {m.lower() for m in cfg.mediums}  if cfg.mediums  else set()
        classes  = {c.lower() for c in cfg.classes}  if cfg.classes  else set()
        subjects = {s.lower() for s in cfg.subjects} if cfg.subjects else set()

        def _matches(item: ContentItem) -> bool:
            if boards and not _field_matches(item.board, boards):
                return False
            if mediums and not _field_matches(item.medium, mediums):
                return False
            if classes and not _field_matches(item.grade_level, classes):
                return False
            if subjects and not _field_matches(item.subject, subjects):
                return False
            return True

        filtered = [i for i in items if _matches(i)]

        # Resolve download URLs for matched items
        from .api import DIKSHAApiClient
        api = DIKSHAApiClient(self._session, self._config)
        for item in filtered:
            item.best_download_url = api.get_download_url(item)

        return filtered

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_payload(fields: List[str], limit: int, offset: int) -> dict:
        return {
            "request": {
                "filters": {"primaryCategory": "Digital Textbook"},
                "limit": limit,
                "offset": offset,
                "fields": fields,
            }
        }

    @staticmethod
    def _parse(raw: dict) -> Optional[ContentItem]:
        identifier = raw.get("identifier")
        if not identifier:
            return None

        raw_dl = raw.get("downloadUrl")
        dl_str = raw_dl if isinstance(raw_dl, str) else None
        dl_map = raw_dl if isinstance(raw_dl, dict) else None

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

    # ── Persistence ────────────────────────────────────────────────────────

    def _save_discovered(self, items: List[ContentItem]) -> None:
        out_path = Path(self._config.reporting.discovered_resources_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        serialisable = [item.model_dump(exclude={"raw"}) for item in items]
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(serialisable, fh, indent=2, ensure_ascii=False)
        log.info("Saved %d discovered items to %s", len(items), out_path)

    @staticmethod
    def load_discovered(config: ScraperConfig) -> List[ContentItem]:
        """Load a previously saved ``discovered_resources.json``."""
        path = Path(config.reporting.discovered_resources_file)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            raw_list = json.load(fh)
        items = [ContentItem.model_validate(r) for r in raw_list]
        log.info("Loaded %d previously discovered items from %s", len(items), path)
        return items


# ── Utility ────────────────────────────────────────────────────────────────────

def _field_matches(field_value: Optional[str], allowed: Set[str]) -> bool:
    """Check if a content item field matches any value in *allowed* (case-insensitive).

    ``field_value`` may be a plain string or a comma-joined string from the
    Pydantic coercion of a list (e.g. ``"English"`` or ``"Mathematics"``).
    """
    if field_value is None:
        return False
    return field_value.lower() in allowed
