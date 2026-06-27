# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Term-TV is a personal customization of [pasiegel/Term-TV](https://github.com/pasiegel/Term-TV) — a command-line IPTV player written in Python. It loads M3U playlists, parses XMLTV EPG data, and launches streams in `mpv`.

There is no build system, no test suite, and no package management beyond a single dependency (`requests`). Everything runs as a standalone Python script.

## Running the App

```bash
# Standard version
python Term-TV.py

# VPN-integrated version (requires OpenVPN + admin/root)
python Term-TV-VPN.py          # Windows (run as Administrator)
sudo python Term-TV-VPN.py     # Linux/macOS
```

Install the only required dependency:
```bash
pip install requests
```

External tools required on PATH: `mpv` (required), `ffmpeg` (optional, for subtitle extraction from recordings).

## Architecture

The app consists of a shared library plus three entry-point scripts:

- **`lib/term_tv_core.py`** — Shared library (~950 lines). All functions common to both CLI scripts live here: M3U/EPG loading, caching, EPG parsing, search, watch history, scheduling utilities, recording helpers.
- **`Term-TV.py`** — Main CLI application (~1500 lines). Imports all shared logic from `lib.term_tv_core`; adds script-specific functions: `setup_logging`, `archive_mpv_log`, `run_mpv_with_logging`, `play_channel`, `scheduled_playback_task`, `scheduled_recording_task`, `display_scheduled_tasks`, `select_from_*`, `main`.
- **`Term-TV-VPN.py`** — VPN-integrated CLI variant (~2050 lines). Same structure as Term-TV.py plus OpenVPN integration: `connect_vpn`, `disconnect_vpn`, `toggle_vpn_menu`, `detect_conflicting_vpn_processes`, `_register_vpn_signal_handlers`.
- **`Term-TV-Web.py`** — Browser-based UI (Flask, ~2800 lines). Self-contained except for `parse_epg_time` and `get_safe_filename` imported from lib; has its own web-specific search (`search_shows_web`), EPG loading, and recording functions.

### Data Flow

1. Load `config.json` → user selects playlist (15s countdown auto-selects playlist 1)
2. VPN check: fetches public IP, compares to `vpn_ip` in config
3. Download + cache M3U → parse channels into `List[Channel]` (dicts with `tvg-id`, `name`, `url`, `group-title`)
4. Download + cache EPG XML/GZ → parse into `EpgData` (`Dict[channel_id, List[program_dicts]]`)
5. Main menu loop: show frequent channels, show scheduled tasks, accept user input
6. User searches shows or channels → selects result → watch/record/schedule

### Key Function Groups

| Group | Functions |
|---|---|
| Startup | `setup_logging`, `check_vpn_status`, `input_with_countdown` |
| M3U loading | `load_m3u`, `load_m3u_cached` |
| EPG loading/parsing | `load_epg`, `parse_epg_time`, `is_new_episode` |
| Search | `search_shows_in_timeframe`, `search_channels`, `find_alternative_streams`, `find_future_reruns` |
| Playback | `play_channel`, `run_mpv_with_logging` |
| Scheduling | `scheduled_playback_task`, `scheduled_recording_task`, `display_scheduled_tasks` |
| Watch history | `log_channel_watch`, `load_watch_history`, `get_frequent_channels` |
| Search history | `load_search_history`, `save_search_history`, `add_to_search_history` |
| UI | `select_from_show_results`, `select_from_channel_list`, `display_frequent_channels` |
| Cache cleanup | `clean_old_cache_files` (runs via `atexit`, removes files >15 days old) |

### Caching

Both M3U and EPG use the same hash-based caching pattern stored in `.m3u_cache/` and `.epg_cache/`:
- URL is hashed → used as filename
- Conditional HTTP (ETag/Last-Modified) avoids re-downloads
- `.meta` sidecar file stores headers for conditional requests
- Falls back to cache on network errors

### Scheduling

Scheduled tasks (future playback or recording) run in background `threading.Thread`s. A global list tracks pending tasks displayed in the main menu. The terminal must stay open for scheduled tasks to fire.

### Retry Logic for Failed Streams

`play_channel` implements 5-tier automatic failover:
1. Retry original URL once
2. Find same episode on alternative channels (`find_alternative_streams`)
3. Try each alternative channel
4. Search for future reruns in next 24h (`find_future_reruns`)
5. Auto-reschedule if a rerun is found

## Configuration (`config.json`)

```json
{
  "vpn_ip": "...",         // Optional: auto-confirms VPN check if IP matches
  "openvpn": {             // Optional: Term-TV-VPN.py only
    "enabled": true,
    "executable": "",      // Auto-detected if empty
    "config_file": "C:\\path\\to\\profile.ovpn",
    "auto_connect": true
  },
  "playlists": [
    {
      "name": "Label",
      "m3u_url": "http://...",
      "epg_url": "http://..."   // Optional but needed for show search
    }
  ]
}
```

Use double backslashes for Windows paths in JSON.

## Log Files

| File | Contents |
|---|---|
| `term-tv.log` | DEBUG-level app logs (all functions, line numbers) |
| `mpv-output.log` | Full mpv stdout/stderr + exit codes per session |
| `openvpn.log` | VPN connection log (Term-TV-VPN.py, Windows only) |

Console only shows WARNING+ to keep the UI clean.

## Auto-Generated Files

These are created at runtime and should stay in `.gitignore`:
- `.watch_history.json` — watch counts + total duration per channel
- `.search_history.json` — last 5 successful searches
- `.epg_cache/` — cached EPG files
- `.m3u_cache/` — cached M3U playlists
- `~/Videos/Recordings/` — recorded streams + extracted SRT subtitles

## EPG Robustness Notes

The EPG parser handles malformed feeds from misconfigured servers:
- Lenient gzip: falls back to `zlib` when gzip footer is missing
- Incremental XML: uses `iterparse` to recover data from truncated XML
- Extended timeouts: 10s connect + 120s read

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`liquidtrains/Term-TV`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the root. See `docs/agents/domain.md`.
