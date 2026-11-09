# Term-TV

A personal fork of [pasiegel/Term-TV](https://github.com/pasiegel/Term-TV) — a command-line IPTV player written in Python. Loads M3U playlists, parses XMLTV EPG data, and launches streams in `mpv`.

Three ways to run it:

| Script | Description |
|---|---|
| `Term-TV.py` | Standard CLI |
| `Term-TV-VPN.py` | CLI with OpenVPN auto-connect |
| `Term-TV-Web.py` | Browser-based TV guide (Flask) |

---

## Features

### All versions
- **Show/movie search** — find content airing now or within 1–9 hours across all channels
- **Channel search** — browse by name with live EPG preview
- **Frequently watched** — quick-launch top 3 channels by total watch time
- **Now-playing from searches** — main menu surfaces currently-airing matches from your recent searches
- **Search history** — re-run any of your last 5 searches in one keystroke
- **Scheduled playback** — auto-launch mpv when a future show starts
- **Scheduled recording** — record future shows unattended
- **5-tier stream failover** — retries original URL → alternative channels → future reruns → auto-reschedule
- **Smart caching** — M3U and EPG use ETag/Last-Modified; only re-downloads when changed
- **Robust EPG parsing** — handles truncated XML and malformed gzip from misconfigured servers
- **Auto cache cleanup** — removes M3U/EPG cache files older than 15 days on exit
- **Subtitle extraction** — ffmpeg extracts subtitle tracks from recordings to SRT files
- **VPN check** — shows public IP on launch; auto-confirms if it matches `vpn_ip` in config

### Term-TV-VPN.py extras
- Auto-connects OpenVPN on startup, disconnects on exit (Ctrl+C or window close)
- Auto-detects OpenVPN executable on Windows, macOS, and Linux
- Detects and optionally kills conflicting VPN software before connecting
- Requires admin/root privileges (OpenVPN kernel-level networking)

### Term-TV-Web.py extras
- EPG grid TV guide in your browser at `http://127.0.0.1:8891`
- Click any programme → launches in mpv
- VOD mode with poster grid and A→Z / Group sort
- Stop mpv or cancel recordings from the browser
- Live VPN status indicator
- Background data loading with loading state indicators

---

## Prerequisites

- **Python 3.8+**
- **mpv** — required for playback
  - macOS: `brew install mpv`
  - Linux: `sudo apt install mpv`
  - Windows: [mpv.io/installation](https://mpv.io/installation/) — add to PATH
- **ffmpeg** *(optional)* — subtitle extraction from recordings
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
  - Windows: [ffmpeg.org/download](https://ffmpeg.org/download.html) — add to PATH
- **OpenVPN** *(Term-TV-VPN.py only)*
  - macOS: `brew install openvpn`
  - Linux: `sudo apt install openvpn`
  - Windows: [openvpn.net/community-downloads](https://openvpn.net/community-downloads/)

Python packages are auto-installed on first run (`requests`, `flask`).

---

## Installation

```bash
git clone https://github.com/liquidtrains/Term-TV.git
cd Term-TV
cp config.example.json config.json
# edit config.json with your playlist URLs
```

No build step. Run directly with Python.

---

## Configuration

Copy `config.example.json` to `config.json` and fill in your details:

```json
{
  "vpn_ip": "",
  "recordings_dir": "~/Videos/Recordings",
  "web_port": 8891,
  "epg_refresh_hours": 6,
  "openvpn": {
    "enabled": false,
    "executable": "",
    "config_file": "/path/to/profile.ovpn",
    "auto_connect": false
  },
  "playlists": [
    {
      "name": "My Provider",
      "m3u_url": "http://your-provider.example/playlist.m3u",
      "epg_url": "http://your-provider.example/epg.xml.gz"
    },
    {
      "name": "Public (iptv-org)",
      "m3u_url": "https://iptv-org.github.io/iptv/index.m3u"
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `playlists[].name` | Yes | Label shown at startup |
| `playlists[].m3u_url` | Yes | HTTP URL to M3U playlist |
| `playlists[].epg_url` | No | HTTP URL to XMLTV file (`.xml` or `.xml.gz`) — needed for show search. Feed suffixes are honoured: ids that differ only in picture quality (`CNN.us@SD`/`@HD`/`CNN.us`) share listings, while a different time zone or station (`HBO.us@West` vs `@East`) never does |
| `playlists[].disabled` | No | `true` parks a source (e.g. temporarily offline) with its URLs intact — it is skipped instead of deleted |
| `recordings_dir` | No | Where recordings are written (default `~/Videos/Recordings`) |
| `vpn_ip` | No | Auto-confirms VPN check when public IP matches |
| `web_port` | No | Term-TV-Web.py port (default `8891`) |
| `epg_refresh_hours` | No | Term-TV-Web.py guide refresh interval (default `6`) |
| `tmdb_api_key` | No | Term-TV-Web.py only — enables episode-level IMDb links in the popup |
| `openvpn` | No | Term-TV-VPN.py only — omit entire block if not using |

> **Windows paths in JSON**: use double backslashes: `"C:\\Users\\name\\profile.ovpn"`

---

## Runtime data

Caches, logs and JSON state are kept **outside the source tree**, so a
cloud-synced checkout (Google Drive, OneDrive, Dropbox) does not re-upload the
EPG cache and append-only logs on every run:

| Platform | Default location |
|---|---|
| Windows | `%LOCALAPPDATA%\Term-TV` |
| macOS / Linux | `$XDG_CACHE_HOME/term-tv` (or `~/.cache/term-tv`) |

Set `TERM_TV_DATA_DIR` to put it anywhere else:

```bash
setx TERM_TV_DATA_DIR "D:\Term-TV-data"     # Windows
export TERM_TV_DATA_DIR="$HOME/tv-data"     # macOS / Linux
```

It holds `.epg_cache/`, `.m3u_cache/`, the logs (`term-tv.log`, `mpv-output.log`,
`openvpn.log`), `mpv-log-archive/`, and the small state files
(`.watch_history.json`, `.favorites.json`, …). Older versions kept these next to
the scripts; the first run moves them automatically. `config.json` and the
recordings directory are unaffected.

---

## Self-hosted EPG (US/CA)

Public XMLTV endpoints routinely drop or rename the iptv-org channel ids that
the playlists use (`Buzzr.us`, `CBCTDT.ca`, …). [iptv-org/epg](https://github.com/iptv-org/epg)
is the open-source grabber built on that same id database, so
[`tools/update_epg.py`](tools/update_epg.py) runs it locally and writes a guide
the app reads straight from disk — no third-party EPG service.

Requirements: Node.js 18+ (for the grabber). The grabber checkout and npm cache
live in the runtime data directory above, never in the repository.

```bash
# Free-TV USA + Canada (the tool's defaults)
python tools/update_epg.py

# Broader US/CA coverage: also index the iptv-org playlists
python tools/update_epg.py ^
  --ids-from https://iptv-org.github.io/iptv/countries/us.m3u ^
  --ids-from https://iptv-org.github.io/iptv/countries/ca.m3u
```

The tool maps playlist `tvg-id`s to grabber sites, fetches the guide (typically
1–3 minutes), and prints the resulting path. Put it in a playlist's `epg_url`:

```json
"epg_url": "C:/Users/you/AppData/Local/Term-TV/guide/guide.xml.gz"
```

Feed suffixes are kept, because `HBO.us@East` and `HBO.us@West` (or two local
NBC stations) air different schedules. When a channel's own feed has no source,
the tool skips it and lists it rather than give it another feed's times. The
grabber only carries the network time-zone feeds for the big broadcast networks,
so local affiliates (`NBC.us@WNYW`) are skipped by default; `--loose-feeds` gives
them the network feed's schedule instead, which can be hours off.

`load_epg` accepts a local path, a `file://` URI, or an HTTP(S) URL. Re-run the
tool whenever the guide goes stale; `--days` controls how far ahead it fetches.

---

## Usage

### Term-TV.py — Standard CLI

```bash
python Term-TV.py
```

### Term-TV-VPN.py — VPN-integrated CLI

```bash
# Windows — run as Administrator
python Term-TV-VPN.py

# Linux / macOS
sudo python Term-TV-VPN.py
```

The VPN version auto-connects OpenVPN on startup. It will warn and exit if not run with elevated privileges. Add a `vpn` command at the main menu to toggle/reconnect mid-session.

### Term-TV-Web.py — Browser UI

```bash
python Term-TV-Web.py
```

Opens `http://127.0.0.1:8891` automatically. Press `Ctrl+C` to stop. Use `--port N` (or `web_port` in `config.json`) to change it.

---

## Main menu walkthrough (CLI)

```
================================================================================
FREQUENTLY WATCHED:
================================================================================
1. AMC HD [Provider] (1h 30m watched)
   NOW PLAYING: Breaking Bad (S03E05) - "Mas"

================================================================================
SCHEDULED TASKS:
================================================================================
▶ [PLAYBACK] In 25 min
   The Big Bang Theory (S04E12)
   Channel: TBS HD [Provider]

Options:
  1-3: Watch frequent channel
  s: Search for show/movie
  c: Search for channel
  epg: Refresh EPG data
  quit: Exit

Your choice (default: s):
```

#### Show search

```
Search for a show/movie: Breaking Bad
Search how many hours ahead? (1-9, default 3): 5

Found 3 result(s):
1. [ NOW PLAYING] 07:00 PM - Breaking Bad (S03E05) +++ ◄◄◄
   "Mas" - AMC HD [Provider]

2. [   In 45 min] 08:00 PM - Breaking Bad (S03E06)
   "Sunset" - AMC HD [Provider]

Select show to watch (1-3, or 'b' for back): 2

Options:
  p: Pop open when show starts (auto-watch)
  r: Schedule recording
  w: Watch channel now
  b: Back

Your choice (default: p): p
✓ Playback scheduled! Will auto-launch in 45 minutes
```

**Visual indicators:** `◄◄◄` = live now · `+++` = new episode (≤7 days) · `(S03E05)` = episode number

#### Recording options

When you select a channel (live or from search):

```
Recording options:
  w: Watch only (no recording)
  r: Record while watching
  s: Schedule recording for later
  b: Back
```

Recordings are saved to `~/Videos/Recordings/` as `.mkv` files. Subtitle tracks are automatically extracted to `.srt` after recording.

> **Subtitle note**: press `v` to toggle subtitle visibility while recording. Do not change subtitle tracks (`c` key) — it will stop the recording.

---

## File structure

```
Term-TV/
├── Term-TV.py              # Standard CLI
├── Term-TV-VPN.py          # CLI + OpenVPN integration
├── Term-TV-Web.py          # Browser UI (Flask)
├── lib/
│   └── term_tv_core.py     # Shared library (M3U, EPG, search, history, recording utils)
├── tools/
│   └── update_epg.py       # Build a self-hosted US/CA guide with iptv-org/epg
├── config.json             # Your config (gitignored — keep private)
├── config.example.json     # Safe template to copy from
├── .gitignore
├── README.md
└── LICENSE

# Runtime data — NOT in this folder (see "Runtime data" above):
%LOCALAPPDATA%\Term-TV\
├── .watch_history.json     # Watch counts and total duration per channel
├── .search_history.json    # Last 5 successful searches
├── .epg_cache/             # Cached EPG files (hash-named, cleaned after 15 days)
├── .m3u_cache/             # Cached M3U playlists (same)
├── term-tv.log             # DEBUG-level app log
├── term-tv-web.log         # Web UI log
├── mpv-output.log          # Full mpv stdout/stderr per session (archived on exit)
├── mpv-log-archive/        # LZMA-compressed mpv log archives (auto-rotated, 1yr retention)
├── openvpn.log             # VPN connection log (VPN version only)
├── guide/                  # Self-hosted XMLTV guide from tools/update_epg.py
└── epg-grabber/            # iptv-org/epg checkout (only if update_epg.py was run)

~/Videos/Recordings/        # Recorded streams + extracted SRT subtitles
```

---

## Troubleshooting

**mpv not found**
Install mpv and ensure it is on your system PATH. Term-TV-VPN.py will attempt `winget`/`brew`/`apt` auto-install and print manual instructions if it fails.

**Stream won't play**
Check `mpv-output.log` for the error. The app will automatically try alternative channels and future reruns before giving up.

**EPG errors (gzip / XML)**
Handled automatically. The parser uses `zlib` fallback for truncated gzip and `iterparse` to recover data from malformed XML. If EPG loads 0 programs the cache is cleared so the next run downloads fresh.

**Recording stops when changing subtitle track**
By design — mpv stops stream recording when the subtitle track changes. Use `v` to show/hide subtitles (safe), not `c` to switch tracks.

**VPN connection failed (Term-TV-VPN.py)**
1. Check `openvpn.log` for details
2. Ensure OpenVPN is installed
3. Verify the `.ovpn` path in `config.json`
4. Run as Administrator (Windows) or with `sudo` (Linux/macOS)

**Windows port in use (Term-TV-Web.py)**
Hyper-V reserves large swaths of high ports. The web UI defaults to port 8891 (outside the common exclusion ranges). If you still get a socket permission error, check `netsh int ipv4 show excludedportrange protocol=tcp` and pass `--port N` (or set `web_port` in `config.json`) to a free port.

---

## Credits

Fork of [pasiegel/Term-TV](https://github.com/pasiegel/Term-TV).  
Built with Python 3, `requests`, `flask`, and `mpv`.
