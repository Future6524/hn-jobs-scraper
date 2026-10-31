"""Command-line entry point: python -m hn_jobs_scraper [options]."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .api import DEFAULT_CONCURRENCY, HNClient
from .export import push_to_google_sheet, to_dataframe, write_csv, write_xlsx
from .parser import JobPost, looks_like_job_post, parse_comment

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hn-jobs-scraper",
        description='Export the latest HN "Ask HN: Who is hiring?" thread to CSV/XLSX.',
    )
    p.add_argument("--keywords", help='Comma-separated keywords, e.g. "python,scraping,contract"')
    p.add_argument("--only-matches", action="store_true", help="Only export posts that match --keywords")
    p.add_argument("--remote-only", action="store_true", help="Only export remote (yes/hybrid) posts")
    p.add_argument("--thread-id", type=int, help="Use a specific thread id instead of the latest one")
    p.add_argument("--limit", type=int, help="Only process the first N top-level comments")
    p.add_argument("--out-dir", type=Path, default=Path("."), help="Output directory (default: .)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Max parallel requests (default: 20)")
    p.add_argument("--sheet-id", help="Google Sheet id to push rows to (needs GOOGLE_SHEETS_CREDENTIALS)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


async def scrape(args: argparse.Namespace, keywords: list[str]) -> tuple[dict, list[JobPost]]:
    async with HNClient(concurrency=args.concurrency) as client:
        with console.status("[bold]Finding the latest 'Who is hiring?' thread..."):
            if args.thread_id:
                story = await client.get_item(args.thread_id)
                thread = {"id": args.thread_id, "title": story.get("title", ""), "created_at": None}
            else:
                thread = await client.find_latest_hiring_thread()
                story = await client.get_item(thread["id"])

        kids: list[int] = (story or {}).get("kids", [])
        if args.limit:
            kids = kids[: args.limit]
        console.print(f"[green]✓[/] [bold]{thread['title']}[/] — {len(kids)} top-level comments")

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Fetching comments"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        with progress:
            task = progress.add_task("fetch", total=len(kids))
            items = await client.get_items(kids, on_done=lambda: progress.advance(task))

    order = {cid: i for i, cid in enumerate(kids)}
    items.sort(key=lambda it: order.get(it.get("id"), 0))
    posts = [
        parse_comment(it["text"], it["id"], it.get("time"), keywords)
        for it in items
        if it.get("text") and not it.get("deleted") and not it.get("dead")
    ]
    jobs = [p for p in posts if looks_like_job_post(p)]
    skipped = len(posts) - len(jobs)
    if skipped:
        console.print(f"[dim]Skipped {skipped} non-job comments (meta posts, replies, deleted)[/]")
    return thread, jobs


def print_summary(thread: dict, all_posts: list[JobPost], exported: list[JobPost], keywords: list[str], files: list[str]) -> None:
    remote_yes = sum(p.remote == "yes" for p in all_posts)
    hybrid = sum(p.remote == "hybrid" for p in all_posts)
    with_salary = sum(bool(p.salary) for p in all_posts)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("Thread", f"{thread['title']} (id {thread['id']})")
    table.add_row("Total posts", str(len(all_posts)))
    table.add_row("Remote", str(remote_yes))
    table.add_row("Hybrid", str(hybrid))
    table.add_row("With salary info", str(with_salary))
    if keywords:
        matches = sum(p.match == "yes" for p in all_posts)
        table.add_row(f"Matches ({', '.join(keywords)})", f"[bold green]{matches}[/]")
    table.add_row("Rows exported", str(len(exported)))
    for f in files:
        table.add_row("Saved", f)
    console.print(Panel(table, title="[bold]Summary", border_style="green", expand=False))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]

    try:
        thread, posts = asyncio.run(scrape(args, keywords))
    except Exception as exc:  # network or API failure
        console.print(f"[bold red]Error:[/] {exc}")
        return 1

    exported = posts
    if args.only_matches and keywords:
        exported = [p for p in exported if p.match == "yes"]
    if args.remote_only:
        exported = [p for p in exported if p.remote in ("yes", "hybrid")]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df = to_dataframe(exported)
    csv_path = write_csv(df, args.out_dir / "jobs.csv")
    xlsx_path = write_xlsx(df, args.out_dir / "jobs.xlsx")
    files = [str(csv_path), str(xlsx_path)]

    if args.sheet_id:
        if not os.environ.get("GOOGLE_SHEETS_CREDENTIALS"):
            console.print("[yellow]--sheet-id given but GOOGLE_SHEETS_CREDENTIALS is not set; skipping Google Sheets.")
        else:
            try:
                with console.status("Pushing rows to Google Sheets..."):
                    files.append(push_to_google_sheet(df, args.sheet_id))
            except Exception as exc:
                console.print(f"[bold red]Google Sheets push failed:[/] {exc}")

    print_summary(thread, posts, exported, keywords, files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
