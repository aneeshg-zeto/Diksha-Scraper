"""PDF Extractor -- Phase 2.
Walks every .ecar in the downloads/ folder, reads its hierarchy.json,
finds all chapter PDF URLs, and downloads them.

Resume safe: tracks state in pdf_manifest.json.
Run as many times as needed — completed files are skipped.

Usage:
    .venv\Scripts\python.exe extract_pdfs.py

Options (edit below or pass as env vars):
    OUTPUT_DIR   — where to save PDFs (default: downloads/ same tree)
    CONCURRENCY  — parallel downloads (default: 3)
    DELAY        — seconds between requests (default: 0.3)
"""

from __future__ import annotations

import asyncio
import json
import os
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiofiles
import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

# ── Config ─────────────────────────────────────────────────────────────────────
# Paths relative to this script so it works from any directory
_BASE           = Path(__file__).parent
DOWNLOADS_DIR   = _BASE / "downloads"
PDF_MANIFEST    = _BASE / "pdf_manifest.json"
CONCURRENCY     = int(os.environ.get("CONCURRENCY", 3))
DELAY           = float(os.environ.get("DELAY", 0.3))
CHUNK_SIZE      = 65536
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5

console = Console()


# ── Data classes ───────────────────────────────────────────────────────────────

class PdfTask:
    def __init__(
        self,
        ecar_path: Path,
        chapter_name: str,
        url: str,
        local_path: Path,
    ):
        self.ecar_path    = ecar_path
        self.chapter_name = chapter_name
        self.url          = url
        self.local_path   = local_path
        self.key          = str(local_path)   # manifest key


# ── Manifest ───────────────────────────────────────────────────────────────────

def load_manifest() -> Dict[str, str]:
    """Returns {local_path_str: status}"""
    if PDF_MANIFEST.exists():
        try:
            with PDF_MANIFEST.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_manifest(manifest: Dict[str, str]) -> None:
    with PDF_MANIFEST.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# ── Ecar parsing ───────────────────────────────────────────────────────────────

def get_pdf_tasks_from_ecar(ecar_path: Path) -> List[PdfTask]:
    """Extract all PDF download tasks from one ecar file."""
    tasks: List[PdfTask] = []
    seen_urls = set()

    try:
        with zipfile.ZipFile(ecar_path) as z:
            if "hierarchy.json" not in z.namelist():
                return []
            with z.open("hierarchy.json") as f:
                hierarchy = json.load(f)
    except Exception:
        return []

    content = hierarchy.get("content", {})

    # Build output folder mirroring the ecar's location
    # ecar is at: downloads/Board/Class/Subject/Name.ecar
    # PDFs go to: downloads/Board/Class/Subject/Name/Chapter.pdf
    ecar_stem = ecar_path.stem  # textbook name without .ecar
    pdf_base  = ecar_path.parent / ecar_stem
    pdf_base.mkdir(parents=True, exist_ok=True)

    def walk(node: dict, depth: int = 0) -> None:
        name = (node.get("name") or "unnamed").strip()
        # Check both artifactUrl and downloadUrl for .pdf
        for key in ["artifactUrl", "downloadUrl"]:
            url = (node.get(key) or "").strip()
            if not url:
                continue
            url_lower = url.lower()
            if ".pdf" not in url_lower:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Build a clean filename
            safe_name = _sanitise(name)[:100] or f"chapter_{len(tasks)+1}"
            local_path = pdf_base / f"{safe_name}.pdf"

            # Avoid collisions
            counter = 1
            while local_path.exists() and local_path in {t.local_path for t in tasks}:
                local_path = pdf_base / f"{safe_name}_{counter}.pdf"
                counter += 1

            tasks.append(PdfTask(
                ecar_path=ecar_path,
                chapter_name=name,
                url=url,
                local_path=local_path,
            ))

        for child in node.get("children", []):
            walk(child, depth + 1)

    walk(content)
    return tasks


