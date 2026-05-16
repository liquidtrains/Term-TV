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
- EPG grid TV guide in your browser at `http://127.0.0.1:8080`
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
| `playlists[].epg_url` | No | HTTP URL to XMLTV file (`.xml` or `.xml.gz`) — needed for show search |
| `vpn_ip` | No | Auto-confirms VPN check when public IP matches |
| `openvpn` | No | Term-TV-VPN.py only — omit entire block if not using |

> **Windows paths in JSON**: use double backslashes: `"C:\\Users\\name\\profile.ovpn"`

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

Opens `http://127.0.0.1:8080` automatically. Press `Ctrl+C` to stop.

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
├── config.json             # Your config (gitignored — keep private)
├── config.example.json     # Safe template to copy from
├── .gitignore
├── README.md
└── LICENSE

# Auto-generated at runtime (gitignored):
.watch_history.json         # Watch counts and total duration per channel
.search_history.json        # Last 5 successful searches
.epg_cache/                 # Cached EPG files (hash-named, cleaned after 15 days)
.m3u_cache/                 # Cached M3U playlists (same)
term-tv.log                 # DEBUG-level app log
term-tv-web.log             # Web UI log
mpv-output.log              # Full mpv stdout/stderr per session (archived on exit)
mpv-log-archive/            # LZMA-compressed mpv log archives (auto-rotated, 1yr retention)
openvpn.log                 # VPN connection log (VPN version only)
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
Hyper-V reserves large swaths of high ports. The web UI uses port 8080 which is outside all known exclusion ranges. If you still get a socket permission error, check `netsh int ipv4 show excludedportrange protocol=tcp` and update `WEB_PORT` in `Term-TV-Web.py` to a free port.

---

## Credits

Fork of [pasiegel/Term-TV](https://github.com/pasiegel/Term-TV).  
Built with Python 3, `requests`, `flask`, and `mpv`.
