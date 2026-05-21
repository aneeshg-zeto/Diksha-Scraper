"""Pydantic data models shared across the scraper."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ── Download status ────────────────────────────────────────────────────────────

class DownloadStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ── Content item (one digital textbook) ───────────────────────────────────────

class ContentItem(BaseModel):
    """Represents a single digital textbook returned by the DIKSHA search API."""

    identifier: str
    name: str
    board: Optional[str] = None
    medium: Optional[str] = None
    grade_level: Optional[str] = None
    subject: Optional[str] = None
    content_type: Optional[str] = None
    primary_category: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None

    # Raw download URLs from the API response
    download_url: Optional[str] = None          # top-level downloadUrl (string)
    download_url_map: Optional[Dict[str, str]] = None  # downloadUrl as dict {pdf: ..., epub: ...}
    variants: Optional[Dict[str, Any]] = None   # quality variants

    # Resolved best URL (populated by api.get_download_url)
    best_download_url: Optional[str] = None
    preferred_format: Optional[str] = None      # "pdf" | "epub" | etc.

    # Local state
    local_path: Optional[str] = None
    status: DownloadStatus = DownloadStatus.PENDING
    error_message: Optional[str] = None

    # Raw API payload kept for debugging
    raw: Optional[Dict[str, Any]] = Field(default=None, exclude=True)

    @field_validator("board", "medium", "grade_level", "subject", mode="before")
    @classmethod
    def coerce_list_to_first(cls, v: Any) -> Optional[str]:
        """DIKSHA sometimes returns these fields as single-element lists."""
        if isinstance(v, list):
            return v[0] if v else None
        return v

    class Config:
        populate_by_name = True


# ── Manifest entry (persisted per download) ───────────────────────────────────

class ManifestEntry(BaseModel):
    identifier: str
    name: str
    local_path: Optional[str] = None
    status: DownloadStatus = DownloadStatus.PENDING
    file_size_bytes: Optional[int] = None
    error_message: Optional[str] = None
    download_url: Optional[str] = None
