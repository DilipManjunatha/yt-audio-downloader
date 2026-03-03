#!/usr/bin/env python3
"""
yt_audio_downloader.py
─────────────────────────────────────────────────────────────────────────────
A robust, parallel YouTube audio downloader built on top of yt-dlp.

Features
────────
• Reads every setting from config.json  (auto-generates a template if absent)
• Parallel downloads via ThreadPoolExecutor
• Playlists are saved in  <download_dir>/<Playlist Title>/
  Single videos land flat in  <download_dir>/
• Pre-flight cleanup removes stale .part / .ytdl files before each run
  (runs on the root dir AND every discovered playlist sub-folder)
• Per-folder .archive.txt prevents re-downloading
• Graceful error handling for bad JSON, missing FFmpeg, and yt-dlp failures

Usage
─────
    python yt_audio_downloader.py                   # uses ./config.json
    python yt_audio_downloader.py --config my.json  # custom config path

Folder layout produced
──────────────────────
downloads/
├── .archive.txt                   ← archive for bare single-video URLs
├── My Favourite Playlist/
│   ├── .archive.txt               ← archive scoped to this playlist
│   ├── Song One.mp3
│   └── Song Two.mp3
├── Another Playlist/
│   ├── .archive.txt
│   └── Track.mp3
└── Some Single Video.mp3
"""

import argparse
import json
import logging
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── third-party (pip install yt-dlp) ─────────────────────────────────────────
try:
    import yt_dlp
    from yt_dlp.utils import DownloadError, ExtractorError
