# yt-audio-downloader

A robust, parallel YouTube audio downloader built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), with an optional **MCP (Model Context Protocol) server** so AI assistants like Claude can trigger downloads directly.

---

## Features

- **Parallel downloads** via `ThreadPoolExecutor` — configure the thread count to your liking
- **Smart folder layout** — playlists get their own sub-folder named after the playlist title; single videos land in the root download directory
- **Per-folder archives** — `.archive.txt` per folder prevents re-downloading tracks you already have
- **Pre-flight cleanup** — removes stale `.part` / `.ytdl` files before each run
- **FFmpeg integration** — audio conversion (MP3, AAC, FLAC, …) and metadata tagging when FFmpeg is on `PATH`; gracefully degrades when it is absent
- **Config-driven** — all settings live in `config.json`; a template is auto-generated on first run
- **MCP server** — expose download functionality as an MCP tool so any MCP-compatible client (Claude Desktop, etc.) can queue downloads

---

## Project Structure

```
yt-audio-downloader/
├── yt_audio_downloader.py   # standalone CLI downloader
├── config.json              # your personal settings (git-ignored — auto-generated)
└── yt_audio_mcp/            # MCP server package
    ├── __init__.py
    ├── __main__.py
    ├── pyproject.toml
    └── setup.cfg
```

---

## Requirements

| Requirement | Notes |
|---|---|
| Python ≥ 3.10 | |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | `pip install yt-dlp` |
| [FFmpeg](https://ffmpeg.org/download.html) | Optional — needed for audio conversion & metadata |
| [mcp](https://pypi.org/project/mcp/) | Only for the MCP server: `pip install mcp` |

---

## Installation

```bash
# Clone
git clone https://github.com/DilipManjunatha/yt-audio-downloader.git
cd yt-audio-downloader

# Install dependencies
pip install yt-dlp

# (Optional) Install MCP server dependencies
pip install mcp
```

---

## Quick Start — CLI

```bash
python yt_audio_downloader.py
```

On the **first run**, if `config.json` is missing, a template is generated for you:

```json
{
    "urls": [
        "https://www.youtube.com/watch?v=EXAMPLE_VIDEO_ID",
        "https://www.youtube.com/playlist?list=EXAMPLE_PLAYLIST_ID"
    ],
    "download_directory": "./downloads",
    "thread_count": 3,
    "audio_format": "mp3",
    "audio_quality": "192",
    "preflight_cleanup": true,
    "yt_dlp_extra_args": {}
}
```

Edit `config.json` with your URLs, then re-run. You can also point to a custom config file:

```bash
python yt_audio_downloader.py --config /path/to/my.json
```

### Config Reference

| Key | Type | Description |
|---|---|---|
| `urls` | `list[str]` | YouTube video or playlist URLs to download |
| `download_directory` | `str` | Root folder for downloads (created if absent) |
| `thread_count` | `int` | Number of parallel download workers |
| `audio_format` | `str` | Output format: `mp3`, `aac`, `flac`, `opus`, … (requires FFmpeg) |
| `audio_quality` | `str` | Bitrate in kbps, e.g. `"192"` |
| `preflight_cleanup` | `bool` | Remove `.part`/`.ytdl` stale files before downloading |
| `yt_dlp_extra_args` | `dict` | Any extra [yt-dlp options](https://github.com/yt-dlp/yt-dlp#usage-and-options) merged verbatim |

---

## Folder Layout

```
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
```

---

## MCP Server

The `yt_audio_mcp` package exposes the downloader as an **MCP tool** so AI assistants can queue downloads programmatically.

### Install

```bash
pip install -e ./yt_audio_mcp
```

### Run directly

```bash
python -m yt_audio_mcp
# or, after pip install:
yt-audio-mcp
```

### Configure Claude Desktop

Add the following to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yt-audio-downloader": {
      "command": "python",
      "args": ["-m", "yt_audio_mcp"],
      "cwd": "/absolute/path/to/yt-audio-downloader"
    }
  }
}
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
