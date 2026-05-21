"""Entry point for the DIKSHA scraper.

Usage
-----
    python -m diksha_scraper [OPTIONS]

Options
-------
    --config PATH       Path to config.yaml  (default: config.yaml)
    --login             Force a fresh browser login and save cookies/token
    --dry-run           Discover and report only — do not download
    --resume            Skip discovery; load discovered_resources.json and download
    --boards B1,B2      Override boards filter (comma-separated)
    --mediums M1,M2     Override mediums filter
    --classes C1,C2     Override classes filter
    --subjects S1,S2    Override subjects filter
    --output DIR        Override download output directory
    --concurrency N     Override concurrent download count
    --help              Show this message
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from .auth import login_and_save_cookies
from .config import ScraperConfig
from .discovery import DiscoveryOrchestrator
from .downloader import DownloadManager
from .logger import get_logger
from .reporter import Reporter

console = Console()
log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diksha_scraper",
        description="DIKSHA digital textbook scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open browser for manual login and save cookies/token",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and report only — do not download files",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip discovery; load discovered_resources.json and download",
    )
    parser.add_argument(
        "--boards",
        metavar="B1,B2",
        help="Override boards filter (comma-separated)",
    )
    parser.add_argument(
        "--mediums",
        metavar="M1,M2",
        help="Override mediums filter (comma-separated)",
    )
    parser.add_argument(
        "--classes",
        metavar="C1,C2",
        help="Override classes filter (comma-separated)",
    )
    parser.add_argument(
        "--subjects",
        metavar="S1,S2",
        help="Override subjects filter (comma-separated)",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        help="Override download output directory",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        metavar="N",
        help="Override concurrent download count",
    )
    return parser


def apply_overrides(config: ScraperConfig, args: argparse.Namespace) -> None:
    """Apply CLI overrides to the loaded config in-place."""
    if args.boards:
        config.diksha.filters.boards = [b.strip() for b in args.boards.split(",")]
    if args.mediums:
        config.diksha.filters.mediums = [m.strip() for m in args.mediums.split(",")]
    if args.classes:
        config.diksha.filters.classes = [c.strip() for c in args.classes.split(",")]
    if args.subjects:
        config.diksha.filters.subjects = [s.strip() for s in args.subjects.split(",")]
    if args.output:
        config.download.output_dir = args.output
    if args.concurrency:
        config.download.concurrent_downloads = args.concurrency
    if args.dry_run:
        config.scraper.dry_run = True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # ── Load config ────────────────────────────────────────────────────────
    try:
        config = ScraperConfig.from_yaml(args.config)
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 1

    apply_overrides(config, args)
    config.ensure_directories()

    console.rule("[bold cyan]DIKSHA Scraper[/]")
    console.print(
        f"  Config : [dim]{args.config}[/]\n"
        f"  Dry run: [dim]{config.scraper.dry_run}[/]\n"
        f"  Output : [dim]{config.download.output_dir}[/]"
    )
    console.rule()

    # ── Login ──────────────────────────────────────────────────────────────
    if args.login:
        log.info("Starting browser login flow…")
        try:
            login_and_save_cookies(config)
        except Exception as exc:
            console.print(f"[red]Login failed:[/] {exc}")
            return 1
        console.print("[green]Login complete.[/]  You can now run without --login.")
        return 0

    # ── Discovery ──────────────────────────────────────────────────────────
    if args.resume:
        log.info("--resume: loading previously discovered items…")
        items = DiscoveryOrchestrator.load_discovered(config)
        if not items:
            console.print(
                "[yellow]No discovered_resources.json found.[/]  "
                "Run without --resume first."
            )
            return 1
    else:
        orchestrator = DiscoveryOrchestrator(config)
        items = orchestrator.run()

    if not items:
        console.print("[yellow]No textbooks discovered.  Check your filters and login.[/]")
        return 0

    console.print(f"\n[bold green]{len(items)}[/] unique textbooks discovered.")

    # ── Reporting ──────────────────────────────────────────────────────────
    reporter = Reporter(config)
    csv_path = reporter.write_csv(items)
    console.print(f"CSV report: [dim]{csv_path}[/]")

    # ── Dry run stops here ─────────────────────────────────────────────────
    if config.scraper.dry_run:
        console.print(
            "\n[yellow]Dry-run mode:[/] discovery and reporting complete.  "
            "No files were downloaded."
        )
        reporter.print_summary(items)
        return 0

    # ── Download ───────────────────────────────────────────────────────────
    downloadable = [i for i in items if i.best_download_url]
    skipped = len(items) - len(downloadable)
    if skipped:
        log.warning("%d items have no download URL and will be skipped.", skipped)

    console.print(
        f"\nDownloading [bold]{len(downloadable)}[/] textbooks "
        f"({config.download.concurrent_downloads} concurrent)…"
    )

    manager = DownloadManager(config)
    manager.run(downloadable)

    # ── Final report ───────────────────────────────────────────────────────
    reporter.write_csv(items)   # refresh CSV with download statuses
    reporter.print_summary(items)

    return 0


if __name__ == "__main__":
    sys.exit(main())
