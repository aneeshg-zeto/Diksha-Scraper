"""Configuration loader — reads config.yaml and exposes a typed ScraperConfig."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


# ── Sub-models ─────────────────────────────────────────────────────────────────

class DIKSHAFilters(BaseModel):
    boards: List[str] = Field(default_factory=list)
    mediums: List[str] = Field(default_factory=list)
    classes: List[str] = Field(default_factory=list)
    subjects: List[str] = Field(default_factory=list)
    content_types: List[str] = Field(default_factory=lambda: ["Digital Textbook"])


class DIKSHAConfig(BaseModel):
    api_base_url: str = "https://diksha.gov.in/api"
    search_endpoint: str = "/content/v1/search"
    read_endpoint: str = "/content/v1/read"
    framework_id: str = "ekstep_ncert_k-12"
    filters: DIKSHAFilters = Field(default_factory=DIKSHAFilters)


class AuthConfig(BaseModel):
    cookies_file: str = "auth/cookies.json"
    token_file: str = "auth/token.json"
    login_url: str = (
        "https://diksha.gov.in/search/Library/1"
        "?id=ekstep_ncert_k-12&primaryCategory=digital%20textbook"
        "&se_mediums=english&&selectedTab=all"
    )
    profile_url: str = "https://diksha.gov.in/profile"
    headless: bool = False
    login_timeout_seconds: int = 300


class DownloadConfig(BaseModel):
    output_dir: str = "downloads"
    manifest_file: str = "downloads/manifest.json"
    concurrent_downloads: int = 3
    chunk_size_bytes: int = 65536
    retry_attempts: int = 3
    retry_delay_seconds: int = 5
    preferred_format: str = "pdf"


class ReportingConfig(BaseModel):
    discovered_resources_file: str = "discovered_resources.json"
    csv_report_file: str = "report.csv"


class ScraperSettings(BaseModel):
    dry_run: bool = False
    request_delay_seconds: float = 0.5
    request_timeout_seconds: int = 30
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


# ── Root config ────────────────────────────────────────────────────────────────

class ScraperConfig(BaseModel):
    diksha: DIKSHAConfig = Field(default_factory=DIKSHAConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    scraper: ScraperSettings = Field(default_factory=ScraperSettings)

    # ── Convenience properties ─────────────────────────────────────────────

    @property
    def search_url(self) -> str:
        return self.diksha.api_base_url + self.diksha.search_endpoint

    @property
    def read_url_template(self) -> str:
        return self.diksha.api_base_url + self.diksha.read_endpoint + "/{content_id}"

    # ── Factory ────────────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> "ScraperConfig":
        """Load and validate configuration from a YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path.resolve()}\n"
                "Copy config.yaml from the project root and adjust as needed."
            )
        with config_path.open("r", encoding="utf-8") as fh:
            raw: Dict[str, Any] = yaml.safe_load(fh) or {}
        return cls.model_validate(raw)

    def ensure_directories(self) -> None:
        """Create all required output directories if they don't exist."""
        dirs = [
            Path(self.download.output_dir),
            Path(self.download.manifest_file).parent,
            Path(self.auth.cookies_file).parent,
            Path(self.auth.token_file).parent,
            Path(self.reporting.discovered_resources_file).parent,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
