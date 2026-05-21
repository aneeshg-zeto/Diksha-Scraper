"""Async download manager with resume support and concurrency control.

Uses ``aiohttp`` for non-blocking HTTP and ``asyncio.Semaphore`` to cap
concurrent downloads.  A ``manifest.json`` file tracks every download so
interrupted runs can be resumed without re-downloading completed files.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
import aiohttp
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .config import ScraperConfig
from .logger import get_logger
from .models import ContentItem, DownloadStatus, ManifestEntry

log = get_logger(__name__)


class DownloadManager:
    """Manages concurrent, resumable downloads for a list of ContentItems."""

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config
        self._manifest_path = Path(config.download.manifest_file)
        self._output_dir = Path(config.download.output_dir)
        self._manifest: Dict[str, ManifestEntry] = {}

    # ── Public entry point ─────────────────────────────────────────────────

    def run(self, items: List[ContentItem]) -> None:
        """Download all *items* (blocking wrapper around the async core)."""
        self._load_manifest()
        try:
            # Windows requires the ProactorEventLoop for aiohttp
            import asyncio
            if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            asyncio.run(self._download_all(items))
        except KeyboardInterrupt:
            log.info("Download interrupted by user — saving manifest.")
        except Exception as exc:
            log.error("Download loop crashed: %s", exc, exc_info=True)
        finally:
            self._save_manifest()

    # ── Path construction ──────────────────────────────────────────────────

    def build_local_path(self, item: ContentItem) -> Path:
        """Build the local file path for *item*.

        Structure: ``output_dir / Board / Class / Subject / <name>.<ext>``
        """
        board = self._sanitise(item.board or "Unknown_Board")
        grade = self._sanitise(item.grade_level or "Unknown_Class")
        subject = self._sanitise(item.subject or "Unknown_Subject")

        folder = self._output_dir / board / grade / subject
        folder.mkdir(parents=True, exist_ok=True)

        ext = self._guess_extension(item)
        safe_name = self._sanitise(item.name or item.identifier)
        # Truncate to avoid OS path-length limits
        safe_name = safe_name[:120]
        return folder / f"{safe_name}{ext}"

    # ── Async core ─────────────────────────────────────────────────────────

    async def _download_all(self, items: List[ContentItem]) -> None:
        sem = asyncio.Semaphore(self._config.download.concurrent_downloads)

        connector = aiohttp.TCPConnector(ssl=True, limit=20)
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        headers = {
            "User-Agent": self._config.scraper.user_agent,
            "Referer": "https://diksha.gov.in/",
        }

        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )

        # Overall progress bar
        overall_task = progress.add_task(
            f"[bold cyan]Downloading {len(items)} files…", total=len(items)
        )

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers
        ) as session:
            with progress:
                for item in items:
                    await self._download_one(item, session, sem, progress, overall_task)
                    # Save manifest periodically so resume works after interruption
                    if list(self._manifest.values()).count != 0:
                        pass  # saved in finally block

    async def _download_one(
        self,
        item: ContentItem,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        progress: Progress,
        overall_task: TaskID,
    ) -> None:
        """Download a single item, honouring resume logic."""
        url = item.best_download_url
        if not url:
            log.warning("No download URL for '%s' — skipping.", item.name)
            self._update_manifest(item, DownloadStatus.SKIPPED, error="No URL")
            progress.advance(overall_task)
            return

        local_path = self.build_local_path(item)
        entry = self._manifest.get(item.identifier)

        # Resume check
        if entry and entry.status == DownloadStatus.COMPLETED:
            if local_path.exists():
                if entry.file_size_bytes and local_path.stat().st_size == entry.file_size_bytes:
                    log.debug("Already downloaded: %s", local_path.name)
                    progress.advance(overall_task)
                    return

        file_task: TaskID = progress.add_task(
            f"[cyan]{item.name[:45]}",
            total=None,
        )

        async with sem:
            for attempt in range(1, self._config.download.retry_attempts + 1):
                try:
                    await self._fetch_file(url, local_path, session, progress, file_task)
                    self._update_manifest(
                        item,
                        DownloadStatus.COMPLETED,
                        local_path=str(local_path),
                        file_size=local_path.stat().st_size if local_path.exists() else None,
                    )
                    progress.update(file_task, description=f"[green]✓ {item.name[:45]}")
                    progress.advance(overall_task)
                    # Periodic manifest save every 10 completions
                    completed_count = sum(
                        1 for e in self._manifest.values()
                        if e.status == DownloadStatus.COMPLETED
                    )
                    if completed_count % 10 == 0:
                        self._save_manifest()
                    return
                except Exception as exc:
                    log.warning(
                        "Attempt %d/%d failed for '%s': %s",
                        attempt, self._config.download.retry_attempts, item.name, exc,
                    )
                    if attempt < self._config.download.retry_attempts:
                        await asyncio.sleep(self._config.download.retry_delay_seconds)

            self._update_manifest(
                item, DownloadStatus.FAILED,
                error=f"Failed after {self._config.download.retry_attempts} attempts",
            )
            progress.update(file_task, description=f"[red]✗ {item.name[:45]}")
            progress.advance(overall_task)

    async def _fetch_file(
        self,
        url: str,
        local_path: Path,
        session: aiohttp.ClientSession,
        progress: Progress,
        task_id: TaskID,
    ) -> None:
        """Stream *url* to *local_path*, updating *progress* as bytes arrive."""
        # Support partial resume via Range header
        resume_pos = 0
        if local_path.exists():
            resume_pos = local_path.stat().st_size

        request_headers: Dict[str, str] = {}
        if resume_pos > 0:
            request_headers["Range"] = f"bytes={resume_pos}-"

        async with session.get(url, headers=request_headers) as resp:
            if resp.status == 416:
                # Range not satisfiable — file already complete
                return
            resp.raise_for_status()

            total = resp.content_length
            if total:
                progress.update(task_id, total=total + resume_pos)
            progress.update(task_id, completed=resume_pos)

            mode = "ab" if resume_pos > 0 and resp.status == 206 else "wb"
            async with aiofiles.open(local_path, mode) as fh:
                async for chunk in resp.content.iter_chunked(
                    self._config.download.chunk_size_bytes
                ):
                    await fh.write(chunk)
                    progress.advance(task_id, len(chunk))

    # ── Manifest helpers ───────────────────────────────────────────────────

    def _load_manifest(self) -> None:
        if self._manifest_path.exists():
            try:
                with self._manifest_path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._manifest = {
                    k: ManifestEntry.model_validate(v) for k, v in raw.items()
                }
                log.info(
                    "Loaded manifest with %d entries.", len(self._manifest)
                )
            except Exception as exc:
                log.warning("Could not load manifest: %s — starting fresh.", exc)
                self._manifest = {}

    def _save_manifest(self) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self._manifest_path.open("w", encoding="utf-8") as fh:
            json.dump(
                {k: v.model_dump() for k, v in self._manifest.items()},
                fh,
                indent=2,
                ensure_ascii=False,
            )
        log.debug("Manifest saved (%d entries).", len(self._manifest))

    def _update_manifest(
        self,
        item: ContentItem,
        status: DownloadStatus,
        local_path: Optional[str] = None,
        file_size: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        entry = self._manifest.get(item.identifier) or ManifestEntry(
            identifier=item.identifier,
            name=item.name,
            download_url=item.best_download_url,
        )
        entry.status = status
        if local_path:
            entry.local_path = local_path
        if file_size is not None:
            entry.file_size_bytes = file_size
        if error:
            entry.error_message = error
        self._manifest[item.identifier] = entry

    # ── Static helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _sanitise(name: str) -> str:
        """Replace filesystem-unsafe characters."""
        for ch in r'\/:*?"<>|':
            name = name.replace(ch, "_")
        return name.strip().strip(".")

    @staticmethod
    def _guess_extension(item: ContentItem) -> str:
        """Infer file extension from mime type or download URL."""
        mime_map = {
            "application/pdf": ".pdf",
            "application/epub+zip": ".epub",
            "application/vnd.ekstep.content-archive": ".ecar",
            "application/vnd.ekstep.content-collection": ".ecar",
            "video/mp4": ".mp4",
        }
        if item.mime_type and item.mime_type in mime_map:
            return mime_map[item.mime_type]
        url = item.best_download_url or ""
        for ext in (".pdf", ".epub", ".ecar", ".mp4", ".zip"):
            if ext in url.lower():
                return ext
        return ".ecar"  # DIKSHA textbooks are primarily .ecar packages