def _sanitise(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip().strip(".")


# ── Async downloader ───────────────────────────────────────────────────────────

async def download_pdf(
    task: PdfTask,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    manifest: Dict[str, str],
    progress: Progress,
    overall_task,
) -> None:
    key = task.key

    # Resume: skip if already completed with correct size
    if manifest.get(key) == "completed" and task.local_path.exists():
        progress.advance(overall_task)
        return

    async with sem:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                # Range resume
                resume_pos = task.local_path.stat().st_size if task.local_path.exists() else 0
                headers = {}
                if resume_pos > 0:
                    headers["Range"] = f"bytes={resume_pos}-"

                async with session.get(task.url, headers=headers) as resp:
                    if resp.status == 416:
                        # Already complete
                        manifest[key] = "completed"
                        progress.advance(overall_task)
                        return
                    if resp.status not in (200, 206):
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status
                        )

                    mode = "ab" if resume_pos > 0 and resp.status == 206 else "wb"
                    task.local_path.parent.mkdir(parents=True, exist_ok=True)
                    async with aiofiles.open(task.local_path, mode) as fh:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            await fh.write(chunk)

                manifest[key] = "completed"
                progress.advance(overall_task)
                return

            except Exception as exc:
                if attempt < RETRY_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    manifest[key] = f"failed: {exc}"
                    progress.advance(overall_task)


async def run_downloads(tasks: List[PdfTask], manifest: Dict[str, str]) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=True, limit=20)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://diksha.gov.in/",
    }

    # Load cookies
    cookies = {}
    cookie_file = _BASE / "auth" / "cookies.json"
    if cookie_file.exists():
        with cookie_file.open(encoding="utf-8") as f:
            for c in json.load(f):
                cookies[c["name"]] = c["value"]

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )

    overall = progress.add_task("Downloading PDFs…", total=len(tasks))

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers=headers,
        cookies=cookies,
    ) as session:
        with progress:
            batch_size = 50  # save manifest every 50 completions
            for i in range(0, len(tasks), batch_size):
                batch = tasks[i : i + batch_size]
                await asyncio.gather(
                    *[
                        download_pdf(t, session, sem, manifest, progress, overall)
                        for t in batch
                    ],
                    return_exceptions=True,
                )
                save_manifest(manifest)
                await asyncio.sleep(DELAY)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    console.rule("[bold cyan]DIKSHA PDF Extractor[/]")

    # Find all ecars
    ecars = sorted(DOWNLOADS_DIR.rglob("*.ecar"))
    console.print(f"Found [bold]{len(ecars)}[/] ecar files to process.")

    if not ecars:
        console.print("[yellow]No ecar files found. Run the main scraper first.[/]")
        return

    # Load manifest
    manifest = load_manifest()
    already_done = sum(1 for v in manifest.values() if v == "completed")
    console.print(f"Manifest: [green]{already_done}[/] PDFs already downloaded.")

    # Build all tasks
    console.print("Scanning ecar files for PDF URLs…")
    all_tasks: List[PdfTask] = []

    scan_progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
    )
    with scan_progress:
        scan_task = scan_progress.add_task("Scanning…", total=len(ecars))
        for ecar in ecars:
            tasks = get_pdf_tasks_from_ecar(ecar)
            all_tasks.extend(tasks)
            scan_progress.advance(scan_task)

    # Filter out already completed
    pending = [t for t in all_tasks if manifest.get(t.key) != "completed"]

    console.print(f"\nTotal PDF chapters found : [bold]{len(all_tasks)}[/]")
    console.print(f"Already downloaded       : [green]{len(all_tasks) - len(pending)}[/]")
    console.print(f"Remaining to download    : [bold cyan]{len(pending)}[/]")

    if not pending:
        console.print("\n[green]All PDFs already downloaded![/]")
        return

    # Estimate size (rough: avg 5 MB per PDF)
    est_gb = len(pending) * 5 / 1024
    console.print(f"Estimated download size  : ~{est_gb:.1f} GB")
    console.print(f"Concurrency              : {CONCURRENCY} parallel downloads\n")

    # Run
    try:
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.run(run_downloads(pending, manifest))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — saving progress…[/]")
    finally:
        save_manifest(manifest)

    # Summary
    completed = sum(1 for v in manifest.values() if v == "completed")
    failed    = sum(1 for v in manifest.values() if v.startswith("failed"))
    console.print(f"\n[green]Done![/] Completed: {completed} | Failed: {failed}")
    console.print("Run again to retry any failed downloads.")


if __name__ == "__main__":
    main()
