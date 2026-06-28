#!/usr/bin/env python3
"""
Term-TV Web  —  Browser-based IPTV TV Guide with MPV integration.

Run:    python Term-TV-Web.py
Opens:  http://localhost:8080

Features:
  • Fast EPG grid TV guide in your browser
  • Click a programme or channel → launches in mpv
  • Search / filter channels and shows
  • VPN auto-connect (reads same config.json as Term-TV-VPN.py)
  • Stop mpv from the browser
  • Live VPN status + current-time indicator
"""

import sys
import subprocess

# Force UTF-8 output on Windows (avoids CP1252 UnicodeEncodeError for emoji/symbols)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Dependency bootstrap ──────────────────────────────────────────────────────
def _ensure_deps():
    missing = []
    for pkg in [("requests", "requests"), ("flask", "flask")]:
        try:
            __import__(pkg[1])
        except ImportError:
            missing.append(pkg[0])
    if missing:
        print(f"Installing required packages: {', '.join(missing)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
        print()

_ensure_deps()

# ── Imports ───────────────────────────────────────────────────────────────────
import os
import re
import json
import lzma
import time
import logging
import argparse
import threading
import platform
import shutil
import webbrowser
import atexit
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request, Response
from lib.term_tv_core import (
    parse_epg_time, get_safe_filename,
    load_m3u_cached as _core_load_m3u_cached,
    load_epg as _core_load_epg,
    load_watch_history as _core_load_watch_history,
    get_public_ip as _core_get_public_ip,
    is_new_episode as _core_is_new_episode,
    send_desktop_notification,
)

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIG_FILE         = Path("config.json")
WATCH_HISTORY_FILE  = Path(".watch_history.json")
EPG_CACHE_DIR       = Path(".epg_cache")
M3U_CACHE_DIR       = Path(".m3u_cache")
LOG_FILE            = Path("term-tv-web.log")
MPV_LOG_FILE        = Path("mpv-output.log")
MPV_LOG_ARCHIVE_DIR = Path("mpv-log-archive")
MPV_LOG_CHUNK_SIZE  = 5 * 1024 * 1024  # 5 MB
RECORDINGS_DIR      = Path.home() / "Videos" / "Recordings"

_VOD_EXTS   = frozenset({'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'})
_VOD_GRP_RE = re.compile(
    r'\bvod\b|movies?\b|\bseries\b|films?\b|\bcinema\b|\banime\b|\bcartoons?\b|\bshows?\s*[-\u2013]',
    re.I
)

WEB_HOST = "127.0.0.1"
WEB_PORT = 8891  # 8079-8378 and 8485-8884 are excluded by Hyper-V on this machine; 8885-9080 is safe
WEB_URL  = f"http://{WEB_HOST}:{WEB_PORT}"

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging():
    fmt = "%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d  %(message)s"
    logging.basicConfig(
        level=logging.DEBUG,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    logging.getLogger().handlers[1].setLevel(logging.WARNING)
    logging.info("Term-TV Web started")

# ── Core data functions (delegated to lib.term_tv_core) ──────────────────────

def get_public_ip() -> Optional[str]:
    return _core_get_public_ip()


# B3 / Fix-6: Cached public IP (60s TTL, invalidated on VPN state change, lock prevents storm)
_cached_ip:     Optional[str]  = None
_cached_ip_ts:  float          = 0.0
_cached_ip_vpn: bool           = False
_cached_ip_lock: threading.Lock = threading.Lock()

def get_public_ip_cached() -> Optional[str]:
    global _cached_ip, _cached_ip_ts, _cached_ip_vpn
    now     = time.time()
    vpn_now = vpn_is_connected()
    with _cached_ip_lock:
        if _cached_ip is not None and (now - _cached_ip_ts) < 60 and vpn_now == _cached_ip_vpn:
            return _cached_ip
    # Cache miss — fetch outside lock so we don't block other callers
    new_ip = get_public_ip()
    with _cached_ip_lock:
        _cached_ip     = new_ip
        _cached_ip_ts  = now
        _cached_ip_vpn = vpn_now
    return new_ip


def load_m3u_cached(url: str) -> List[Dict]:
    return _core_load_m3u_cached(url)


def load_epg(url: str) -> Dict:
    return _core_load_epg(url, lookback_hours=6)


def load_watch_history() -> List[Dict]:
    return _core_load_watch_history()


# B4: Watch history cache (30s TTL) — avoids disk read on every /api/guide call
_watch_cache:      Dict           = {"data": [], "ts": 0.0}
_watch_cache_lock: threading.Lock = threading.Lock()

def load_watch_history_cached() -> List[Dict]:
    # Fix-4: Check cache under lock, but do file I/O outside it
    with _watch_cache_lock:
        if time.time() - _watch_cache["ts"] < 30:
            return _watch_cache["data"]
    data = load_watch_history()  # file I/O outside lock
    with _watch_cache_lock:
        _watch_cache["data"] = data
        _watch_cache["ts"]   = time.time()
    return data


def _is_vod_ch(ch: Dict) -> bool:
    """True when the channel is on-demand content (not a live stream)."""
    url = ch.get("url", "").lower().split("?")[0]
    if os.path.splitext(url)[1] in _VOD_EXTS:
        return True
    # V3: Only flag /series/ or /movie paths as VOD when they also have a VOD extension
    if "/series/" in url or "/movie" in url:
        return os.path.splitext(url)[1] in _VOD_EXTS
    return bool(_VOD_GRP_RE.search(ch.get("group-title", "")))


def is_new_episode(prog: Dict) -> bool:
    """True if the programme's air_date is within the last 7 days."""
    return _core_is_new_episode(prog.get("air_date", ""))


def search_shows_web(query: str, hours_ahead: int = 24, groups: Optional[set] = None) -> List[Dict]:
    """Search full EPG for shows airing now or within hours_ahead. Mirrors CLI search_shows_in_timeframe."""
    with _data_lock:
        channels = list(_channels)
        epg      = dict(_epg)

    query_lower = query.lower()
    now         = datetime.now().astimezone()
    cutoff      = now + timedelta(hours=hours_ahead)

    tvg_to_channels: Dict[str, List[Dict]] = defaultdict(list)
    for ch in channels:
        # S2: Respect group filter if provided
        if groups and ch.get("group-title", "") not in groups:
            continue
        tid = ch.get("tvg-id")
        if tid:
            tvg_to_channels[tid].append(ch)

    results: List[Dict] = []
    for channel_id, programs in epg.items():
        if channel_id not in tvg_to_channels:
            continue
        for prog in programs:
            title = prog.get("title", "")
            if query_lower not in title.lower():
                continue
            start_time = prog.get("start_time")
            stop_time  = prog.get("stop_time")
            if not start_time:
                continue
            is_now   = bool(stop_time and start_time <= now < stop_time)
            upcoming = (now <= start_time <= cutoff)
            if not (is_now or upcoming):
                continue

            mins = int((start_time - now).total_seconds() / 60)
            if is_now:
                time_status = "NOW"
            elif mins < 60:
                time_status = f"In {mins}m"
            else:
                h, m = divmod(mins, 60)
                time_status = f"In {h}h {m}m" if m else f"In {h}h"

            stop_ts = int(stop_time.timestamp()) if stop_time else 0
            dur_min = max(1, int((stop_time - start_time).total_seconds() / 60)) if stop_time else 0

            new_ep = is_new_episode(prog)
            for ch in tvg_to_channels[channel_id]:
                results.append({
                    "channel_name":  ch.get("name", ""),
                    "channel_url":   ch.get("url", ""),
                    "channel_group": ch.get("group-title", ""),
                    "title":         title,
                    "subtitle":      prog.get("subtitle", ""),
                    "description":   prog.get("description", ""),
                    "episode_num":   prog.get("episode_num", ""),
                    "start_ts":      int(start_time.timestamp()),
                    "stop_ts":       stop_ts,
                    "duration_min":  dur_min,
                    "time_status":   time_status,
                    "is_now":        is_now,
                    "is_new":        new_ep,
                })

    results.sort(key=lambda x: (not x["is_now"], x["start_ts"]))
    return results


# ── VPN management (reference: Term-TV-VPN.py) ───────────────────────────────

_vpn_process: Optional[subprocess.Popen] = None
_vpn_exe: Optional[str] = None
_vpn_config_file: Optional[str] = None
_vpn_expected_ip: Optional[str] = None
_win_console_handler_ref = None


def find_openvpn_executable(config_exe: str = "") -> Optional[str]:
    if config_exe and Path(config_exe).exists():
        return config_exe
    found = shutil.which("openvpn")
    if found:
        return found
    system = platform.system()
    if system == "Windows":
        candidates = [
            r"C:\Program Files\OpenVPN\bin\openvpn.exe",
            r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
            r"C:\Program Files\OpenVPN Connect\agents\openvpn.exe",
        ]
    elif system == "Darwin":
        candidates = ["/usr/local/sbin/openvpn", "/opt/homebrew/sbin/openvpn", "/usr/sbin/openvpn"]
    else:
        candidates = ["/usr/sbin/openvpn", "/usr/local/sbin/openvpn"]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def check_admin_privileges() -> bool:
    try:
        if platform.system() == "Windows":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.getuid() == 0
    except Exception:
        return False


def vpn_is_connected() -> bool:
    return _vpn_process is not None and _vpn_process.poll() is None


def connect_vpn(openvpn_exe: str, config_file: str, expected_ip: Optional[str] = None) -> bool:
    global _vpn_process, _vpn_exe, _vpn_config_file, _vpn_expected_ip
    _vpn_exe          = openvpn_exe
    _vpn_config_file  = config_file
    _vpn_expected_ip  = expected_ip

    config_path = Path(config_file)
    if not config_path.exists():
        print(f"OpenVPN config not found: {config_file}", file=sys.stderr)
        return False

    print(f"Connecting VPN: {config_path.name}")
    try:
        log_handle = open(MPV_LOG_FILE.parent / "openvpn.log", "a", encoding="utf-8", errors="replace")
        log_handle.write(f"\n{'='*60}\n[{datetime.now()}] OpenVPN started\n{'='*60}\n")
        log_handle.flush()

        kw: Dict = {"stdout": log_handle, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if platform.system() == "Windows":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        _vpn_process = subprocess.Popen([openvpn_exe, "--config", str(config_path)], **kw)
    except PermissionError:
        print("OpenVPN requires elevated privileges.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Failed to start OpenVPN: {e}", file=sys.stderr)
        return False

    print("Waiting for VPN", end="", flush=True)
    start   = time.time()
    timeout = 40
    while time.time() - start < timeout:
        print(".", end="", flush=True)
        time.sleep(2)
        if _vpn_process.poll() is not None:
            print(f"\nOpenVPN exited unexpectedly (code {_vpn_process.returncode}).", file=sys.stderr)
            return False
        ip = get_public_ip()
        if expected_ip and ip == expected_ip:
            print(f"\n✓ VPN Connected  (IP: {ip})")
            print("  VPN will auto-disconnect when this window is closed.")
            return True
        elif not expected_ip and time.time() - start >= 8:
            print(f"\n✓ VPN process running  (IP: {ip or 'unknown'})")
            print("  VPN will auto-disconnect when this window is closed.")
            return True

    print(f"\n⚠  VPN did not connect within {timeout}s.", file=sys.stderr)
    return False


def disconnect_vpn():
    global _vpn_process
    if _vpn_process is None or _vpn_process.poll() is not None:
        return
    print("\nDisconnecting VPN...")
    try:
        _vpn_process.terminate()
        _vpn_process.wait(timeout=5)
        print("✓ VPN disconnected.")
    except subprocess.TimeoutExpired:
        _vpn_process.kill()
    except Exception as e:
        logging.warning(f"VPN disconnect error: {e}")


def _register_vpn_signal_handlers():
    import signal

    def _on_signal(signum, frame):
        disconnect_vpn()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (OSError, AttributeError):
        pass

    if platform.system() == "Windows":
        import ctypes
        global _win_console_handler_ref

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        def _ctrl_handler(event):
            if event in (2, 5, 6):
                disconnect_vpn()
            return False

        _win_console_handler_ref = _ctrl_handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler, True)


# ── MPV playback management ───────────────────────────────────────────────────

_mpv_process: Optional[subprocess.Popen] = None
_mpv_log_handle = None
_mpv_info: Dict = {}
_mpv_lock = threading.Lock()


def launch_mpv(url: str, channel_name: str, show_title: str = "") -> bool:
    global _mpv_process, _mpv_info, _mpv_log_handle
    stop_mpv()

    mpv_args = [
        "mpv",
        "--stream-lavf-o=reconnect=1,reconnect_at_eof=1,reconnect_streamed=1,reconnect_delay_max=5,timeout=10000000",
        "--cache=yes",
        "--cache-pause=no",
        url,
    ]
    try:
        log_handle = open(MPV_LOG_FILE, "a", encoding="utf-8", errors="replace")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_handle.write(f"\n{'='*80}\n[{ts}] {channel_name}")
        if show_title:
            log_handle.write(f" — {show_title}")
        log_handle.write(f"\nCommand: {' '.join(mpv_args)}\n{'-'*80}\n")
        log_handle.flush()

        kw: Dict = {
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin":  subprocess.DEVNULL,
        }
        with _mpv_lock:
            _mpv_log_handle = log_handle
            _mpv_process    = subprocess.Popen(mpv_args, **kw)
            _mpv_info       = {
                "channel":    channel_name,
                "show":       show_title,
                "url":        url,
                "started_at": int(time.time()),
            }
        logging.info(f"Launched mpv: {channel_name} — {show_title}")
        return True
    except FileNotFoundError:
        logging.error("mpv not found")
        return False
    except Exception as e:
        logging.error(f"launch_mpv error: {e}")
        return False


def stop_mpv():
    global _mpv_process, _mpv_info, _mpv_log_handle
    with _mpv_lock:
        if _mpv_process and _mpv_process.poll() is None:
            _mpv_process.terminate()
            try:
                _mpv_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _mpv_process.kill()
        if _mpv_log_handle:
            try:
                _mpv_log_handle.write(f"{'='*80}\n[Stopped by web UI]\n")
                _mpv_log_handle.close()
            except Exception:
                pass
            _mpv_log_handle = None
        _mpv_process = None
        _mpv_info    = {}


def mpv_status() -> Dict:
    with _mpv_lock:
        if _mpv_process is None:
            return {"playing": False, **_mpv_info}
        rc = _mpv_process.poll()
        if rc is None:
            return {"playing": True, **_mpv_info}
        # F1: Process just died naturally — record exit info once
        if "exit_code" not in _mpv_info and _mpv_info:
            _mpv_info["exit_code"] = rc
            _mpv_info["exit_time"] = int(time.time())
        return {"playing": False, **_mpv_info}


# ── Recording management ──────────────────────────────────────────────────────

_recordings:      Dict[str, Dict] = {}
_recordings_lock: threading.Lock  = threading.Lock()

_web_tasks:      List[Dict] = []   # [{id, type, title, ch_name, ch_url, start_ts}]
_web_tasks_lock: threading.Lock = threading.Lock()

_tvmaze_show_cache: Dict[str, Dict] = {}   # title_key → {id, imdb_id, summary, _ts}
_tvmaze_ep_cache:   Dict[str, Dict] = {}   # ep_key    → {season, episode, ep_title, ep_desc, _ts}
_tvmaze_lock:       threading.Lock  = threading.Lock()


def launch_recording(url: str, channel: str, show: str = "") -> Dict:
    """Start a background mpv recording (no window). Returns status dict."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    filename = get_safe_filename(channel, show)
    out_path = RECORDINGS_DIR / filename

    mpv_cmd = [
        "mpv",
        f"--stream-record={out_path}",
        "--video=no", "--audio=no",          # background — no window
        "--stream-lavf-o=timeout=10000000",
        url,
    ]
    rec_id = str(int(time.time() * 1000))
    try:
        log_handle = open(MPV_LOG_FILE, "a", encoding="utf-8", errors="replace")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_handle.write(f"\n{'='*80}\n[{ts}] RECORDING: {channel}")
        if show:
            log_handle.write(f" — {show}")
        log_handle.write(f"\nOutput: {out_path}\nCommand: {' '.join(str(a) for a in mpv_cmd)}\n{'-'*80}\n")
        log_handle.flush()

        kw: Dict = {"stdout": log_handle, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if platform.system() == "Windows":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(mpv_cmd, **kw)

        with _recordings_lock:
            _recordings[rec_id] = {
                "id":         rec_id,
                "channel":    channel,
                "show":       show,
                "url":        url,
                "path":       str(out_path),
                "filename":   filename,
                "started_at": int(time.time()),
                "process":    proc,
                "log_handle": log_handle,
            }
        logging.info(f"Recording started: {channel} → {filename}")
        return {"ok": True, "id": rec_id, "filename": filename, "path": str(out_path)}
    except FileNotFoundError:
        return {"ok": False, "error": "mpv not found — is it installed?"}
    except Exception as e:
        logging.error(f"launch_recording error: {e}")
        return {"ok": False, "error": str(e)}


def stop_recording(rec_id: str) -> bool:
    """Stop and remove a recording by id. Returns True if found."""
    with _recordings_lock:
        rec = _recordings.get(rec_id)
        if not rec:
            return False
        proc = rec.get("process")
        lh   = rec.get("log_handle")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    if lh:
        try:
            lh.write(f"{'='*80}\n[Stopped by web UI]\n")
            lh.close()
        except Exception:
            pass
    with _recordings_lock:
        _recordings.pop(rec_id, None)
    logging.info(f"Recording stopped: {rec_id}")
    return True


def recordings_status() -> List[Dict]:
    """Return serialisable status list for all active recordings."""
    with _recordings_lock:
        recs = list(_recordings.values())
    return [
        {
            "id":         r["id"],
            "channel":    r["channel"],
            "show":       r["show"],
            "filename":   r["filename"],
            "path":       r["path"],
            "started_at": r["started_at"],
            "running":    r.get("process") is not None and r["process"].poll() is None,
            "duration_s": int(time.time()) - r["started_at"],
        }
        for r in recs
    ]


# ── MPV log archiving (reference: Term-TV.py) ─────────────────────────────────

def archive_mpv_log():
    if not MPV_LOG_FILE.exists() or MPV_LOG_FILE.stat().st_size == 0:
        return
    MPV_LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        data = MPV_LOG_FILE.read_bytes()
        if len(data) <= MPV_LOG_CHUNK_SIZE:
            arch = MPV_LOG_ARCHIVE_DIR / f"mpv-output-{ts}.log.xz"
            with lzma.open(arch, "wb", preset=9) as f:
                f.write(data)
            print(f"MPV log archived: {arch.name}  ({len(data)//1024} KB → {arch.stat().st_size//1024} KB)")
        else:
            total = (len(data) + MPV_LOG_CHUNK_SIZE - 1) // MPV_LOG_CHUNK_SIZE
            for idx in range(total):
                chunk = data[idx * MPV_LOG_CHUNK_SIZE:(idx + 1) * MPV_LOG_CHUNK_SIZE]
                arch  = MPV_LOG_ARCHIVE_DIR / f"mpv-output-{ts}-part{idx+1:03d}of{total:03d}.log.xz"
                with lzma.open(arch, "wb", preset=9) as f:
                    f.write(chunk)
            print(f"MPV log split into {total} chunks and archived.")
        MPV_LOG_FILE.write_bytes(b"")
    except Exception as e:
        logging.warning(f"archive_mpv_log: {e}")
        return

    cutoff = datetime.now() - timedelta(days=365)
    deleted = 0
    for arch in MPV_LOG_ARCHIVE_DIR.iterdir():
        if arch.is_file() and arch.name.startswith("mpv-output-"):
            try:
                if datetime.fromtimestamp(arch.stat().st_mtime) < cutoff:
                    arch.unlink()
                    deleted += 1
            except Exception:
                pass
    if deleted:
        print(f"MPV log archive: Deleted {deleted} file(s) older than 1 year.")


# ── Application state ─────────────────────────────────────────────────────────

_channels:            List[Dict] = []
_epg:                 Dict       = {}
_config:              Dict       = {}
_vpn_configured                  = False
_data_loading                    = True
_data_version:        int        = 0   # increments when M3U or EPG finishes loading
_active_playlist_idx: int        = 0
_data_lock                       = threading.Lock()


# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


# ── HTML / CSS / JS template ──────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Term-TV Web Guide</title>
<style>
:root {
  --bg:           #0e1117;
  --surface:      #161b22;
  --surface2:     #1c2230;
  --ch-bg:        #12151d;
  --prog-bg:      #1e2433;
  --prog-now-bg:  #0d2035;
  --prog-now-bdr: #1d6fa4;
  --prog-hover:   #252d3d;
  --prog-match:   #1a2d10;
  --time-bg:      #111420;
  --hdr-bg:       #0a0d13;
  --border:       #272f3d;
  --text:         #d1d5db;
  --muted:        #6b7280;
  --dim:          #3d4452;
  --accent:       #3b82f6;
  --now-line:     #ef4444;
  --green:        #22c55e;
  --ch-w:         180px;
  --row-h:        54px;
  --time-h:       32px;
  --hdr-h:        50px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 13px;
  overflow: hidden;
  height: 100vh;
  user-select: none;
}

/* ── Header ── */
#hdr {
  position: fixed; top: 0; left: 0; right: 0;
  height: var(--hdr-h);
  background: var(--hdr-bg);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 7px;
  padding: 0 12px; z-index: 100;
  flex-wrap: nowrap; overflow: hidden;
}
.logo { font-weight: 700; font-size: 14px; color: var(--accent); white-space: nowrap; }
.divider { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
#now-playing {
  background: #14401a; color: var(--green);
  padding: 3px 8px; border-radius: 4px; font-size: 11px;
  max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.flex1 { flex: 1; min-width: 0; }
#search {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  border-radius: 5px; padding: 5px 10px; font-size: 12px; width: 200px; outline: none;
}
#search:focus { border-color: var(--accent); }
#search::placeholder { color: var(--muted); }
.vpn-badge { font-size: 11px; padding: 3px 8px; border-radius: 4px; font-weight: 600; white-space: nowrap; }
.vpn-on  { background: #14532d; color: #4ade80; }
.vpn-off { background: #3b1515; color: #f87171; }
#clock { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.btn {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  border-radius: 5px; padding: 4px 9px; cursor: pointer; font-size: 11px;
  white-space: nowrap; transition: background .1s; flex-shrink: 0;
}
.btn:hover  { background: var(--surface2); }
.btn:disabled { opacity: .4; cursor: default; }
.btn-accent { border-color: var(--accent); color: var(--accent); }
.btn-danger { border-color: #7f1d1d; color: #f87171; }
.btn-danger:hover { background: #3b1515; }
.btn-group { display: flex; gap: 2px; flex-shrink: 0; }

/* ── Guide wrapper ── */
#guide-wrap {
  position: fixed; top: var(--hdr-h); left: 0; right: 0; bottom: 0;
  overflow: hidden;
}
#guide {
  position: absolute; top: 0; left: 0; right: 0; bottom: 0;
  overflow: auto;
}

/* ── Rows ── */
.row { display: flex; border-bottom: 1px solid var(--border); }
.row-time {
  height: var(--time-h); position: sticky; top: 0; z-index: 20;
  background: var(--time-bg);
}
.row-ch {
  height: var(--row-h);
  /* Fix-2: Skip layout/paint for off-screen rows — major scroll perf win */
  content-visibility: auto;
  contain-intrinsic-size: auto var(--row-h);
}
.row-ch:hover > .ch-cell { background: var(--surface2); }

/* ── Channel cell ── */
.ch-cell {
  width: var(--ch-w); min-width: var(--ch-w); flex-shrink: 0;
  position: sticky; left: 0; z-index: 10;
  background: var(--ch-bg); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; justify-content: center;
  padding: 4px 10px; cursor: pointer; overflow: hidden;
  transition: background .1s;
}
.ch-cell:hover { background: var(--surface2); }
.ch-name {
  font-weight: 500; font-size: 12px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.ch-group {
  color: var(--muted); font-size: 10px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; margin-top: 1px;
}
.corner {
  z-index: 30 !important; background: var(--time-bg) !important;
  cursor: default; justify-content: center;
  font-size: 10px; color: var(--muted); font-weight: 600;
  text-transform: uppercase; letter-spacing: .5px;
}

/* ── Programme area ── */
.prog-area { position: relative; height: 100%; flex-shrink: 0; }

/* ── Time ticks ── */
.tick {
  position: absolute; top: 0; height: 100%;
  display: flex; align-items: center; padding-left: 6px;
  border-left: 1px solid var(--border);
  font-size: 11px; color: var(--muted); white-space: nowrap; pointer-events: none;
}

/* ── Programme blocks ── */
.prog {
  position: absolute; top: 3px; height: calc(100% - 6px);
  background: var(--prog-bg); border-radius: 4px; border: 1px solid transparent;
  padding: 3px 6px; overflow: hidden; cursor: pointer;
  display: flex; flex-direction: column; justify-content: center;
  transition: background .1s; min-width: 4px;
}
.prog:hover    { background: var(--prog-hover); border-color: var(--border); z-index: 2; }
.prog.now      { background: var(--prog-now-bg); border-color: var(--prog-now-bdr); border-left-width: 3px; }
.prog.now:hover{ background: #0f2a40; }
.prog.past     { opacity: .4; }
.prog.match    { background: var(--prog-match); border-color: #4ade80 !important; }
.prog-title    { font-size: 11px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prog-bar {
  position: absolute; bottom: 0; left: 0; height: 2px;
  background: var(--prog-now-bdr); border-radius: 0 0 3px 3px; pointer-events: none;
}
.prog-badge {
  position: absolute; top: 2px; right: 3px;
  font-size: 8px; font-weight: 700; letter-spacing: .3px;
  padding: 1px 3px; border-radius: 3px; pointer-events: none; line-height: 1.5; z-index: 1;
}
.pb-remind   { background: #0c4a6e; color: #38bdf8; }
.pb-schedule { background: #3b0764; color: #a78bfa; }
.pb-record   { background: #7f1d1d; color: #f87171; }
.no-epg {
  position: absolute; top: 0; left: 0; height: 100%;
  display: flex; align-items: center; padding-left: 10px;
  color: var(--dim); font-size: 11px; font-style: italic;
}

/* ── Now line ── */
#now-line {
  position: fixed; width: 2px; pointer-events: none; z-index: 15;
  background: linear-gradient(180deg, var(--now-line), rgba(239,68,68,.15));
}
#now-line::before {
  content: ''; position: absolute; top: -2px; left: -3px;
  width: 8px; height: 8px; border-radius: 50%; background: var(--now-line);
}

/* ── Loading overlay ── */
#loading {
  position: fixed; inset: 0; background: rgba(14,17,23,.85);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  z-index: 500; gap: 12px;
}
.spinner {
  width: 32px; height: 32px;
  border: 3px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
#loading-msg { color: var(--muted); font-size: 13px; }

/* ── Popup ── */
#popup-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.75);
  display: flex; align-items: center; justify-content: center;
  z-index: 200; backdrop-filter: blur(3px);
}
.popup-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  width: min(480px, 92vw); max-height: 80vh; display: flex; flex-direction: column;
  box-shadow: 0 24px 64px rgba(0,0,0,.6); overflow: hidden;
}
#popup-hdr {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
}
#popup-ch { font-size: 12px; color: var(--muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.popup-x { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 15px; padding: 2px 5px; border-radius: 3px; }
.popup-x:hover { background: var(--surface2); color: var(--text); }
#popup-body { padding: 16px; overflow-y: auto; flex: 1; }
.now-badge {
  display: inline-block; background: #14532d; color: #4ade80;
  font-size: 10px; font-weight: 700; letter-spacing: .5px;
  padding: 2px 7px; border-radius: 4px; margin-bottom: 8px;
}
#popup-title { font-size: 18px; font-weight: 600; margin-bottom: 6px; }
#popup-ep    { font-size: 12px; color: var(--accent); margin-bottom: 4px; }
#popup-time  { font-size: 12px; color: var(--muted); margin-bottom: 12px; }
#popup-desc  { font-size: 13px; color: var(--muted); line-height: 1.55; }
#popup-ftr   { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn-primary:hover { background: #2563eb; border-color: #2563eb; }

/* ── Toast ── */
#toast {
  position: fixed; bottom: 18px; right: 18px;
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--text); padding: 10px 14px; border-radius: 7px;
  font-size: 12px; box-shadow: 0 4px 20px rgba(0,0,0,.4); z-index: 300;
  max-width: 380px;
}
#toast.err { background: #3b1515; border-color: #7f1d1d; color: #f87171; }

/* ── Info bar ── */
#info-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  height: 22px; background: var(--hdr-bg); border-top: 1px solid var(--border);
  display: flex; align-items: center; padding: 0 12px; gap: 16px;
  font-size: 11px; color: var(--muted); z-index: 50;
}

/* ── Group filter button + panel ── */
#btn-groups { position: relative; }
#btn-groups.active { border-color: var(--accent); color: var(--accent); }
#groups-panel {
  position: fixed;
  width: 240px; max-height: 400px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: 0 12px 40px rgba(0,0,0,.55); z-index: 150;
  padding: 0;
}
.gp-actions {
  display: flex; gap: 4px; padding: 8px 10px;
  border-bottom: 1px solid var(--border); position: sticky; top: 0;
  background: var(--surface); z-index: 1;
}
.gp-action-btn {
  flex: 1; font-size: 10px; padding: 3px 6px; cursor: pointer;
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--muted); border-radius: 4px; transition: color .1s;
}
.gp-action-btn:hover { color: var(--text); }
.gp-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; cursor: pointer; transition: background .1s;
}
.gp-item:hover { background: var(--surface2); }
.gp-item input[type=checkbox] { accent-color: var(--accent); flex-shrink: 0; cursor: pointer; }
.gp-item-name {
  font-size: 12px; flex: 1; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; cursor: pointer;
}
.gp-item-count { font-size: 10px; color: var(--muted); flex-shrink: 0; }
.gp-vod-tag {
  font-size: 9px; padding: 1px 5px; border-radius: 3px;
  background: #1e2a3a; color: #60a5fa; flex-shrink: 0;
}
.gp-apply-row {
  padding: 8px 10px; border-top: 1px solid var(--border);
  position: sticky; bottom: 0; background: var(--surface);
}
.gp-apply-btn {
  width: 100%; padding: 6px; font-size: 12px; font-weight: 600;
  background: var(--accent); border: none; color: #fff;
  border-radius: 5px; cursor: pointer; transition: background .1s;
}
.gp-apply-btn:hover { background: #2563eb; }

/* F3: Currently playing row highlight */
.row-playing > .ch-cell {
  background: #0d2a0d !important;
  border-left: 3px solid var(--green);
}

/* V2: VOD sort select */
#vod-sort {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  border-radius: 4px; padding: 3px 8px; font-size: 11px; cursor: pointer; outline: none;
}
#vod-sort:focus { border-color: var(--accent); }

/* ── VOD view ── */
#vod-view {
  position: fixed; top: var(--hdr-h); left: 0; right: 0; bottom: 22px;
  overflow-y: auto; padding: 12px 14px;
}
#vod-toolbar {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 12px; color: var(--muted); font-size: 12px;
}
.vod-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
}
.vod-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 7px;
  overflow: hidden; cursor: pointer; transition: border-color .15s, transform .1s;
}
.vod-card:hover { border-color: var(--accent); transform: translateY(-2px); }
.vod-poster {
  height: 110px; background-color: var(--surface2);
  background-size: cover; background-position: center top;
  position: relative; overflow: hidden;
}
.vod-poster-placeholder {
  height: 110px; background: var(--surface2);
  display: flex; align-items: center; justify-content: center;
  font-size: 28px; color: var(--dim);
}
.vod-play-overlay {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  background: rgba(0,0,0,0); color: rgba(255,255,255,0);
  font-size: 30px; transition: all .15s;
}
.vod-card:hover .vod-play-overlay { background: rgba(0,0,0,.5); color: rgba(255,255,255,.9); }
.vod-info { padding: 7px 8px 8px; }
.vod-title {
  font-size: 11px; font-weight: 500; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.vod-group {
  font-size: 10px; color: var(--muted); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; margin-top: 2px;
}
#vod-pagination {
  display: flex; justify-content: center; align-items: center;
  gap: 10px; padding: 18px 0 6px; color: var(--muted); font-size: 12px;
}
#vod-pagination .btn { min-width: 70px; }

/* ── Recordings panel ── */
#rec-panel {
  position: fixed; bottom: 26px; left: 12px; z-index: 80;
  background: var(--surface); border: 1px solid #7f1d1d;
  border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.5);
  min-width: 280px; max-width: 400px;
}
#rec-panel-hdr {
  display: flex; align-items: center; gap: 7px;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  font-size: 12px; font-weight: 600; color: #f87171;
}
.rec-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #f87171; flex-shrink: 0;
  animation: rec-pulse 1.2s ease-in-out infinite;
}
@keyframes rec-pulse { 0%,100% { opacity: 1; } 50% { opacity: .25; } }
.rec-item {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 12px; border-bottom: 1px solid var(--border); font-size: 11px;
}
.rec-item:last-child { border-bottom: none; }
.rec-info { flex: 1; min-width: 0; }
.rec-ch  { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rec-dur { color: var(--muted); margin-top: 2px; font-size: 10px;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rec-stop-btn {
  background: none; border: 1px solid #7f1d1d; color: #f87171;
  border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 10px; flex-shrink: 0;
}
.rec-stop-btn:hover { background: #3b1515; }

/* ── Source selector ── */
#source-select {
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 5px;
  padding: 5px 26px 5px 10px;
  font-size: 12px;
  outline: none;
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%236b7280'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
  max-width: 160px;
}
#source-select:focus { border-color: var(--accent); }
#source-select option { background: var(--surface2); }

/* ── Search panel dropdown ── */
#search-wrap { position: relative; }
#search-panel {
  position: absolute; top: calc(100% + 6px); right: 0;
  width: 420px; max-height: 480px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: 0 12px 40px rgba(0,0,0,.55); z-index: 150;
}
.sp-section { padding: 4px 0; }
.sp-label {
  padding: 6px 12px 3px; font-size: 10px; font-weight: 700;
  letter-spacing: .8px; color: var(--muted); text-transform: uppercase;
}
.sp-item {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 12px; cursor: pointer; transition: background .1s;
}
.sp-item:hover { background: var(--surface2); }
.sp-col { flex: 1; min-width: 0; }
.sp-name {
  font-weight: 500; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sp-sub {
  font-size: 11px; color: var(--muted); margin-top: 1px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sp-badge {
  font-size: 10px; font-weight: 700; padding: 2px 7px;
  border-radius: 4px; white-space: nowrap; flex-shrink: 0;
}
.sp-badge.now  { background: #14532d; color: #4ade80; }
.sp-badge.soon { background: #1e2a3a; color: #60a5fa; }
.sp-group-badge {
  font-size: 10px; padding: 2px 6px; border-radius: 4px;
  background: var(--surface2); color: var(--muted); white-space: nowrap; flex-shrink: 0;
}
.sp-divider { height: 1px; background: var(--border); }
.sp-empty { padding: 14px 12px; color: var(--muted); font-size: 12px; font-style: italic; }
</style>
</head>
<body>

<!-- Header -->
<header id="hdr">
  <span class="logo">📺 Term-TV</span>
  <div class="divider"></div>
  <div id="now-playing" style="display:none">▶ ...</div>
  <div class="flex1"></div>
  <select id="source-select" title="Switch playlist source"></select>
  <button id="btn-groups" class="btn" title="Filter by group">Groups ▾</button>
  <div id="search-wrap">
    <input id="search" type="search" placeholder="Search shows or channels…" autocomplete="off">
    <div id="search-panel" style="display:none"></div>
  </div>
  <div id="vpn-badge" class="vpn-badge" style="display:none"></div>
  <div id="clock" class="clock"></div>
  <div class="btn-group">
    <button class="btn" id="btn-back" title="Back 2 hours">◀ 2h</button>
    <button class="btn btn-accent" id="btn-now" title="Jump to now">Now</button>
    <button class="btn" id="btn-fwd" title="Forward 2 hours">2h ▶</button>
  </div>
  <button class="btn btn-danger" id="btn-stop" title="Stop mpv" disabled>■ Stop</button>
  <button class="btn" id="btn-refresh" title="Refresh guide">⟳</button>
</header>

<!-- Groups panel (positioned by JS) -->
<div id="groups-panel" style="display:none"></div>

<!-- VOD view -->
<div id="vod-view" style="display:none">
  <div id="vod-toolbar">
    <span id="vod-count"></span>
    <select id="vod-sort" title="Sort order">
      <option value="az">Title A&#x2192;Z</option>
      <option value="za">Title Z&#x2192;A</option>
      <option value="group">Group</option>
    </select>
  </div>
  <div class="vod-grid" id="vod-grid"></div>
  <div id="vod-pagination"></div>
</div>

<!-- Guide -->
<div id="guide-wrap">
  <div id="guide"></div>
  <div id="now-line" style="display:none"></div>
</div>

<!-- Loading overlay -->
<div id="loading">
  <div class="spinner"></div>
  <div id="loading-msg">Loading channel guide…</div>
</div>

<!-- Programme popup -->
<div id="popup-overlay" style="display:none">
  <div class="popup-card">
    <div id="popup-hdr">
      <span id="popup-ch"></span>
      <button class="popup-x" onclick="closePopup()">✕</button>
    </div>
    <div id="popup-body">
      <div id="popup-now" class="now-badge" style="display:none">NOW PLAYING</div>
      <div id="popup-task-status" style="display:none;align-items:center;gap:8px;font-size:11px;font-weight:600;margin-bottom:6px">
        <span id="popup-task-label"></span>
        <button id="popup-task-cancel" style="font-size:10px;padding:1px 6px;background:none;border:1px solid #555;border-radius:3px;color:#999;cursor:pointer;flex-shrink:0">✕ Cancel</button>
      </div>
      <h2 id="popup-title"></h2>
      <div id="popup-ep"></div>
      <div id="popup-time"></div>
      <p id="popup-desc"></p>
    </div>
    <div id="popup-ftr">
      <button class="btn btn-primary" id="popup-play">▶ Play in mpv</button>
      <button class="btn" id="popup-record" style="border-color:#7f1d1d;color:#f87171">⏺ Record</button>
      <button class="btn" id="popup-schedule" style="display:none;border-color:#7c3aed;color:#a78bfa">⏰ Schedule</button>
      <button class="btn" id="popup-remind"   style="display:none;border-color:#0369a1;color:#38bdf8">🔔 Remind</button>
      <a id="popup-imdb" class="btn" target="_blank" rel="noopener" style="border-color:#f5c518;color:#f5c518;text-decoration:none;display:none">IMDb</a>
      <button class="btn" onclick="closePopup()">Cancel</button>
    </div>
  </div>
</div>

<!-- Recordings panel -->
<div id="rec-panel" style="display:none">
  <div id="rec-panel-hdr"><span class="rec-dot"></span> Recording</div>
  <div id="rec-list"></div>
</div>

<!-- Toast -->
<div id="toast" style="display:none"></div>

<!-- Info bar -->
<div id="info-bar">
  <span id="info-ch-count"></span>
  <span id="info-data-age"></span>
</div>

<script>
// Catch any JS errors and show them in the loading overlay
window.onerror = function(msg, src, line) {
  const el = document.getElementById('loading-msg');
  if (el) el.textContent = 'JS Error (line ' + line + '): ' + msg;
  return false;
};

// ── Constants ──────────────────────────────────────────────────────────────
const PX_MIN  = 4;    // pixels per minute
const WIN_HRS = 8;    // default window width in hours
const CH_W    = 180;  // channel column width (must match CSS --ch-w)
const ROW_H   = 54;   // row height (must match CSS --row-h)
const TIME_H  = 32;   // time header height

// ── State ──────────────────────────────────────────────────────────────────
let guideData       = null;
let winStartTs      = Math.floor(Date.now() / 1000) - 1800; // Fix-1: start 30 min back
let searchQ         = '';
let popupCh         = null;
let popupProg       = null;
let loadedAt        = null;
let selectedGroups  = new Set();   // empty = show all groups
let _groupsData     = [];          // [{name, count, is_vod}, ...]
let vodMode         = false;
let vodPage         = 1;
let _vodTotal       = 0;
let _prevDataLoading  = true;       // U3: start true so first poll always detects load completion
const _webTasks = new Map();        // key: ch_url+'|'+start_ts  value: {type, taskId}
let _dataVersion      = -1;         // tracks data_version from /api/status; reload guide when it changes
let _playingRow       = null;       // Fix-7: cached DOM ref to highlighted guide row
let _loadGuideActive  = false;      // prevent concurrent loadGuide() calls
let _guideRetryTimer  = null;       // pending error-retry setTimeout handle
let _playingChName   = '';         // Fix-7: name of currently highlighted channel

// ── Boot ───────────────────────────────────────────────────────────────────
// Script is at end of <body> so DOM is already ready — no DOMContentLoaded needed
function _showBootError(msg) {
  const el = document.getElementById('loading-msg');
  if (el) el.textContent = 'ERROR: ' + msg;
  console.error('Term-TV boot error:', msg);
}

async function _boot() {
  document.getElementById('loading-msg').textContent = 'Initializing…';
  try {
    setupEvents();
  } catch (e) {
    _showBootError('setupEvents failed — ' + e.message);
    return;
  }

  // Connectivity test — confirms browser can reach Flask before loading guide
  document.getElementById('loading-msg').textContent = 'Pinging server…';
  try {
    const pr = await fetch('/api/ping');
    const pj = await pr.json();
    document.getElementById('loading-msg').textContent =
      'Server OK (' + pj.channels + ' ch) — loading guide…';
  } catch (e) {
    _showBootError('Cannot reach server — ' + e.message);
    return;
  }

  loadSources();
  _loadScheduled();
  loadGuide().then(() => {
    jumpToNow();
    loadGroups();
    setInterval(tickClock, 1000);
    setInterval(updateStatus, 5000);
    setInterval(() => { if (guideData) positionNowLine(); }, 15000);
    tickClock();
    updateStatus();
  }).catch(e => _showBootError('loadGuide rejected — ' + e.message));
}
_boot();

// ── Event setup ───────────────────────────────────────────────────────────
function setupEvents() {
  const searchEl = document.getElementById('search');
  searchEl.addEventListener('input', e => {
    searchQ = e.target.value.toLowerCase().trim();
    clearTimeout(_searchTimer);
    if (vodMode) {
      vodPage = 1;
      _searchTimer = setTimeout(() => loadVod(1), 300);
    } else {
      if (guideData) filterRender();
      if (searchQ.length >= 2) {
        _searchTimer = setTimeout(() => fetchSearch(searchQ), 400);
      } else {
        clearSearchPanel();
      }
    }
  });
  searchEl.addEventListener('focus', () => {
    if (searchQ.length >= 2 && document.getElementById('search-panel').innerHTML)
      document.getElementById('search-panel').style.display = '';
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('#search-wrap')) clearSearchPanel();
  });
  document.getElementById('btn-back').onclick = () => { winStartTs -= 7200; loadGuide(); };
  document.getElementById('btn-fwd').onclick  = () => { winStartTs += 7200; loadGuide(); };
  document.getElementById('btn-now').onclick  = () => {
    winStartTs = Math.floor(Date.now() / 1000) - 1800; // Fix-1: match default offset
    loadGuide().then(jumpToNow);
  };
  document.getElementById('btn-stop').onclick    = stopMpv;
  document.getElementById('btn-refresh').onclick = () => vodMode ? loadVod(vodPage) : loadGuide();
  document.getElementById('source-select').addEventListener('change', e => switchSource(parseInt(e.target.value)));
  document.getElementById('vod-sort').addEventListener('change', () => { if (vodMode) loadVod(vodPage); }); // V2
  document.getElementById('btn-groups').addEventListener('click', e => { e.stopPropagation(); toggleGroupsPanel(); });
  document.addEventListener('click', e => {
    if (!e.target.closest('#groups-panel') && !e.target.closest('#btn-groups')) closeGroupsPanel();
  });
  document.getElementById('popup-overlay').addEventListener('click', e => {
    if (e.target.id === 'popup-overlay') closePopup();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closePopup();
  });
}

// ── Data loading ──────────────────────────────────────────────────────────
// silent=true: background refresh (no loading overlay, skipped if already running)
async function loadGuide(silent = false) {
  if (_loadGuideActive && silent) return;  // skip concurrent background refreshes
  _loadGuideActive = true;

  // Cancel any pending error-retry; this call supersedes it
  if (_guideRetryTimer !== null) { clearTimeout(_guideRetryTimer); _guideRetryTimer = null; }

  if (!silent) setLoading(true);
  const p = new URLSearchParams({ start_ts: winStartTs, hours: WIN_HRS });
  selectedGroups.forEach(g => p.append('groups', g));
  try {
    // AbortController inside try so any throw is caught and finally always dismisses the overlay
    const _ctrl    = new AbortController();
    const _timeout = setTimeout(() => _ctrl.abort(), 30000);  // 30s hard timeout
    const res = await fetch('/api/guide?' + p, { signal: _ctrl.signal });
    clearTimeout(_timeout);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    guideData = await res.json();
    loadedAt  = Date.now();
    if (guideData.error) {
      if (!silent) document.getElementById('loading-msg').textContent = guideData.error;
      // Data still loading — retry in 3s without waiting for updateStatus poll
      _guideRetryTimer = setTimeout(() => loadGuide(), 3000);
      return;
    }
    renderGuide(guideData.channels);
    updateInfoBar();
    setLoading(false);  // always dismiss overlay on successful render (even if silent)
  } catch (e) {
    const msg = 'Failed to load guide: ' + e.message;
    showToast(msg, true);
    if (!silent) {
      const el = document.getElementById('loading-msg');
      if (el) el.textContent = msg;  // show error in overlay so it's visible without dev tools
    }
  } finally {
    _loadGuideActive = false;
    if (!silent) setLoading(false);  // dismiss overlay on error/exception for non-silent calls
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────
function renderGuide(channels) {
  // Fix-7: DOM is being replaced — invalidate cached row reference
  _playingRow    = null;
  _playingChName = '';
  const guide   = document.getElementById('guide');
  const totalW  = WIN_HRS * 60 * PX_MIN;
  const frag    = document.createDocumentFragment();

  frag.appendChild(buildTimeRow(totalW));
  for (const ch of channels) frag.appendChild(buildRow(ch, totalW));

  guide.innerHTML = '';
  guide.appendChild(frag);
  positionNowLine();
}

function filterRender() {
  if (!guideData) return;
  const q  = searchQ;
  let chs  = guideData.channels;
  if (q) {
    chs = chs.filter(ch => {
      if (ch.name.toLowerCase().includes(q)) return true;
      if ((ch.group || '').toLowerCase().includes(q)) return true;
      return (ch.programs || []).some(p => p.title.toLowerCase().includes(q));
    });
  }
  const guide  = document.getElementById('guide');
  const totalW = WIN_HRS * 60 * PX_MIN;
  const frag   = document.createDocumentFragment();
  frag.appendChild(buildTimeRow(totalW));
  for (const ch of chs) frag.appendChild(buildRow(ch, totalW));
  guide.innerHTML = '';
  guide.appendChild(frag);
  positionNowLine();
}

function buildTimeRow(totalW) {
  const row    = mk('div', 'row row-time');
  const corner = mk('div', 'ch-cell corner');
  corner.textContent = (guideData.total || 0) + ' ch';
  row.appendChild(corner);

  const area = mk('div', 'prog-area time-area');
  area.style.width = totalW + 'px';

  const winEnd = winStartTs + WIN_HRS * 3600;
  let ts = Math.ceil(winStartTs / 1800) * 1800;
  while (ts < winEnd) {
    const tick = mk('div', 'tick');
    tick.style.left = tsX(ts) + 'px';
    tick.textContent = fmt(ts);
    area.appendChild(tick);
    ts += 1800;
  }
  row.appendChild(area);
  return row;
}

function buildRow(ch, totalW) {
  const row    = mk('div', 'row row-ch');
  const cell   = mk('div', 'ch-cell');
  const nameEl = mk('span', 'ch-name');
  nameEl.textContent = ch.name;
  const grpEl  = mk('span', 'ch-group');
  grpEl.textContent = ch.group || '';
  cell.appendChild(nameEl);
  cell.appendChild(grpEl);
  cell.title = 'Watch ' + ch.name + ' live';
  cell.addEventListener('click', () => play(ch.url, ch.name, ''));
  row.appendChild(cell);

  const area = mk('div', 'prog-area');
  area.style.width = totalW + 'px';

  if (!ch.programs || !ch.programs.length) {
    const nd = mk('div', 'no-epg');
    nd.style.width = totalW + 'px';
    nd.textContent = 'No guide data';
    area.appendChild(nd);
  } else {
    for (const p of ch.programs) {
      const b = buildBlock(p, ch, totalW);
      if (b) area.appendChild(b);
    }
  }
  row.appendChild(area);
  return row;
}

function buildBlock(prog, ch, totalW) {
  const winEnd = winStartTs + WIN_HRS * 3600;
  const cs     = Math.max(prog.start_ts, winStartTs);
  const ce     = Math.min(prog.stop_ts,  winEnd);
  if (ce <= cs) return null;

  const x  = tsX(cs);
  const w  = Math.max(3, (ce - cs) * PX_MIN / 60 - 2);
  const nt = guideData.now_ts;

  let cls = 'prog';
  if (prog.stop_ts  <= nt) cls += ' past';
  else if (prog.start_ts <= nt) cls += ' now';
  if (searchQ && prog.title.toLowerCase().includes(searchQ)) cls += ' match';

  const b     = mk('div', cls);
  b.style.left  = x + 'px';
  b.style.width = w + 'px';

  const t = mk('span', 'prog-title');
  t.textContent = prog.title;
  b.appendChild(t);

  if (prog.start_ts <= nt && prog.stop_ts > nt) {
    const pct = (nt - prog.start_ts) / (prog.stop_ts - prog.start_ts);
    const bar = mk('div', 'prog-bar');
    bar.style.width = (Math.min(1, pct) * 100).toFixed(1) + '%';
    b.appendChild(bar);
  }

  b.title = prog.title + '\\n' + fmt(prog.start_ts) + ' – ' + fmt(prog.stop_ts);
  b.addEventListener('click', e => { e.stopPropagation(); showPopup(prog, ch); });
  const _tk = (ch.url || '') + '|' + prog.start_ts;
  b.dataset.tk = _tk;
  const _bt = _webTasks.get(_tk);
  if (_bt) b.appendChild(_makeTaskBadge(_bt.type));
  return b;
}

// ── Helpers ───────────────────────────────────────────────────────────────
function tsX(ts)  { return (ts - winStartTs) * PX_MIN / 60; }
function fmt(ts)  {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}
function mk(tag, cls) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  return el;
}

// ── Now line ──────────────────────────────────────────────────────────────
function positionNowLine() {
  const guide = document.getElementById('guide');
  const line  = document.getElementById('now-line');
  if (!guideData) { line.style.display = 'none'; return; }

  const nowX   = tsX(guideData.now_ts) + CH_W - guide.scrollLeft;
  const guideH = guide.clientHeight;
  // U4: removed unused topOff variable (hdr-h=50 + TIME_H is hardcoded below)

  if (nowX < CH_W || nowX > guide.clientWidth) {
    line.style.display = 'none';
    return;
  }
  line.style.display = 'block';
  line.style.left    = nowX + 'px';
  line.style.top     = (50 + TIME_H) + 'px';   // hdr-h=50 + time-h
  line.style.height  = Math.max(0, guideH - TIME_H - guide.scrollTop) + 'px';
}

// Keep now-line synced on scroll
document.addEventListener('scroll', positionNowLine, true);

function jumpToNow() {
  if (!guideData) return;
  const guide = document.getElementById('guide');
  guide.scrollLeft = Math.max(0, tsX(guideData.now_ts) - 80);
}

// ── Clock ─────────────────────────────────────────────────────────────────
function tickClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  if (guideData) {
    guideData.now_ts = Math.floor(now.getTime() / 1000);
    positionNowLine();
  }
}

// ── Status polling ────────────────────────────────────────────────────────
async function updateStatus() {
  try {
    const res = await fetch('/api/status');
    const st  = await res.json();

    // VPN badge
    const vb = document.getElementById('vpn-badge');
    if (st.vpn_configured) {
      vb.style.display = '';
      if (st.vpn_connected) {
        vb.textContent = '● VPN';
        vb.className   = 'vpn-badge vpn-on';
        vb.title       = 'VPN Connected: ' + (st.vpn_ip || '');
      } else {
        vb.textContent = '○ VPN Off';
        vb.className   = 'vpn-badge vpn-off';
        vb.title       = 'VPN disconnected';
      }
    } else {
      vb.style.display = 'none';
    }

    // Now playing
    const np = document.getElementById('now-playing');
    if (st.playing) {
      np.style.display = '';
      np.textContent   = '▶ ' + st.channel + (st.show ? ' — ' + st.show : '');
    } else {
      np.style.display = 'none';
    }

    document.getElementById('btn-stop').disabled = !st.playing;

    // F3 / Fix-7: Highlight the playing row — only query DOM when channel actually changes
    const nowChName = st.playing ? (st.channel || '') : '';
    if (nowChName !== _playingChName) {
      if (_playingRow) { _playingRow.classList.remove('row-playing'); _playingRow = null; }
      _playingChName = nowChName;
      if (nowChName) {
        document.querySelectorAll('.row-ch').forEach(row => {
          const nameEl = row.querySelector('.ch-name');
          if (nameEl && nameEl.textContent === nowChName) {
            row.classList.add('row-playing');
            _playingRow = row;
          }
        });
      }
    }

    // U3: Auto-refresh guide when background data load transitions loading → done
    if (_prevDataLoading && !st.data_loading) {
      loadGuide(true).then(() => { loadGroups(); if (!vodMode) jumpToNow(); });
    }
    _prevDataLoading = !!st.data_loading;

    // Auto-refresh when EPG finishes loading after M3U (data_version increments)
    if (_dataVersion >= 0 && st.data_version !== _dataVersion && !st.data_loading) {
      loadGuide(true).then(() => loadGroups());
    }
    _dataVersion = st.data_version ?? _dataVersion;

  } catch (_) {}
}

// ── Playback ──────────────────────────────────────────────────────────────
async function play(url, channel, show) {
  try {
    const res  = await fetch('/api/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, channel, show })
    });
    const data = await res.json();
    if (data.ok) {
      showToast('▶ Launching: ' + channel + (show ? ' — ' + show : ''));
      setTimeout(updateStatus, 800);
      // F2: Aggressive short poll (5 × 2s) to detect immediate stream failure
      let _fpc = 0;
      const _fp = setInterval(async () => {
        _fpc++;
        try {
          const r = await fetch('/api/status');
          const s = await r.json();
          if (s.mpv_just_died) {
            clearInterval(_fp);
            showToast('Stream failed — mpv exited (code ' + (s.mpv_exit_code ?? '?') + '). URL may be dead.', true);
            updateStatus();
          } else if (s.playing || _fpc >= 5) {
            clearInterval(_fp);
          }
        } catch (_) { if (_fpc >= 5) clearInterval(_fp); }
      }, 2000);
    } else {
      showToast('mpv error: ' + (data.error || 'unknown'), true);
    }
  } catch (e) { showToast('Error: ' + e.message, true); }
}

async function stopMpv() {
  await fetch('/api/stop', { method: 'POST' });
  showToast('■ Stopped mpv');
  setTimeout(updateStatus, 500);
}

// ── Popup ─────────────────────────────────────────────────────────────────
function showPopup(prog, ch) {
  popupCh   = ch;
  popupProg = prog;
  const nt  = guideData ? guideData.now_ts : Math.floor(Date.now() / 1000);
  const isNow = prog.start_ts <= nt && prog.stop_ts > nt;

  document.getElementById('popup-ch').textContent    = ch.name + (ch.group ? '  [' + ch.group + ']' : '');
  document.getElementById('popup-title').textContent = prog.title;
  // Format episode_num nicely; subtitle fills ep line when there's no ep number
  const _epParsed = parseEpNum(prog.episode_num);
  const _epEl     = document.getElementById('popup-ep');
  if (_epParsed) {
    const _se  = fmtSE(_epParsed.season, _epParsed.episode);
    const _sub = prog.subtitle || '';
    _epEl.textContent  = _sub ? _se + '  —  ' + _sub : _se;
    _epEl.style.display = '';
  } else if (prog.subtitle) {
    _epEl.textContent  = prog.subtitle;
    _epEl.style.display = '';
  } else {
    _epEl.textContent  = '';
    _epEl.style.display = 'none';
  }
  document.getElementById('popup-time').textContent  =
    fmt(prog.start_ts) + ' – ' + fmt(prog.stop_ts) + '  (' + prog.duration_min + ' min)';
  document.getElementById('popup-desc').textContent  = prog.description || 'No description available.';
  document.getElementById('popup-now').style.display = isNow ? 'inline-block' : 'none';

  const playBtn = document.getElementById('popup-play');
  playBtn.textContent = isNow ? '▶ Watch Now' : '▶ Play in mpv';
  playBtn.onclick = () => { play(ch.url, ch.name, prog.title); closePopup(); };

  const recBtn = document.getElementById('popup-record');
  recBtn.onclick = async () => {
    closePopup();
    const recId = await startRecording(ch.url, ch.name, prog.title);
    if (recId) _markTask(ch, prog, 'record', recId);
  };

  const _taskStatusEl = document.getElementById('popup-task-status');
  const _existingTask = _webTasks.get((ch.url || '') + '|' + prog.start_ts);
  if (_existingTask) {
    const _taskLabels = {remind: '🔔 Reminder set', schedule: '⏰ Scheduled for playback', record: '⏺ Recording active'};
    const _taskColors = {remind: '#38bdf8', schedule: '#a78bfa', record: '#f87171'};
    const _tk = _taskKey(ch, prog);
    const _type = _existingTask.type;
    const _tid  = _existingTask.taskId;
    document.getElementById('popup-task-label').textContent = _taskLabels[_type] || _type;
    document.getElementById('popup-task-label').style.color = _taskColors[_type] || '';
    document.getElementById('popup-task-cancel').onclick = () => cancelTask(_tk, _type, _tid);
    _taskStatusEl.style.display = 'flex';
  } else {
    _taskStatusEl.style.display = 'none';
  }

  const imdbBtn   = document.getElementById('popup-imdb');
  const imdbQuery = prog.title + (prog.air_date ? ' ' + prog.air_date.slice(0, 4) : '');
  imdbBtn.href        = 'https://www.imdb.com/find/?q=' + encodeURIComponent(imdbQuery) + '&s=tt';
  imdbBtn.textContent = 'IMDb';   // will be updated by fetchShowMeta if match found
  imdbBtn.style.display = '';
  fetchShowMeta(prog, prog);   // async; updates href/label/desc/ep when ready

  const isFuture = prog.start_ts > nt;
  const schedBtn  = document.getElementById('popup-schedule');
  const remindBtn = document.getElementById('popup-remind');
  schedBtn.style.display  = isFuture ? '' : 'none';
  remindBtn.style.display = isFuture ? '' : 'none';
  if (isFuture) {
    schedBtn.onclick  = () => { schedulePb(prog, ch);  closePopup(); };
    remindBtn.onclick = () => { remindPb(prog, ch);    closePopup(); };
  }

  document.getElementById('popup-overlay').style.display = 'flex';
}

function closePopup() {
  document.getElementById('popup-overlay').style.display = 'none';
}

function _taskKey(ch, prog)    { return (ch.url || '') + '|' + prog.start_ts; }
function _makeTaskBadge(type)  {
  const s = document.createElement('span');
  s.className = 'prog-badge pb-' + type;
  s.textContent = type === 'remind' ? 'RM' : type === 'schedule' ? 'SC' : 'RC';
  return s;
}
function _refreshTaskBadges() {
  _webTasks.forEach((entry, key) => {
    document.querySelectorAll('[data-tk="' + key.replace(/"/g, '\\"') + '"]').forEach(cell => {
      if (!cell.querySelector('.prog-badge')) cell.appendChild(_makeTaskBadge(entry.type));
    });
  });
}
function _markTask(ch, prog, type, taskId) {
  _webTasks.set(_taskKey(ch, prog), {type, taskId});
  _refreshTaskBadges();
}
async function _loadScheduled() {
  try {
    const data = await (await fetch('/api/scheduled')).json();
    (data.tasks || []).forEach(t => {
      if (t.ch_url && t.start_ts) _webTasks.set(t.ch_url + '|' + t.start_ts, {type: t.type, taskId: t.id});
    });
    _refreshTaskBadges();
  } catch (_) {}
}

async function cancelTask(taskKey, type, taskId) {
  const endpoint = type === 'record'
    ? '/api/recording/' + taskId + '/stop'
    : '/api/scheduled/' + taskId + '/cancel';
  try {
    await fetch(endpoint, {method: 'POST'});
    _webTasks.delete(taskKey);
    document.querySelectorAll('[data-tk="' + taskKey.replace(/"/g, '\\"') + '"]').forEach(cell => {
      cell.querySelectorAll('.prog-badge').forEach(b => b.remove());
    });
    showToast('Cancelled');
    closePopup();
  } catch (e) { showToast('Error: ' + e.message, true); }
}

// Parse raw XMLTV episode-num strings into {season, episode}
function parseEpNum(raw) {
  if (!raw) return null;
  // xmltv_ns: "1.4.0"  → season=2, episode=5  (0-indexed)
  if (/^\\d+\\.\\d/.test(raw)) {
    const p = raw.split('.');
    const s = parseInt(p[0], 10) + 1;
    const e = parseInt(p[1], 10) + 1;
    return (s > 0 && e > 0) ? {season: s, episode: e} : null;
  }
  // onscreen: S02E05 or s2e5
  const m = raw.match(/[Ss](\\d+)[Ee](\\d+)/);
  if (m) return {season: parseInt(m[1], 10), episode: parseInt(m[2], 10)};
  // 2x05
  const m2 = raw.match(/^(\\d+)[xX](\\d+)$/);
  if (m2) return {season: parseInt(m2[1], 10), episode: parseInt(m2[2], 10)};
  return null;
}

function fmtSE(s, e) {
  return 'S' + String(s).padStart(2,'0') + 'E' + String(e).padStart(2,'0');
}

const _showMetaCache = new Map();  // title_lower → resolved meta object

async function fetchShowMeta(prog, capturedPopupProg) {
  const params = new URLSearchParams({title: prog.title});
  if (prog.subtitle)    params.set('subtitle',    prog.subtitle);
  if (prog.air_date)    params.set('air_date',    prog.air_date);
  if (prog.episode_num) params.set('episode_num', prog.episode_num);
  try {
    const cacheKey = prog.title.toLowerCase() + '|' + (prog.air_date||'') + '|' + (prog.subtitle||'');
    let meta = _showMetaCache.get(cacheKey);
    if (!meta) {
      meta = await (await fetch('/api/show_meta?' + params)).json();
      if (meta.imdb_id) _showMetaCache.set(cacheKey, meta);
    }
    if (!meta || !meta.imdb_id) return;
    if (popupProg !== capturedPopupProg) return;   // popup switched

    // Determine season — prefer TVMaze match, fall back to parsed EPG episode_num
    let season  = meta.season;
    let episode = meta.episode;
    if (!season) {
      const parsed = parseEpNum(prog.episode_num);
      if (parsed) { season = parsed.season; episode = parsed.episode; }
    }

    // Update IMDb button — best available link in priority order
    const imdbBtn = document.getElementById('popup-imdb');
    if (imdbBtn) {
      const _se  = (season && episode) ? fmtSE(season, episode) : season ? 'S' + String(season).padStart(2,'0') : '';
      let url, label;
      if (meta.ep_imdb_id) {
        // TVMaze has the episode-level IMDb tt-ID — direct episode page
        url   = 'https://www.imdb.com/title/' + meta.ep_imdb_id + '/';
        label = 'IMDb ' + _se;
      } else if (meta.ep_title) {
        // No episode tt-ID but we know the title — IMDb episode search (first result = this ep)
        url   = 'https://www.imdb.com/find/?q=' + encodeURIComponent((meta.name || prog.title) + ' ' + meta.ep_title) + '&type=episode';
        label = 'IMDb ' + _se;
      } else if (season) {
        // Fallback: season episodes list
        url   = 'https://www.imdb.com/title/' + meta.imdb_id + '/episodes/?season=' + season;
        label = 'IMDb ' + _se;
      } else {
        url   = 'https://www.imdb.com/title/' + meta.imdb_id;
        label = 'IMDb';
      }
      imdbBtn.href        = url;
      imdbBtn.textContent = label.trim() || 'IMDb';
    }

    // Fill description from TVMaze if EPG had none
    const descEl = document.getElementById('popup-desc');
    if (descEl && popupProg === capturedPopupProg && !prog.description) {
      const fill = meta.ep_desc || meta.summary;
      if (fill) descEl.textContent = fill;
    }

    // Fill / improve episode line if TVMaze found season+episode
    if (season && episode) {
      const epEl = document.getElementById('popup-ep');
      if (epEl && popupProg === capturedPopupProg) {
        const se   = fmtSE(season, episode);
        const sub  = meta.ep_title || prog.subtitle || '';
        epEl.textContent = sub ? se + '  —  ' + sub : se;
        epEl.style.display = '';
      }
    }
  } catch (_) {}
}

async function schedulePb(prog, ch) {
  try {
    const res  = await fetch('/api/schedule', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: ch.url, channel_name: ch.name, title: prog.title, start_ts: prog.start_ts})
    });
    const data = await res.json();
    if (data.ok) { _markTask(ch, prog, 'schedule', data.task_id); showToast('Scheduled: ' + prog.title + ' in ' + data.minutes_until + ' min'); }
    else           showToast('Schedule failed: ' + (data.error || 'unknown'), true);
  } catch (e) { showToast('Error: ' + e.message, true); }
}

async function remindPb(prog, ch) {
  try {
    const res  = await fetch('/api/remind', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start_ts: prog.start_ts, title: prog.title, channel_name: ch.name})
    });
    const data = await res.json();
    if (data.ok) { _markTask(ch, prog, 'remind', data.task_id); showToast('Reminder set for ' + prog.title + ' (in ' + data.minutes_until + ' min)'); }
    else           showToast('Reminder failed: ' + (data.error || 'unknown'), true);
  } catch (e) { showToast('Error: ' + e.message, true); }
}

// ── Toast ─────────────────────────────────────────────────────────────────
let _toastTimer;
function showToast(msg, err) {
  const t      = document.getElementById('toast');
  t.textContent= msg;
  t.className  = err ? 'err' : '';
  t.style.display = 'block';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.style.display = 'none'; }, 3500);
}

// ── Loading overlay ───────────────────────────────────────────────────────
function setLoading(show) {
  const el = document.getElementById('loading');
  if (show) {
    el.style.display = 'flex';
    document.getElementById('loading-msg').textContent = 'Loading guide…';
  } else {
    el.style.display = 'none';
  }
}

// ── Info bar ──────────────────────────────────────────────────────────────
function updateInfoBar() {
  if (!guideData) return;
  document.getElementById('info-ch-count').textContent = guideData.total + ' channels';
  if (loadedAt) {
    const age = Math.round((Date.now() - loadedAt) / 1000);
    document.getElementById('info-data-age').textContent = 'Loaded ' + age + 's ago';
  }
}
setInterval(() => { if (loadedAt) updateInfoBar(); }, 10000);

// ── Group filter ───────────────────────────────────────────────────────────
async function loadGroups() {
  try {
    const res  = await fetch('/api/groups');
    const data = await res.json();
    _groupsData = data.groups || [];
    updateGroupsBtnLabel();
  } catch (_) {}
}

function updateGroupsBtnLabel() {
  const btn = document.getElementById('btn-groups');
  if (selectedGroups.size === 0) {
    btn.textContent = 'Groups \u25be';
    btn.classList.remove('active');
  } else {
    btn.textContent = selectedGroups.size + ' Group' + (selectedGroups.size > 1 ? 's' : '') + ' \u25be';
    btn.classList.add('active');
  }
}

async function toggleGroupsPanel() {
  const panel = document.getElementById('groups-panel');
  if (panel.style.display !== 'none') { closeGroupsPanel(); return; }
  // Always refresh groups from server when opening — ensures they're populated
  // even if the panel was first opened before M3U had finished loading
  await loadGroups();
  renderGroupsPanel();
  // Position below the button
  const btn  = document.getElementById('btn-groups');
  const rect = btn.getBoundingClientRect();
  panel.style.display = '';
  panel.style.top  = (rect.bottom + 4) + 'px';
  panel.style.left = Math.max(4, rect.right - 240) + 'px';
}

function closeGroupsPanel() {
  document.getElementById('groups-panel').style.display = 'none';
}

function renderGroupsPanel() {
  const panel = document.getElementById('groups-panel');
  let html = '<div class="gp-actions">'
    + '<button class="gp-action-btn" onclick="selectAllGroups()">Select All</button>'
    + '<button class="gp-action-btn" onclick="clearAllGroups()">Clear All</button>'
    + '</div>';
  if (_groupsData.length === 0) {
    // U5: Show loading indicator instead of empty list
    html += '<div style="padding:12px 10px;color:var(--muted);font-size:12px;text-align:center">Loading\u2026</div>';
  } else {
    for (const g of _groupsData) {
      const chk  = selectedGroups.has(g.name) ? ' checked' : '';
      const vod  = g.is_vod ? '<span class="gp-vod-tag">VOD</span>' : '';
      html += '<label class="gp-item">'
            + '<input type="checkbox"' + chk + ' onchange="toggleGroup(' + escH(JSON.stringify(g.name)) + ',this.checked)">'
            + '<span class="gp-item-name">' + escH(g.name) + '</span>'
            + vod
            + '<span class="gp-item-count">' + g.count + '</span>'
            + '</label>';
    }
    html += '<div class="gp-apply-row">'
          + '<button class="gp-apply-btn" onclick="applyAndClose()">Apply Filter</button>'
          + '</div>';
  }
  panel.innerHTML = html;
}

let _applyTimer;
function toggleGroup(name, checked) {
  if (checked) selectedGroups.add(name);
  else         selectedGroups.delete(name);
  updateGroupsBtnLabel();
  clearTimeout(_applyTimer);
  _applyTimer = setTimeout(applyGroupFilter, 600);
}

function applyAndClose() {
  clearTimeout(_applyTimer);
  closeGroupsPanel();
  applyGroupFilter();
}

function selectAllGroups() {
  _groupsData.forEach(g => selectedGroups.add(g.name));
  renderGroupsPanel();
  updateGroupsBtnLabel();
  applyGroupFilter();
}

function clearAllGroups() {
  selectedGroups.clear();
  renderGroupsPanel();
  updateGroupsBtnLabel();
  applyGroupFilter();
}

function applyGroupFilter() {
  // Determine if all selected groups are VOD-only
  if (selectedGroups.size > 0) {
    const allVod = [...selectedGroups].every(n => {
      const g = _groupsData.find(d => d.name === n);
      return g && g.is_vod;
    });
    if (allVod) { switchToVodMode(); return; }
  }
  // Guide mode
  if (vodMode) switchToGuideMode();
  else loadGuide();
}

// ── VOD mode ───────────────────────────────────────────────────────────────
function switchToVodMode() {
  vodMode = true;
  vodPage = 1;
  document.getElementById('guide-wrap').style.display = 'none';
  document.getElementById('vod-view').style.display   = '';
  document.getElementById('btn-back').style.display   = 'none';
  document.getElementById('btn-now').style.display    = 'none';
  document.getElementById('btn-fwd').style.display    = 'none';
  document.getElementById('now-line').style.display   = 'none';
  // U1: Clear stale search state on mode switch
  searchQ = '';
  document.getElementById('search').value = '';
  clearSearchPanel();
  loadVod(1);
}

function switchToGuideMode() {
  vodMode = false;
  document.getElementById('vod-view').style.display   = 'none';
  document.getElementById('guide-wrap').style.display = '';
  document.getElementById('btn-back').style.display   = '';
  document.getElementById('btn-now').style.display    = '';
  document.getElementById('btn-fwd').style.display    = '';
  // U1: Clear stale search state on mode switch
  searchQ = '';
  document.getElementById('search').value = '';
  clearSearchPanel();
  loadGuide().then(jumpToNow);
}

async function loadVod(page) {
  vodPage = page;
  const p = new URLSearchParams({ page });
  if (searchQ) p.set('q', searchQ);
  p.set('sort', document.getElementById('vod-sort').value); // Fix-5: server-side sort
  selectedGroups.forEach(g => p.append('groups', g));
  try {
    const res  = await fetch('/api/vod?' + p);
    const data = await res.json();
    _vodTotal  = data.total;
    renderVodGrid(data);
  } catch (e) {
    showToast('VOD load error: ' + e.message, true);
  }
}

function renderVodGrid(data) {
  // Fix-5: Sort is now done server-side in /api/vod; items arrive pre-sorted
  const grid = document.getElementById('vod-grid');
  const frag = document.createDocumentFragment();

  for (const item of data.items) {
    const card    = mk('div', 'vod-card');
    card.title    = item.name;
    card.onclick  = () => play(item.url, item.name, '');

    // V1: Use <img> with onerror fallback instead of CSS background-image
    const poster = mk('div', 'vod-poster');
    const phElem = mk('div', 'vod-poster-placeholder');
    phElem.style.cssText = 'position:absolute;inset:0;display:none;';
    phElem.textContent = '\U0001F3AC';
    if (item.logo) {
      const img = mk('img', '');
      img.src   = item.logo;
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;object-position:center top;display:block;';
      img.onerror = () => { img.style.display = 'none'; phElem.style.display = 'flex'; };
      poster.appendChild(img);
    } else {
      phElem.style.display = 'flex';
    }
    poster.appendChild(phElem);
    const overlay = mk('div', 'vod-play-overlay');
    overlay.textContent = '\u25b6';
    poster.appendChild(overlay);
    card.appendChild(poster);

    const info  = mk('div', 'vod-info');
    const title = mk('div', 'vod-title');
    title.textContent = item.name;
    const grp   = mk('div', 'vod-group');
    grp.textContent   = item.group;
    info.appendChild(title);
    info.appendChild(grp);
    card.appendChild(info);
    frag.appendChild(card);
  }

  grid.innerHTML = '';
  grid.appendChild(frag);

  // Toolbar count
  const from = (data.page - 1) * 100 + 1;
  const to   = Math.min(data.page * 100, data.total);
  document.getElementById('vod-count').textContent =
    data.total === 0 ? 'No results' : `Showing ${from}\u2013${to} of ${data.total}`;

  // Pagination
  const pg = document.getElementById('vod-pagination');
  pg.innerHTML = '';
  if (data.pages > 1) {
    const prev = mk('button', 'btn');
    prev.textContent = '\u25c4 Prev';
    prev.disabled    = data.page <= 1;
    prev.onclick     = () => loadVod(data.page - 1);
    const info2 = mk('span', '');
    info2.textContent = `Page ${data.page} of ${data.pages}`;
    const next  = mk('button', 'btn');
    next.textContent = 'Next \u25ba';
    next.disabled    = data.page >= data.pages;
    next.onclick     = () => loadVod(data.page + 1);
    pg.appendChild(prev);
    pg.appendChild(info2);
    pg.appendChild(next);
  }
}

// ── Source selector ────────────────────────────────────────────────────────
async function loadSources() {
  try {
    const res  = await fetch('/api/sources');
    const data = await res.json();
    const sel  = document.getElementById('source-select');
    sel.innerHTML = '';
    for (const s of data.sources) {
      const opt       = document.createElement('option');
      opt.value       = s.index;
      opt.textContent = s.name;
      if (s.index === data.active) opt.selected = true;
      sel.appendChild(opt);
    }
    // Hide the selector if there's only one source
    sel.style.display = data.sources.length <= 1 ? 'none' : '';
  } catch (_) {}
}

async function switchSource(idx) {
  setLoading(true);
  document.getElementById('loading-msg').textContent = 'Switching source…';
  guideData = null;
  try {
    const res  = await fetch('/api/source', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ index: idx }),
    });
    const data = await res.json();
    if (!data.ok) {
      showToast('Error: ' + (data.error || 'unknown'), true);
      setLoading(false);
      return;
    }
    showToast('Loading ' + data.name + '…');
    await waitForData();
    selectedGroups.clear();
    await loadGroups();
    winStartTs = Math.floor(Date.now() / 1000) - 3600;
    if (vodMode) switchToGuideMode();
    await loadGuide();
    jumpToNow();
  } catch (e) {
    showToast('Error: ' + e.message, true);
    setLoading(false);
  }
}

async function waitForData(maxSec = 120) {
  for (let i = 0; i < maxSec; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const res = await fetch('/api/status');
      const st  = await res.json();
      if (!st.data_loading) return;
      document.getElementById('loading-msg').textContent =
        'Loading source… ' + (i + 1) + 's';
    } catch (_) {}
  }
  // U2: Notify user when load takes longer than expected
  showToast('Data is taking longer than expected — try \u27f3 to refresh', true);
}

// ── Search panel (server-side, mirrors CLI search logic) ───────────────────
let _searchTimer;
let _srChannels = [];
let _srShows    = [];

async function fetchSearch(q) {
  try {
    // S2: Include active group filter so search is scoped to visible groups
    const p = new URLSearchParams({ q, hours: 24 });
    selectedGroups.forEach(g => p.append('groups', g));
    const res  = await fetch('/api/search?' + p);
    const data = await res.json();
    // B5: Handle loading state
    if (data.loading) {
      const panel = document.getElementById('search-panel');
      panel.innerHTML = '<div class="sp-empty">Loading data\u2026 try again in a moment.</div>';
      panel.style.display = '';
      return;
    }
    renderSearchPanel(data, q);
  } catch (_) {}
}

function renderSearchPanel(data, q) {
  _srChannels = data.channels || [];
  _srShows    = data.shows    || [];
  const panel = document.getElementById('search-panel');

  if (!_srChannels.length && !_srShows.length) {
    panel.innerHTML = '<div class="sp-empty">No results for \u201c' + escH(q) + '\u201d</div>';
    panel.style.display = '';
    return;
  }

  let html = '';

  if (_srChannels.length) {
    html += '<div class="sp-section"><div class="sp-label">Channels (' + _srChannels.length + ')</div>';
    for (let i = 0; i < _srChannels.length; i++) {
      const ch = _srChannels[i];
      html += '<div class="sp-item" onclick="spPlayCh(' + i + ')">'
            + '<div class="sp-col"><div class="sp-name">' + escH(ch.name) + '</div></div>'
            + (ch.group ? '<span class="sp-group-badge">' + escH(ch.group) + '</span>' : '')
            + '</div>';
    }
    html += '</div>';
  }

  if (_srShows.length) {
    if (_srChannels.length) html += '<div class="sp-divider"></div>';
    html += '<div class="sp-section"><div class="sp-label">Shows \u2014 now &amp; next 24h (' + _srShows.length + ')</div>';
    for (let i = 0; i < _srShows.length; i++) {
      const s  = _srShows[i];
      const bc = s.is_now ? 'now' : 'soon';
      const sub = s.subtitle ? ' <span style="font-weight:400;color:var(--muted)">\u2013 ' + escH(s.subtitle) + '</span>' : '';
      const ep  = s.episode_num ? '  \u00b7  ' + escH(s.episode_num) : '';
      // S1: Show "NEW" badge when air_date is within the last 7 days
      const newBadge = s.is_new ? '<span class="sp-badge" style="background:#14532d;color:#4ade80;margin-right:4px">NEW</span>' : '';
      html += '<div class="sp-item" onclick="spOpenShow(' + i + ')">'
            + '<div class="sp-col">'
            + '<div class="sp-name">' + escH(s.title) + sub + '</div>'
            + '<div class="sp-sub">' + escH(s.channel_name) + ep + '</div>'
            + '</div>'
            + newBadge
            + '<span class="sp-badge ' + bc + '">' + escH(s.time_status) + '</span>'
            + '</div>';
    }
    html += '</div>';
  }

  panel.innerHTML = html;
  panel.style.display = '';
}

function clearSearchPanel() {
  const p = document.getElementById('search-panel');
  p.style.display = 'none';
}

function spPlayCh(idx) {
  const ch = _srChannels[idx];
  if (ch) { play(ch.url, ch.name, ''); clearSearchPanel(); }
}

function spOpenShow(idx) {
  const s = _srShows[idx];
  if (!s) return;
  clearSearchPanel();
  const stopTs = s.stop_ts || (s.start_ts + s.duration_min * 60);
  showPopup(
    { title: s.title, subtitle: s.subtitle, description: s.description,
      episode_num: s.episode_num, start_ts: s.start_ts, stop_ts: stopTs,
      duration_min: s.duration_min },
    { name: s.channel_name, group: s.channel_group, url: s.channel_url }
  );
}

// ── Recording ──────────────────────────────────────────────────────────────
let _recTimer = null;

async function startRecording(url, channel, show) {
  try {
    const res  = await fetch('/api/record', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url, channel, show }),
    });
    const data = await res.json();
    if (data.ok) {
      showToast('\u23fa Recording: ' + channel + (show ? ' \u2014 ' + show : ''));
      setTimeout(updateRecordings, 600);
      return data.id;
    } else {
      showToast('Record error: ' + (data.error || 'unknown'), true);
      return null;
    }
  } catch (e) { showToast('Error: ' + e.message, true); return null; }
}

async function stopRecording(id) {
  await fetch('/api/recording/' + id + '/stop', { method: 'POST' });
  updateRecordings();
}

async function updateRecordings() {
  try {
    const res  = await fetch('/api/recordings');
    const data = await res.json();
    const recs  = (data.recordings || []).filter(r => r.running);
    const panel = document.getElementById('rec-panel');
    const list  = document.getElementById('rec-list');

    if (recs.length === 0) {
      panel.style.display = 'none';
      if (_recTimer) { clearInterval(_recTimer); _recTimer = null; }
      return;
    }

    panel.style.display = '';
    if (!_recTimer) _recTimer = setInterval(updateRecordings, 5000);

    list.innerHTML = recs.map(r => {
      const label = r.show ? escH(r.channel) + ' \u2014 ' + escH(r.show) : escH(r.channel);
      return '<div class="rec-item">'
        + '<div class="rec-info">'
        + '<div class="rec-ch">' + label + '</div>'
        + '<div class="rec-dur">' + fmtDur(r.duration_s) + '  \u00b7  ' + escH(r.filename) + '</div>'
        + '</div>'
        + '<button class="rec-stop-btn" onclick="stopRecording(\\'' + r.id + '\\')">&#9632; Stop</button>'
        + '</div>';
    }).join('');
  } catch (_) {}
}

function fmtDur(s) {
  const h   = Math.floor(s / 3600);
  const m   = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return h + 'h ' + String(m).padStart(2, '0') + 'm';
  return m + 'm ' + String(sec).padStart(2, '0') + 's';
}

function escH(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
</script>
</body>
</html>"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True, "channels": len(_channels), "epg": len(_epg)})


@app.route("/")
def index():
    resp = Response(HTML_PAGE, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/guide")
def api_guide():
    _t0      = time.time()
    print(f"api_guide: request received (channels={len(_channels)}, epg={len(_epg)})")
    start_ts = request.args.get("start_ts", type=float)
    hours    = request.args.get("hours", default=8, type=int)
    hours    = max(1, min(hours, 24))

    now    = datetime.now().astimezone()
    now_ts = now.timestamp()

    if start_ts is None:
        start_ts = now_ts - 3600
    end_ts = start_ts + hours * 3600

    with _data_lock:
        channels = list(_channels)
        epg      = dict(_epg)
    print(f"api_guide: lock released in {time.time()-_t0:.3f}s")

    if _data_loading and not channels:
        return jsonify({"error": "Guide data is still loading — please wait and refresh.", "channels": [], "total": 0})

    # Sort channels by watch history (most watched first) — B4: use cached version
    history   = load_watch_history_cached()
    watch_map = {e.get("url", ""): e.get("total_duration_seconds", 0) for e in history}
    channels.sort(key=lambda c: watch_map.get(c.get("url", ""), 0), reverse=True)
    print(f"api_guide: sorted {len(channels)} channels in {time.time()-_t0:.3f}s")

    grp_filter = set(request.args.getlist("groups"))

    # Build channel list with programmes in the window
    # Fix-3: Count total matching channels but only build EPG data for the first 200
    result = []
    total  = 0
    for ch in channels:
        if grp_filter and ch.get("group-title", "") not in grp_filter:
            continue
        total += 1
        if total > 200:
            continue  # still count, but skip the expensive EPG build

        ch_name = ch.get("name", "")
        ch_grp  = ch.get("group-title", "")
        tvg_id  = ch.get("tvg-id", "")

        progs = []
        if tvg_id in epg:
            for p in epg[tvg_id]:
                st  = p.get("start_time")
                et  = p.get("stop_time")
                if not st or not et:
                    continue
                st_ts = st.timestamp()
                et_ts = et.timestamp()
                if et_ts <= start_ts or st_ts >= end_ts:
                    continue
                desc = p.get("description", "")
                progs.append({
                    "title":       p.get("title", ""),
                    "subtitle":    p.get("subtitle", ""),
                    "description": desc[:400] if len(desc) > 400 else desc,
                    "episode_num": p.get("episode_num", ""),
                    "air_date":    p.get("air_date", ""),
                    "start_ts":    int(st_ts),
                    "stop_ts":     int(et_ts),
                    "duration_min":max(1, int((et_ts - st_ts) / 60)),
                    "is_now":      st_ts <= now_ts < et_ts,
                })

        result.append({
            "tvg_id":  tvg_id,
            "name":    ch_name,
            "group":   ch_grp,
            "url":     ch.get("url", ""),
            "programs": progs,
        })

    print(f"api_guide: built result ({len(result)} ch, {sum(len(c['programs']) for c in result)} progs) in {time.time()-_t0:.3f}s")
    resp = jsonify({
        "window_start_ts": int(start_ts),
        "window_end_ts":   int(end_ts),
        "now_ts":          int(now_ts),
        "channels":        result,
        "total":           total,
    })
    print(f"api_guide: jsonify done, total {time.time()-_t0:.3f}s")
    return resp


@app.route("/api/status")
def api_status():
    st     = mpv_status()
    now_ts = int(time.time())
    # F1: Detect mpv dying within the last 15 seconds (stream failure feedback)
    mpv_just_died = (
        not st.get("playing") and
        "exit_code" in st and
        st.get("exit_time", 0) >= now_ts - 15
    )
    return jsonify({
        **st,
        "vpn_configured": _vpn_configured,
        "vpn_connected":  vpn_is_connected(),
        "vpn_ip":         (get_public_ip_cached() if vpn_is_connected() else None),  # B3: cached
        "data_loading":   _data_loading,
        "data_version":   _data_version,
        "mpv_just_died":  mpv_just_died,
        "mpv_exit_code":  st.get("exit_code"),
    })


def _parse_ep_num(raw: str):
    """Parse XMLTV episode-num to (season, episode). Returns None if not parseable."""
    if not raw:
        return None
    # xmltv_ns: "8.3.0" → Season 9, Episode 4  (0-indexed)
    if re.match(r"^\d+\.\d", raw):
        parts = raw.split(".")
        s, e = int(parts[0]) + 1, int(parts[1]) + 1
        return (s, e) if s > 0 and e > 0 else None
    m = re.match(r"[Ss](\d+)[Ee](\d+)", raw)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d+)[xX](\d+)$", raw)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def _ep_cached_entry(ep: dict) -> dict:
    """Build a cache entry from a TVMaze episode object."""
    return {
        "season":       ep.get("season"),
        "episode":      ep.get("number"),
        "ep_title":     ep.get("name", ""),
        "ep_desc":      re.sub(r"<[^>]+>", "", ep.get("summary") or ""),
        "tvmaze_ep_id": ep.get("id"),          # used to fetch episode-level IMDb ID
        "_ts":          time.time(),
    }


def _fetch_ep_imdb_id(tvmaze_ep_id: int) -> Optional[str]:
    """Return the episode-level IMDb tt-ID from TVMaze, cached 24 h."""
    if not tvmaze_ep_id:
        return None
    cache_key = f"epimdb|{tvmaze_ep_id}"
    with _tvmaze_lock:
        cached = _tvmaze_ep_cache.get(cache_key)
    if cached and time.time() - cached.get("_ts", 0) < 86400:
        return cached.get("ep_imdb_id")
    try:
        r = requests.get(f"https://api.tvmaze.com/episodes/{tvmaze_ep_id}", timeout=5)
        if r.status_code == 200:
            ep_imdb = (r.json().get("externals") or {}).get("imdb")
            with _tvmaze_lock:
                _tvmaze_ep_cache[cache_key] = {"ep_imdb_id": ep_imdb, "_ts": time.time()}
            return ep_imdb
    except Exception:
        pass
    return None


@app.route("/api/show_meta")
def api_show_meta():
    """Return TVMaze show + episode metadata for the IMDb popup link."""
    title       = request.args.get("title",       "").strip()
    subtitle    = request.args.get("subtitle",    "").strip()
    air_date    = request.args.get("air_date",    "").strip()   # YYYYMMDD
    episode_num = request.args.get("episode_num", "").strip()   # raw XMLTV episode-num
    if not title:
        return jsonify({"error": "No title"}), 400

    title_key = re.sub(r"\s+", " ", title.lower())

    # ── 1. Fetch show-level info (24 h cache) ──────────────────────────────
    with _tvmaze_lock:
        show_cached = _tvmaze_show_cache.get(title_key)

    if not show_cached or time.time() - show_cached.get("_ts", 0) > 86400:
        try:
            r = requests.get("https://api.tvmaze.com/singlesearch/shows",
                             params={"q": title}, timeout=5)
            if r.status_code == 404:
                return jsonify({"error": "Show not found on TVMaze"}), 404
            r.raise_for_status()
            d = r.json()
            show_cached = {
                "id":      d["id"],
                "name":    d.get("name", ""),
                "imdb_id": (d.get("externals") or {}).get("imdb"),
                "summary": re.sub(r"<[^>]+>", "", d.get("summary") or ""),
                "_ts":     time.time(),
            }
            with _tvmaze_lock:
                _tvmaze_show_cache[title_key] = show_cached
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    show_id = show_cached["id"]
    result  = {k: v for k, v in show_cached.items() if k not in ("_ts", "id")}

    # ── 2. Find specific episode ────────────────────────────────────────────
    ep_info   = None
    ep_cached = None

    # Strategy A: episode_num present → /episodebynumber (fastest, precise)
    parsed = _parse_ep_num(episode_num)
    if parsed:
        s, e   = parsed
        ep_key = f"{show_id}|se|{s}|{e}"
        with _tvmaze_lock:
            ep_cached = _tvmaze_ep_cache.get(ep_key)
        if not ep_cached or time.time() - ep_cached.get("_ts", 0) > 86400:
            try:
                r = requests.get(f"https://api.tvmaze.com/shows/{show_id}/episodebynumber",
                                 params={"season": s, "number": e}, timeout=5)
                if r.status_code == 200:
                    ep_cached = _ep_cached_entry(r.json())
                    with _tvmaze_lock:
                        _tvmaze_ep_cache[ep_key] = ep_cached
                else:
                    ep_cached = {"season": s, "episode": e, "_ts": time.time()}
            except Exception:
                ep_cached = {"season": s, "episode": e, "_ts": time.time()}
        if ep_cached and ep_cached.get("season"):
            ep_info = {k: v for k, v in ep_cached.items() if k != "_ts"}

    # Strategy B: original air_date → episodesbydate
    if not ep_info and air_date and len(air_date) >= 8:
        ep_key = f"{show_id}|date|{air_date[:8]}"
        with _tvmaze_lock:
            ep_cached = _tvmaze_ep_cache.get(ep_key)
        if not ep_cached or time.time() - ep_cached.get("_ts", 0) > 86400:
            try:
                date_str = f"{air_date[:4]}-{air_date[4:6]}-{air_date[6:8]}"
                r = requests.get(f"https://api.tvmaze.com/shows/{show_id}/episodesbydate",
                                 params={"date": date_str}, timeout=5)
                if r.status_code == 200:
                    eps = r.json()
                    if eps:
                        ep_cached = _ep_cached_entry(eps[0])
                        with _tvmaze_lock:
                            _tvmaze_ep_cache[ep_key] = ep_cached
            except Exception:
                pass
        if ep_cached and ep_cached.get("season"):
            ep_info = {k: v for k, v in ep_cached.items() if k != "_ts"}

    # Strategy C: fuzzy subtitle scan across all episodes
    if not ep_info and subtitle:
        sub_norm = re.sub(r"[.\s…]+$", "", subtitle).lower()
        ep_key   = f"{show_id}|sub|{sub_norm[:60]}"
        with _tvmaze_lock:
            ep_cached = _tvmaze_ep_cache.get(ep_key)
        if not ep_cached or time.time() - ep_cached.get("_ts", 0) > 86400:
            try:
                r = requests.get(f"https://api.tvmaze.com/shows/{show_id}/episodes",
                                 timeout=8)
                if r.status_code == 200:
                    def _norm(t): return re.sub(r"[.\s…]+$", "", t).lower()
                    match = next(
                        (e for e in r.json()
                         if _norm(e.get("name", "")).startswith(sub_norm)
                         or sub_norm.startswith(_norm(e.get("name", "")))),
                        None
                    )
                    if match:
                        ep_cached = _ep_cached_entry(match)
                        with _tvmaze_lock:
                            _tvmaze_ep_cache[ep_key] = ep_cached
            except Exception:
                pass
        if ep_cached and ep_cached.get("season"):
            ep_info = {k: v for k, v in ep_cached.items() if k != "_ts"}

    # ── 3. Enrich with episode-level IMDb ID ───────────────────────────────
    if ep_info and ep_info.get("tvmaze_ep_id"):
        ep_imdb = _fetch_ep_imdb_id(ep_info["tvmaze_ep_id"])
        if ep_imdb:
            ep_info["ep_imdb_id"] = ep_imdb

    if ep_info:
        result.update({k: v for k, v in ep_info.items() if k != "tvmaze_ep_id"})

    return jsonify(result)


@app.route("/api/remind", methods=["POST"])
def api_remind():
    data       = request.get_json(force=True) or {}
    start_ts   = int(data.get("start_ts", 0))
    title      = data.get("title", "Unknown")
    ch_name    = data.get("channel_name", "")
    now_ts     = int(datetime.now().astimezone().timestamp())
    if start_ts <= now_ts:
        return jsonify({"ok": False, "error": "Show has already started"}), 400
    delay = max(0, start_ts - now_ts - 60)
    minutes_until = max(0, (start_ts - now_ts) // 60)

    task_id    = int(time.time() * 1000)
    cancel_evt = threading.Event()
    with _web_tasks_lock:
        _web_tasks.append({"id": task_id, "type": "remind", "title": title,
                           "ch_name": ch_name, "ch_url": data.get("ch_url", ""),
                           "start_ts": start_ts, "_cancel": cancel_evt})

    def _fire():
        if cancel_evt.wait(timeout=delay):
            print(f"[REMIND] '{title}' — cancelled")
            return
        send_desktop_notification("Term-TV Reminder", f"Starting in ~1 min: {title}")
        print(f"[REMINDER] '{title}' — notification fired")
        with _web_tasks_lock:
            _web_tasks[:] = [t for t in _web_tasks if t["id"] != task_id]

    threading.Thread(target=_fire, daemon=True).start()
    print(f"[REMIND] '{title}' on {ch_name} — notification in {minutes_until} min")
    return jsonify({"ok": True, "minutes_until": minutes_until, "task_id": task_id})


@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data       = request.get_json(force=True) or {}
    url        = data.get("url", "").strip()
    ch_name    = data.get("channel_name", "")
    title      = data.get("title", "Unknown")
    start_ts   = int(data.get("start_ts", 0))
    if not url:
        return jsonify({"ok": False, "error": "No URL provided"}), 400
    now_ts = int(datetime.now().astimezone().timestamp())
    if start_ts <= now_ts:
        return jsonify({"ok": False, "error": "Show has already started"}), 400
    delay = max(0, start_ts - now_ts)
    minutes_until = max(0, delay // 60)

    task_id    = int(time.time() * 1000)
    cancel_evt = threading.Event()
    with _web_tasks_lock:
        _web_tasks.append({"id": task_id, "type": "schedule", "title": title,
                           "ch_name": ch_name, "ch_url": url, "start_ts": start_ts,
                           "_cancel": cancel_evt})

    def _schedule():
        notify_wait = max(0, delay - 300)
        if cancel_evt.wait(timeout=notify_wait):
            print(f"[SCHEDULE] '{title}' — cancelled")
            return
        if notify_wait > 0:
            send_desktop_notification("Term-TV", f"Starting in 5 min: {title}")
            print(f"[SCHEDULE] '{title}' — 5-min notification sent")
        if cancel_evt.wait(timeout=delay - notify_wait):
            print(f"[SCHEDULE] '{title}' — cancelled before launch")
            return
        with _web_tasks_lock:
            _web_tasks[:] = [t for t in _web_tasks if t["id"] != task_id]
        try:
            subprocess.Popen(["mpv", "--stream-lavf-o=timeout=10000000", url])
            print(f"[SCHEDULE] '{title}' on {ch_name} — mpv launched")
        except Exception as e:
            print(f"[SCHEDULE] '{title}' — launch failed: {e}", file=sys.stderr)

    threading.Thread(target=_schedule, daemon=True).start()
    print(f"[SCHEDULE] '{title}' on {ch_name} — playback in {minutes_until} min")
    return jsonify({"ok": True, "minutes_until": minutes_until, "task_id": task_id})


@app.route("/api/play", methods=["POST"])
def api_play():
    data    = request.get_json(force=True) or {}
    url     = data.get("url", "").strip()
    channel = data.get("channel", "").strip()
    show    = data.get("show", "").strip()

    if not url:
        return jsonify({"ok": False, "error": "No URL provided"}), 400

    ok = launch_mpv(url, channel, show)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Failed to launch mpv — is it installed?"}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_mpv()
    return jsonify({"ok": True})


@app.route("/api/record", methods=["POST"])
def api_record():
    data    = request.get_json(force=True) or {}
    url     = data.get("url", "").strip()
    channel = data.get("channel", "").strip()
    show    = data.get("show", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL provided"}), 400
    result = launch_recording(url, channel, show)
    return jsonify(result), (200 if result["ok"] else 500)


@app.route("/api/scheduled")
def api_scheduled():
    with _web_tasks_lock:
        tasks = [{k: v for k, v in t.items() if k != "_cancel"} for t in _web_tasks]
    with _recordings_lock:
        recs = [{"id": r["id"], "type": "record",
                 "title": r.get("show", r.get("channel", "")),
                 "ch_name": r.get("channel", ""), "ch_url": r.get("url", ""),
                 "start_ts": 0}
                for r in _recordings.values() if r.get("process") and r["process"].poll() is None]
    return jsonify({"tasks": tasks + recs})


@app.route("/api/scheduled/<int:task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id):
    with _web_tasks_lock:
        for t in _web_tasks:
            if t["id"] == task_id:
                ev = t.get("_cancel")
                if ev:
                    ev.set()
                break
        _web_tasks[:] = [t for t in _web_tasks if t["id"] != task_id]
    print(f"[CANCEL] task {task_id} cancelled")
    return jsonify({"ok": True})


@app.route("/api/recordings")
def api_recordings():
    # Prune finished recordings (clean up dead processes)
    with _recordings_lock:
        dead = [rid for rid, r in _recordings.items()
                if r.get("process") and r["process"].poll() is not None]
    for rid in dead:
        stop_recording(rid)
    return jsonify({"recordings": recordings_status()})


@app.route("/api/recording/<rec_id>/stop", methods=["POST"])
def api_stop_recording(rec_id):
    ok = stop_recording(rec_id)
    return jsonify({"ok": ok})


@app.route("/api/vpn/connect", methods=["POST"])
def api_vpn_connect():
    if not _vpn_exe or not _vpn_config_file:
        return jsonify({"ok": False, "error": "No VPN config loaded"}), 400
    if vpn_is_connected():
        return jsonify({"ok": True, "msg": "Already connected"})
    ok = connect_vpn(_vpn_exe, _vpn_config_file, _vpn_expected_ip)
    return jsonify({"ok": ok})


@app.route("/api/vpn/disconnect", methods=["POST"])
def api_vpn_disconnect():
    disconnect_vpn()
    return jsonify({"ok": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=_load_data, daemon=True).start()
    return jsonify({"ok": True, "msg": "Refreshing in background"})


@app.route("/api/search")
def api_search():
    q     = request.args.get("q", "").strip()
    hours = request.args.get("hours", default=24, type=int)
    if len(q) < 2:
        return jsonify({"channels": [], "shows": []})

    # B5: Return loading indicator if data hasn't arrived yet
    if _data_loading and not _channels:
        return jsonify({"channels": [], "shows": [], "loading": True})

    # S2: Respect active group filter
    grp_filter = set(request.args.getlist("groups"))

    with _data_lock:
        channels = list(_channels)

    ch_results = [
        {"name": c.get("name", ""), "group": c.get("group-title", ""), "url": c.get("url", "")}
        for c in channels
        if q.lower() in c.get("name", "").lower()
        and (not grp_filter or c.get("group-title", "") in grp_filter)
    ]

    show_results = search_shows_web(q, hours_ahead=hours, groups=grp_filter if grp_filter else None)

    return jsonify({
        "channels": ch_results[:20],
        "shows":    show_results[:60],
    })


@app.route("/api/groups")
def api_groups():
    with _data_lock:
        channels = list(_channels)
    groups: Dict[str, Dict] = {}
    for ch in channels:
        g = ch.get("group-title", "") or "Ungrouped"
        if g not in groups:
            groups[g] = {"name": g, "count": 0, "vod_count": 0}
        groups[g]["count"] += 1
        if _is_vod_ch(ch):
            groups[g]["vod_count"] += 1
    result = []
    for info in sorted(groups.values(), key=lambda x: x["name"]):
        info["is_vod"] = info["vod_count"] > info["count"] * 0.5
        result.append(info)
    return jsonify({"groups": result})


@app.route("/api/vod")
def api_vod():
    q          = request.args.get("q", "").strip().lower()
    grp_filter = set(request.args.getlist("groups"))
    sort       = request.args.get("sort", "az")
    page       = request.args.get("page", 1, type=int)
    per_page   = 100

    with _data_lock:
        channels = list(_channels)

    items = []
    for ch in channels:
        if not _is_vod_ch(ch):
            continue
        grp = ch.get("group-title", "")
        if grp_filter and grp not in grp_filter:
            continue
        name = ch.get("name", "")
        if q and q not in name.lower():
            continue
        items.append({
            "name":  name,
            "url":   ch.get("url", ""),
            "group": grp,
            "logo":  ch.get("tvg-logo", ""),
        })

    # Fix-5: Sort server-side so pagination is consistent across all pages
    if sort == "za":
        items.sort(key=lambda x: x["name"].lower(), reverse=True)
    elif sort == "group":
        items.sort(key=lambda x: (x["group"].lower(), x["name"].lower()))
    else:  # az default
        items.sort(key=lambda x: x["name"].lower())

    total = len(items)
    start = (page - 1) * per_page
    return jsonify({
        "items":  items[start:start + per_page],
        "total":  total,
        "page":   page,
        "pages":  max(1, (total + per_page - 1) // per_page),
    })


@app.route("/api/sources")
def api_sources():
    playlists = _config.get("playlists", [])
    return jsonify({
        "sources": [{"index": i, "name": p.get("name", f"Playlist {i + 1}")} for i, p in enumerate(playlists)],
        "active":  _active_playlist_idx,
    })


@app.route("/api/source", methods=["POST"])
def api_set_source():
    global _active_playlist_idx, _data_loading, _data_version, _channels, _epg
    data      = request.get_json(force=True) or {}
    idx       = data.get("index", 0)
    playlists = _config.get("playlists", [])
    if not isinstance(idx, int) or not (0 <= idx < len(playlists)):
        return jsonify({"ok": False, "error": "Invalid source index"}), 400
    with _data_lock:
        _active_playlist_idx = idx
        _channels            = []   # B1: clear stale channels immediately
        _epg                 = {}   # B1: clear stale EPG immediately
        _data_loading        = True # B2: set flag inside lock
    threading.Thread(target=_load_data, daemon=True).start()
    return jsonify({"ok": True, "name": playlists[idx].get("name", f"Playlist {idx + 1}")})


# ── Background data loader ────────────────────────────────────────────────────

def _load_data():
    global _channels, _epg, _data_loading, _data_version, _active_playlist_idx
    playlists = _config.get("playlists", [])

    # B6: Guard against out-of-range playlist index
    with _data_lock:
        idx = _active_playlist_idx
    if playlists and idx >= len(playlists):
        logging.error(f"Playlist index {idx} out of range ({len(playlists)} playlists), resetting to 0")
        with _data_lock:
            _active_playlist_idx = 0
        idx = 0
    playlist = playlists[idx] if playlists else {}

    try:
        if playlist.get("m3u_url"):
            new_ch = load_m3u_cached(playlist["m3u_url"])
            if new_ch:
                with _data_lock:
                    _channels = new_ch
    except Exception as e:
        logging.error(f"M3U load error: {e}")

    try:
        if playlist.get("epg_url"):
            new_epg = load_epg(playlist["epg_url"])
            if new_epg:
                with _data_lock:
                    _epg = new_epg
    except Exception as e:
        logging.error(f"EPG load error: {e}")

    with _data_lock:
        _data_loading = False
        _data_version += 1
    print(f"✓ Guide data ready  ({len(_channels)} channels, {len(_epg)} EPG entries)")


# ── Open browser after short delay ───────────────────────────────────────────

def _open_browser():
    # Wait until M3U + EPG are fully loaded before opening the browser
    while _data_loading:
        time.sleep(0.5)
    time.sleep(0.3)  # tiny extra delay so Flask can finish any in-flight work
    webbrowser.open_new_tab(WEB_URL + f"?_={int(time.time())}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global _config, _vpn_configured

    parser = argparse.ArgumentParser(description="Term-TV Web: Browser-based IPTV guide")
    parser.add_argument("--skip-vpn", action="store_true", help="Skip VPN prompt at startup")
    args = parser.parse_args()

    setup_logging()
    atexit.register(archive_mpv_log)
    atexit.register(disconnect_vpn)

    # Load config
    if not CONFIG_FILE.exists():
        print(f"Error: {CONFIG_FILE} not found.", file=sys.stderr)
        sys.exit(1)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid config.json — {e}", file=sys.stderr)
        sys.exit(1)

    playlists = _config.get("playlists", [])
    if not playlists:
        print("Error: No playlists defined in config.json.", file=sys.stderr)
        sys.exit(1)

    # VPN setup (optional)
    vpn_cfg = _config.get("openvpn", {})
    if not args.skip_vpn and vpn_cfg.get("enabled") and vpn_cfg.get("auto_connect"):
        _vpn_configured = True
        try:
            ans = input("Connect VPN? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans != "y":
            print("VPN skipped.")
        else:
            ovpn_file  = vpn_cfg.get("config_file", "")
            config_exe = vpn_cfg.get("executable", "")
            exp_ip     = _config.get("vpn_ip")

            if not ovpn_file:
                print("⚠  openvpn.config_file not set in config.json", file=sys.stderr)
            else:
                openvpn_exe = find_openvpn_executable(config_exe)
                if not openvpn_exe:
                    print("⚠  OpenVPN executable not found — VPN auto-connect skipped.", file=sys.stderr)
                else:
                    if not check_admin_privileges():
                        print("⚠  Not running as administrator — VPN auto-connect skipped.", file=sys.stderr)
                    elif connect_vpn(openvpn_exe, ovpn_file, exp_ip):
                        _register_vpn_signal_handlers()
                    else:
                        print("⚠  VPN connection failed — continuing without VPN.", file=sys.stderr)
    elif not args.skip_vpn and vpn_cfg.get("enabled"):
        _vpn_configured = True

    # Start data loading in background
    threading.Thread(target=_load_data, daemon=True).start()

    # Open browser
    threading.Thread(target=_open_browser, daemon=True).start()

    print(f"\n{'='*60}")
    print(f"  Term-TV Web Guide")
    print(f"  Open: {WEB_URL}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*60}\n")

    try:
        app.run(host=WEB_HOST, port=WEB_PORT, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()