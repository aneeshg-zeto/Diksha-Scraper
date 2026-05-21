"""Reporting module — generates CSV summaries of discovered and downloaded content."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List

from .config import ScraperConfig
from .logger import get_logger
from .models import ContentItem, DownloadStatus, ManifestEntry

log = get_logger(__name__)

_CSV_FIELDS = [
    "identifier",
    "name",
    "board",
    "medium",
    "grade_level",
    "subject",
    "content_type",
    "mime_type",
    "size_bytes",
    "best_download_url",
    "local_path",
    "download_status",
    "error_message",
]


class Reporter:
    """Generates CSV and summary reports."""

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config

    # ── CSV report ─────────────────────────────────────────────────────────

    def write_csv(self, items: List[ContentItem]) -> Path:
        """Write a CSV report for *items* and return the output path."""
        manifest = self._load_manifest()
        out_path = Path(self._config.reporting.csv_report_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for item in items:
                entry = manifest.get(item.identifier)
                writer.writerow(
                    {
                        "identifier": item.identifier,
                        "name": item.name,
                        "board": item.board or "",
                        "medium": item.medium or "",
                        "grade_level": item.grade_level or "",
                        "subject": item.subject or "",
                        "content_type": item.content_type or "",
                        "mime_type": item.mime_type or "",
                        "size_bytes": item.size or "",
                        "best_download_url": item.best_download_url or "",
                        "local_path": entry.local_path if entry else "",
                        "download_status": entry.status.value if entry else DownloadStatus.PENDING.value,
                        "error_message": entry.error_message if entry else "",
                    }
                )

        log.info("CSV report written to %s (%d rows)", out_path, len(items))
        return out_path

    # ── Console summary ────────────────────────────────────────────────────

    def print_summary(self, items: List[ContentItem]) -> None:
        """Print a Rich-formatted summary table to the console."""
        from rich.table import Table
        from rich.console import Console

        manifest = self._load_manifest()
        console = Console()

        counts = {s: 0 for s in DownloadStatus}
        for item in items:
            entry = manifest.get(item.identifier)
            status = entry.status if entry else DownloadStatus.PENDING
            counts[status] += 1

        table = Table(title="Download Summary", show_header=True, header_style="bold cyan")
        table.add_column("Status", style="bold")
        table.add_column("Count", justify="right")

        status_styles = {
            DownloadStatus.COMPLETED: "green",
            DownloadStatus.FAILED: "red",
            DownloadStatus.SKIPPED: "yellow",
            DownloadStatus.PENDING: "dim",
            DownloadStatus.IN_PROGRESS: "blue",
        }
        for status, count in counts.items():
            if count:
                table.add_row(
                    f"[{status_styles[status]}]{status.value}[/]",
                    str(count),
                )

        table.add_row("[bold]Total[/]", str(len(items)), end_section=True)
        console.print(table)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _load_manifest(self) -> dict:
        path = Path(self._config.download.manifest_file)
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return {k: ManifestEntry.model_validate(v) for k, v in raw.items()}
        except Exception as exc:
            log.warning("Could not load manifest for reporting: %s", exc)
            return {}
