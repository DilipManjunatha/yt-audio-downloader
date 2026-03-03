#!/usr/bin/env python3
"""
yt_audio_mcp – MCP server that exposes yt-audio-downloader as an MCP tool.

Tool exposed
────────────
• download_audio(urls, config_path?)
    Download audio from one or more YouTube video or playlist URLs and return
    a plain-text summary of what succeeded / failed.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── locate project root so we can import the standalone downloader ────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from yt_audio_downloader import (  # noqa: E402
    check_ffmpeg,
    download_url,
    load_config,
    preflight_cleanup,
)

DEFAULT_CONFIG = _PROJECT_ROOT / "config.json"

mcp = FastMCP("yt-audio-mcp")


@mcp.tool()
def download_audio(
    urls: list[str],
    config_path: str = "",
) -> str:
    """
    Download audio from one or more YouTube video or playlist URLs.

    :param urls:        One or more YouTube video or playlist URLs.
    :param config_path: Absolute path to config.json.
                        Defaults to config.json at the project root.
    :return: A plain-text summary of succeeded and failed downloads.
    """
    if not urls:
        return "No URLs provided."

    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not cfg_path.exists():
        return (
            f"Config file not found: {cfg_path}\n"
            "Run `python yt_audio_downloader.py` once to generate a template, "
            "then edit it with your settings and re-try."
        )

    # load_config may call sys.exit() on bad JSON / missing keys — catch that
    try:
        cfg = load_config(cfg_path)
    except SystemExit as exc:
        return f"Config error: {exc}"

    # Override the URL list with whatever the caller supplied
    cfg["urls"] = urls

    root_dir = Path(cfg["download_directory"]).expanduser().resolve()
    threads = cfg["thread_count"]
    do_cleanup = cfg["preflight_cleanup"]
    ffmpeg_ok = check_ffmpeg()

    root_dir.mkdir(parents=True, exist_ok=True)
    if do_cleanup:
        preflight_cleanup(root_dir)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(download_url, url, cfg, root_dir, ffmpeg_ok, do_cleanup): url
            for url in urls
        }
        for future in as_completed(futures):
            results.append(future.result())

    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "error"]

    lines = [f"Downloads complete — OK: {len(ok)}  |  Failed: {len(failed)}"]
    for r in ok:
        lines.append(f"  ✓  {r['url']}  →  {r['dest']}")
    for r in failed:
        lines.append(f"  ✗  {r['url']}  —  {r['message']}")

    return "\n".join(lines)


def main() -> None:
    """Entry point — starts the MCP server over stdio."""
    mcp.run()