except ImportError:
    sys.exit(
        "[FATAL] yt-dlp is not installed.\n"
        "        Run:  pip install yt-dlp\n"
        "        Then re-run this script."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yt-audio")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG_PATH = Path("config.json")
ARCHIVE_FILENAME    = ".archive.txt"
STALE_EXTENSIONS    = ("*.part", "*.ytdl")

# OS-illegal / problematic characters for folder names
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CONFIG_TEMPLATE = {
    "_comment": "Delete this file to regenerate. All keys below are required unless marked optional.",
    "urls": [
        "https://www.youtube.com/watch?v=EXAMPLE_VIDEO_ID",
        "https://www.youtube.com/playlist?list=EXAMPLE_PLAYLIST_ID"
    ],
    "download_directory": "./downloads",
    "thread_count": 3,
    "audio_format": "mp3",
    "audio_quality": "192",
    "preflight_cleanup": True,
    "yt_dlp_extra_args": {}
}


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def generate_template(path: Path) -> None:
    """Write a starter config.json and exit."""
    with path.open("w", encoding="utf-8") as fh:
        json.dump(CONFIG_TEMPLATE, fh, indent=4)
    log.info("Config template written → %s", path)
    log.info("Edit the file, then re-run the script.")


def load_config(path: Path) -> dict:
    """Load and validate config.json; exit with a clear message on any error."""
    if not path.exists():
        log.warning("Config file not found: %s", path)
        generate_template(path)
        sys.exit(0)

    try:
        with path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(
            f"[FATAL] Invalid JSON in {path}:\n"
            f"        Line {exc.lineno}, column {exc.colno}: {exc.msg}\n"
            f"        Fix the syntax error and re-run."
        )

    required = ("urls", "download_directory", "thread_count")
    missing  = [k for k in required if k not in cfg]
    if missing:
        sys.exit(
            f"[FATAL] Missing required key(s) in {path}: {missing}\n"
            f"        Delete the file to regenerate the template."
        )

    if not isinstance(cfg["urls"], list) or not cfg["urls"]:
        sys.exit("[FATAL] 'urls' must be a non-empty list.")
    if not isinstance(cfg["thread_count"], int) or cfg["thread_count"] < 1:
        sys.exit("[FATAL] 'thread_count' must be a positive integer.")

    cfg.setdefault("audio_format",      "mp3")
    cfg.setdefault("audio_quality",     "192")
    cfg.setdefault("preflight_cleanup", True)
    cfg.setdefault("yt_dlp_extra_args", {})
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem helpers
# ─────────────────────────────────────────────────────────────────────────────

def sanitise_dirname(name: str) -> str:
    """Replace OS-illegal chars; fall back to 'Unknown_Playlist' if empty."""
    cleaned = _UNSAFE_CHARS.sub("_", name).strip(". ")
    return cleaned or "Unknown_Playlist"


def preflight_cleanup(directory: Path) -> int:
    """Delete *.part and *.ytdl files in *directory*; return count removed."""
    removed = 0
    for pattern in STALE_EXTENSIONS:
        for stale in directory.glob(pattern):
            try:
                stale.unlink()
                log.debug("  stale removed: %s", stale)
                removed += 1
            except OSError as exc:
                log.warning("Could not remove %s: %s", stale, exc)
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg
# ─────────────────────────────────────────────────────────────────────────────

def check_ffmpeg() -> bool:
    """Return True if FFmpeg is on PATH; log a non-fatal warning otherwise."""
    if shutil.which("ffmpeg") is None:
        log.warning(
            "FFmpeg not found on PATH.\n"
            "         Audio conversion and metadata tagging will be DISABLED.\n"
            "         Install FFmpeg: https://ffmpeg.org/download.html"
        )
        return False
    log.info("FFmpeg detected ✓")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# URL probe — resolve playlist title without downloading
# ─────────────────────────────────────────────────────────────────────────────

def probe_url(url: str) -> dict:
    """
    Fetch metadata only (no download) to discover whether *url* is a playlist
    and — if so — what its title is.

    Returns
    ───────
    {
        "is_playlist":    bool,
        "playlist_title": str | None,
        "entry_count":    int | None,
    }
    """
    probe_opts = {
        "quiet":          True,
        "no_warnings":    True,
        "extract_flat":   "in_playlist",  # list entries but don't recurse
        "skip_download":  True,
        "ignoreerrors":   True,
        "playlist_items": "1",            # only need the container metadata
    }
    result = {"is_playlist": False, "playlist_title": None, "entry_count": None}

    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            meta = ydl.extract_info(url, download=False)

        if not meta:
            return result

        if meta.get("_type") == "playlist" or "entries" in meta:
            result["is_playlist"]    = True
            result["playlist_title"] = (
                meta.get("title") or meta.get("playlist_title") or None
            )
            entries = list(meta.get("entries") or [])
            result["entry_count"] = meta.get("playlist_count") or len(entries)

    except Exception as exc:  # noqa: BLE001  – probe errors are non-fatal
        log.debug("Probe error for %s: %s", url, exc)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp options builder
# ─────────────────────────────────────────────────────────────────────────────

def build_ydl_opts(
    cfg:          dict,
    dest_dir:     Path,
    archive_path: Path,
    ffmpeg_ok:    bool,
) -> dict:
    """
    Build the yt-dlp options dict.

    The caller is responsible for setting *dest_dir* to the correct folder
    (playlist subfolder or root).  This function simply points yt-dlp at it.
    """
    postprocessors = []
    if ffmpeg_ok:
        postprocessors.append({
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   cfg["audio_format"],
            "preferredquality": str(cfg["audio_quality"]),
        })
        postprocessors.append({"key": "FFmpegMetadata"})

    opts = {
        "outtmpl":          str(dest_dir / "%(title)s.%(ext)s"),
        "format":           "bestaudio/best",
        "download_archive": str(archive_path),
        "postprocessors":   postprocessors,
        "noplaylist":       False,   # allow playlists; single URLs still work
        "quiet":            False,
        "no_warnings":      False,
        "writethumbnail":   False,
        "ignoreerrors":     True,    # skip unavailable videos within a playlist
    }
    # User overrides take precedence
    opts.update(cfg.get("yt_dlp_extra_args", {}))
    return opts


# ─────────────────────────────────────────────────────────────────────────────
# Per-URL download task  (runs inside a thread-pool worker)
# ─────────────────────────────────────────────────────────────────────────────

def download_url(
    url:        str,
    cfg:        dict,
    root_dir:   Path,
    ffmpeg_ok:  bool,
    do_cleanup: bool,
) -> dict:
    """
    1. Probe the URL to detect playlist vs single video.
    2. Resolve/create the correct destination folder.
    3. Optionally run pre-flight cleanup on that folder.
    4. Download with a per-folder archive.

    Returns
    ───────
    {"url": str, "status": "ok"|"error", "dest": Path, "message": str}
    """
    result: dict = {"url": url, "status": "ok", "dest": root_dir, "message": ""}

    # ── 1. Probe ──────────────────────────────────────────────────────────────
    log.info("Probing  : %s", url)
    info = probe_url(url)

    if info["is_playlist"] and info["playlist_title"]:
        folder_name = sanitise_dirname(info["playlist_title"])
        dest_dir    = root_dir / folder_name
        log.info(
            "Playlist : \"%s\"  (%s tracks)  →  %s/",
            info["playlist_title"],
            info["entry_count"] if info["entry_count"] is not None else "?",
            folder_name,
        )
    elif info["is_playlist"]:
        # Playlist but title unknown — use a safe fallback
        dest_dir = root_dir / "Unknown_Playlist"
        log.warning(
            "Playlist title could not be determined for %s\n"
            "           Saving to: %s/",
            url, dest_dir.name,
        )
    else:
        dest_dir = root_dir
        log.info("Single   : %s  →  (root)", url)

    result["dest"] = dest_dir

    # ── 2. Create folder ──────────────────────────────────────────────────────
    dest_dir.mkdir(parents=True, exist_ok=True)

    # ── 3. Pre-flight cleanup ─────────────────────────────────────────────────
    if do_cleanup:
        n = preflight_cleanup(dest_dir)
        if n:
            log.info("  Cleaned %d stale file(s) from: %s", n, dest_dir)

    # ── 4. Per-folder archive path ────────────────────────────────────────────
    archive_path = dest_dir / ARCHIVE_FILENAME

    # ── 5. Build yt-dlp opts & download ──────────────────────────────────────
    ydl_opts = build_ydl_opts(
        cfg,
        dest_dir     = dest_dir,
        archive_path = archive_path,
        ffmpeg_ok    = ffmpeg_ok,
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ret = ydl.download([url])
        if ret != 0:
            result["status"]  = "error"
            result["message"] = f"yt-dlp exited with code {ret}"
    except DownloadError as exc:
        result["status"]  = "error"
        result["message"] = str(exc)
    except ExtractorError as exc:
        result["status"]  = "error"
        result["message"] = f"Extractor error: {exc}"
    except Exception as exc:  # noqa: BLE001
        result["status"]  = "error"
        result["message"] = f"Unexpected error: {exc}"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(config_path: Path) -> None:
    log.info("=" * 64)
    log.info("  yt-audio-downloader  |  config: %s", config_path)
    log.info("=" * 64)

    cfg        = load_config(config_path)
    urls       = cfg["urls"]
    root_dir   = Path(cfg["download_directory"]).expanduser().resolve()
    threads    = cfg["thread_count"]
    do_cleanup = cfg["preflight_cleanup"]

    log.info("Root download dir  : %s", root_dir)
    log.info("URLs to process    : %d", len(urls))
    log.info("Worker threads     : %d", threads)
    log.info("Audio format       : %s @ %s kbps", cfg["audio_format"], cfg["audio_quality"])
    log.info("Pre-flight cleanup : %s", "enabled" if do_cleanup else "disabled")

    # Ensure root exists and clean it too
    root_dir.mkdir(parents=True, exist_ok=True)
    if do_cleanup:
        n = preflight_cleanup(root_dir)
        log.info("Root cleanup removed %d stale file(s).", n)

    ffmpeg_ok = check_ffmpeg()

    log.info("-" * 64)
    log.info("Dispatching %d URL(s) across %d thread(s)…", len(urls), threads)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(download_url, url, cfg, root_dir, ffmpeg_ok, do_cleanup): url
            for url in urls
        }
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            if res["status"] == "ok":
                log.info("✓  Done   : %s  →  %s", res["url"], res["dest"])
            else:
                log.error("✗  Failed : %s\n           %s", res["url"], res["message"])

    ok     = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")

    log.info("=" * 64)
    log.info("  Summary  →  OK: %d  |  Failed: %d  |  Total: %d", ok, failed, len(results))
    log.info("=" * 64)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Parallel YouTube audio downloader — playlists get their own folder."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.json  (default: ./config.json)",
    )
    main(parser.parse_args().config)
