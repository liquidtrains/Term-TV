"""
Shared core library for Term-TV scripts.

All data-loading, parsing, search, watch-history, and recording-utility
functions live here so Term-TV.py, Term-TV-VPN.py and Term-TV-Web.py share a
single authoritative implementation.
"""

import sys
import re
import gzip
import zlib
import xml.etree.ElementTree as ET
from io import BytesIO
import json
import subprocess
import hashlib
import logging
import platform
import time
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone

import requests

# Platform-specific imports for input_with_countdown
if platform.system() == "Windows":
    import msvcrt
else:
    import select

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
Channel = Dict[str, str]
EpgData = Dict[str, List[Dict[str, Any]]]
ShowResult = Dict[str, Any]

# ---------------------------------------------------------------------------
# Shared file-path constants (relative to CWD where the script is launched)
# ---------------------------------------------------------------------------
WATCH_HISTORY_FILE  = Path(".watch_history.json")
SEARCH_HISTORY_FILE = Path(".search_history.json")
FAVORITES_FILE      = Path(".favorites.json")
RECORDINGS_DIR      = Path.home() / "Videos" / "Recordings"
EPG_CACHE_DIR       = Path(".epg_cache")
M3U_CACHE_DIR       = Path(".m3u_cache")

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def clean_old_cache_files(max_age_days: int = 15):
    """Remove cached files older than max_age_days from EPG/M3U cache dirs."""
    now = datetime.now()
    max_age_seconds = max_age_days * 24 * 60 * 60
    total_deleted = 0
    total_freed_bytes = 0

    for cache_dir in (EPG_CACHE_DIR, M3U_CACHE_DIR):
        if not cache_dir.exists():
            continue
        for file_path in cache_dir.iterdir():
            if not file_path.is_file():
                continue
            try:
                # Delete .meta sidecars whose data file no longer exists
                if file_path.suffix == ".meta":
                    stem = file_path.stem  # e.g. "abc123"
                    siblings = list(cache_dir.glob(f"{stem}.*"))
                    has_data = any(s != file_path for s in siblings)
                    if not has_data:
                        st = file_path.stat()
                        file_path.unlink()
                        total_deleted += 1
                        total_freed_bytes += st.st_size
                        logging.info(f"Deleted orphaned meta file: {file_path.name}")
                    continue

                st = file_path.stat()
                age_seconds = (now - datetime.fromtimestamp(st.st_mtime)).total_seconds()
                if age_seconds > max_age_seconds:
                    # Delete the data file and its .meta sidecar together
                    meta_file = file_path.with_suffix(".meta")
                    file_path.unlink()
                    total_deleted += 1
                    total_freed_bytes += st.st_size
                    logging.info(f"Deleted old cache file ({age_seconds/86400:.1f}d): {file_path.name}")
                    if meta_file.exists():
                        meta_st = meta_file.stat()
                        meta_file.unlink()
                        total_deleted += 1
                        total_freed_bytes += meta_st.st_size
                        logging.info(f"Deleted paired meta file: {meta_file.name}")
            except Exception as e:
                logging.warning(f"Error checking/deleting cache file {file_path}: {e}")

    if total_deleted > 0:
        freed_mb = total_freed_bytes / (1024 * 1024)
        logging.info(f"Cache cleanup: deleted {total_deleted} file(s), freed {freed_mb:.2f} MB")
        print(f"Cache cleanup: Removed {total_deleted} old cache file(s) ({freed_mb:.2f} MB)")

# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------

def load_search_history() -> List[str]:
    """Return recent successful search terms (most recent first)."""
    if not SEARCH_HISTORY_FILE.exists():
        return []
    try:
        with open(SEARCH_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("searches", [])
    except Exception as e:
        logging.warning(f"Failed to load search history: {e}")
        return []


def save_search_history(searches: List[str], max_entries: int = 5):
    """Persist search history, keeping only the most recent unique entries."""
    unique: List[str] = []
    seen: set = set()
    for s in searches:
        key = s.lower()
        if key not in seen:
            unique.append(s)
            seen.add(key)
        if len(unique) >= max_entries:
            break
    try:
        with open(SEARCH_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"searches": unique, "last_updated": datetime.now().isoformat()}, f, indent=2)
    except Exception as e:
        logging.warning(f"Failed to save search history: {e}")


def add_to_search_history(search_term: str):
    """Prepend a successful search term and persist."""
    searches = load_search_history()
    searches.insert(0, search_term)
    save_search_history(searches)
    logging.info(f"Added to search history: {search_term}")

# ---------------------------------------------------------------------------
# Timed input (used by CLI scripts)
# ---------------------------------------------------------------------------

def input_with_countdown(prompt: str, timeout: int = 15, default: str = "") -> str:
    """
    Read user input with a visible countdown.  Returns *default* on timeout.
    Works on Windows (msvcrt) and Unix (select).
    """
    if platform.system() == "Windows":
        print(f"{prompt}", end="", flush=True)
        input_chars: List[str] = []
        start_time = time.time()
        last_update = start_time

        while True:
            elapsed = time.time() - start_time
            remaining = timeout - int(elapsed)

            if remaining <= 0:
                print(f"\r{prompt} [{remaining}s] (auto-selecting: {default})")
                return default

            if time.time() - last_update >= 1.0:
                print(f"\r{prompt} [{remaining}s] {''.join(input_chars)} ", end="", flush=True)
                last_update = time.time()

            if msvcrt.kbhit():
                char = msvcrt.getwche()
                if char == "\r":
                    print()
                    return "".join(input_chars)
                elif char == "\b":
                    if input_chars:
                        input_chars.pop()
                        print(f"\r{prompt} [{remaining}s] {''.join(input_chars)} ", end="", flush=True)
                elif char in ("\x00", "\xe0"):
                    msvcrt.getwche()  # consume second byte of special key
                else:
                    input_chars.append(char)

            time.sleep(0.1)

    else:
        deadline = time.time() + timeout
        while True:
            remaining = max(0, int(deadline - time.time()))
            print(f"\r{prompt} [{remaining}s] ", end="", flush=True)
            if remaining == 0:
                print(f"(auto-selecting: {default})")
                return default
            r, _, _ = select.select([sys.stdin], [], [], min(1.0, deadline - time.time()))
            if r:
                print()
                return sys.stdin.readline().strip()

# ---------------------------------------------------------------------------
# Networking helpers
# ---------------------------------------------------------------------------

def get_public_ip() -> Optional[str]:
    """Return current public IP address, or None on failure."""
    for url in ("https://api.ipify.org?format=text", "https://icanhazip.com"):
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            return r.text.strip()
        except requests.RequestException:
            continue
    return None


def check_vpn_status(expected_vpn_ip: Optional[str] = None) -> bool:
    """
    Show current public IP and ask user to confirm VPN status.
    Auto-confirms when IP matches *expected_vpn_ip*.
    Returns True to continue, False to exit.
    """
    print("\n" + "=" * 80)
    print("VPN CHECK")
    print("=" * 80)

    ip_address = get_public_ip()
    if ip_address:
        print(f"Your current public IP: {ip_address}")
        if expected_vpn_ip and ip_address == expected_vpn_ip:
            print(f"✓ VPN Confirmed (matches {expected_vpn_ip})")
            print("Auto-continuing...")
            return True
        elif expected_vpn_ip:
            print(f"⚠ Warning: IP does not match expected VPN IP ({expected_vpn_ip})")
    else:
        print("Warning: Unable to fetch your public IP address.")

    print("\nIs your VPN connected?")
    print("  y: Continue (VPN is on)")
    print("  n: Exit (VPN is off)")
    while True:
        choice = input("\nYour choice (y/n): ").strip().lower()
        if choice == "y":
            return True
        if choice == "n":
            print("\nExiting. Please connect to your VPN and try again.")
            return False
        print("Invalid choice. Please enter 'y' or 'n'.")

# ---------------------------------------------------------------------------
# M3U loading
# ---------------------------------------------------------------------------

def _parse_m3u_lines(lines: List[str]) -> List[Channel]:
    """Parse raw M3U text lines into a list of channel dicts."""
    channels: List[Channel] = []
    current: Optional[Dict[str, str]] = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            current = {}
            m = re.search(r'tvg-id="([^"]*)"', line)
            if m:
                current["tvg-id"] = m.group(1)
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                current["group-title"] = m.group(1)
            name_part = line.split(",")[-1]
            if name_part:
                current["name"] = name_part.strip()
        elif line and not line.startswith("#") and current is not None:
            if "name" in current:
                current["url"] = line
                channels.append(current)
            current = None
    # F4: collapse duplicate URLs — merge group-title labels
    seen: Dict[str, Channel] = {}
    order: List[str] = []
    for ch in channels:
        url = ch.get("url", "")
        if url not in seen:
            seen[url] = dict(ch)
            order.append(url)
        else:
            g_existing = seen[url].get("group-title", "")
            g_new = ch.get("group-title", "")
            if g_new and g_new not in g_existing:
                seen[url]["group-title"] = f"{g_existing}, {g_new}" if g_existing else g_new
    return [seen[url] for url in order]


def load_m3u(url: str) -> List[Channel]:
    """Download and parse an M3U playlist (no caching)."""
    print(f"Downloading M3U from {url}...")
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Error: Failed to download M3U playlist. {e}", file=sys.stderr)
        return []
    return _parse_m3u_lines(r.text.splitlines())


def load_m3u_cached(url: str) -> List[Channel]:
    """Download and parse an M3U playlist with ETag/Last-Modified caching."""
    M3U_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file    = M3U_CACHE_DIR / f"{url_hash}.m3u"
    metadata_file = M3U_CACHE_DIR / f"{url_hash}.meta"

    headers: Dict[str, str] = {}
    if metadata_file.exists():
        try:
            with open(metadata_file) as f:
                meta = json.load(f)
            if "etag" in meta:
                headers["If-None-Match"] = meta["etag"]
            if "last-modified" in meta:
                headers["If-Modified-Since"] = meta["last-modified"]
        except Exception as e:
            logging.warning(f"Failed to load M3U cache metadata: {e}")

    print(f"Checking M3U updates from {url}...")
    content: Optional[str] = None
    try:
        r = requests.get(url, timeout=15, headers=headers)
        if r.status_code == 304:
            print("✓ M3U is up to date (using cached version)")
            if cache_file.exists():
                content = cache_file.read_text(encoding="utf-8")
            else:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                content = r.text
        elif r.status_code == 200:
            content = r.text
            print("✓ M3U downloaded successfully (updated)")
            cache_file.write_text(content, encoding="utf-8")
            meta: Dict[str, str] = {}
            if "etag" in r.headers:
                meta["etag"] = r.headers["etag"]
            if "last-modified" in r.headers:
                meta["last-modified"] = r.headers["last-modified"]
            if meta:
                with open(metadata_file, "w") as f:
                    json.dump(meta, f)
        else:
            r.raise_for_status()
    except requests.RequestException as e:
        if cache_file.exists():
            print(f"Warning: Network error, using cached M3U. {e}", file=sys.stderr)
            try:
                content = cache_file.read_text(encoding="utf-8")
            except Exception as ce:
                print(f"Error: Failed to load cached M3U. {ce}", file=sys.stderr)
                return []
        else:
            print(f"Warning: Failed to download M3U, no cache available. {e}", file=sys.stderr)
            return []

    if content is None:
        return []

    channels = _parse_m3u_lines(content.splitlines())
    logging.info(f"Parsed {len(channels)} channels from M3U")
    return channels

# ---------------------------------------------------------------------------
# EPG loading  (robust: iterparse + zlib fallback + tuple timeout)
# ---------------------------------------------------------------------------

def load_epg(url: str, lookback_hours: int = 0) -> EpgData:
    """
    Download and parse an XMLTV EPG with caching.
    Uses iterparse to recover data from truncated XML and zlib for lenient
    gzip decompression (tolerates missing gzip footer from misconfigured servers).

    lookback_hours: keep programmes that ended up to this many hours ago (0 = discard all past).
    """
    EPG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file    = EPG_CACHE_DIR / f"{url_hash}.xml.gz"
    metadata_file = EPG_CACHE_DIR / f"{url_hash}.meta"

    headers: Dict[str, str] = {}
    if metadata_file.exists():
        try:
            with open(metadata_file) as f:
                meta = json.load(f)
            if "etag" in meta:
                headers["If-None-Match"] = meta["etag"]
            if "last-modified" in meta:
                headers["If-Modified-Since"] = meta["last-modified"]
        except Exception:
            pass

    print(f"Checking EPG updates from {url}...")
    content: Optional[bytes] = None
    try:
        r = requests.get(url, timeout=(10, 120), headers=headers)

        if r.status_code == 304:
            print("✓ EPG is up to date (using cached version)")
            if cache_file.exists():
                content = cache_file.read_bytes()
            else:
                print("Cache file missing, downloading fresh copy...")
                r = requests.get(url, timeout=(10, 120))
                r.raise_for_status()
                content = r.content
        elif r.status_code == 200:
            content = r.content
            # Verify completeness via Content-Length
            expected = r.headers.get("content-length")
            if expected and len(content) < int(expected):
                raise IOError(f"Incomplete download: {len(content)}/{expected} bytes")
            print("✓ EPG downloaded successfully (updated)")
            # Store as gzip regardless of server encoding
            if not content.startswith(b"\x1f\x8b"):
                import io
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                    gz.write(content)
                content = buf.getvalue()
            cache_file.write_bytes(content)
            meta_out: Dict[str, str] = {}
            if "etag" in r.headers:
                meta_out["etag"] = r.headers["etag"]
            if "last-modified" in r.headers:
                meta_out["last-modified"] = r.headers["last-modified"]
            if meta_out:
                with open(metadata_file, "w") as f:
                    json.dump(meta_out, f)
        else:
            r.raise_for_status()

    except requests.RequestException as e:
        if cache_file.exists():
            print(f"Warning: Network error, using cached EPG. {e}", file=sys.stderr)
            try:
                content = cache_file.read_bytes()
            except Exception as ce:
                print(f"Error: Failed to load cached EPG. {ce}", file=sys.stderr)
                return {}
        else:
            print(f"Warning: Failed to download EPG, no cache available. {e}", file=sys.stderr)
            return {}

    if content is None:
        return {}

    # Decompress
    if content.startswith(b"\x1f\x8b"):
        try:
            xml_data = gzip.decompress(content)
        except (EOFError, gzip.BadGzipFile):
            d = zlib.decompressobj(wbits=31)
            xml_data = d.decompress(content)
    else:
        xml_data = content

    # Parse with iterparse so truncated XML yields whatever completed elements exist
    root = ET.Element("tv")
    recovered = 0
    try:
        for _event, elem in ET.iterparse(BytesIO(xml_data), events=("end",)):
            if elem.tag in ("programme", "channel"):
                root.append(elem)
                recovered += 1
    except ET.ParseError:
        logging.warning(f"EPG XML truncated; recovered {recovered} elements")

    if recovered == 0:
        # Delete corrupted cache and bail
        for f in (cache_file, metadata_file):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        print("Warning: EPG contained no parseable data. Cache cleared.", file=sys.stderr)
        return {}

    epg: EpgData = {}
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=lookback_hours)
    total = filtered = 0

    for prog in root.findall("programme"):
        total += 1
        channel_id = prog.get("channel")
        if not channel_id:
            continue

        def _text(tag: str) -> str:
            el = prog.find(tag)
            return (el.text or "") if el is not None else ""

        start_str = prog.get("start", "")
        stop_str  = prog.get("stop", "")
        start_time = parse_epg_time(start_str)
        stop_time  = parse_epg_time(stop_str)

        if stop_time and stop_time < cutoff:
            filtered += 1
            continue

        epg.setdefault(channel_id, []).append({
            "start":       start_str,
            "stop":        stop_str,
            "start_time":  start_time,
            "stop_time":   stop_time,
            "title":       _text("title") or "Untitled",
            "subtitle":    _text("sub-title"),
            "episode_num": _text("episode-num"),
            "description": _text("desc"),
            "air_date":    _text("date"),
        })

    for channel_id in epg:
        epg[channel_id].sort(
            key=lambda x: x["start_time"] if x["start_time"] else datetime.max.astimezone()
        )

    if total > 0:
        print(f"Processed {total} programs ({filtered} past, {total - filtered} current/upcoming)")

    return epg

# ---------------------------------------------------------------------------
# EPG time parsing
# ---------------------------------------------------------------------------

def parse_epg_time(time_str: str) -> Optional[datetime]:
    """Parse XMLTV time string (YYYYMMDDHHmmss ±HHmm) to timezone-aware local datetime."""
    if not time_str:
        return None
    try:
        parts = time_str.split()
        if len(parts) != 2:
            return None
        dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
        tz_str = parts[1]
        sign = 1 if tz_str[0] == "+" else -1
        offset = sign * (int(tz_str[1:3]) * 60 + int(tz_str[3:5]))
        return dt.replace(tzinfo=timezone(timedelta(minutes=offset))).astimezone()
    except (ValueError, IndexError):
        return None


def is_new_episode(air_date: str, days_threshold: int = 7) -> bool:
    """Return True if *air_date* (YYYYMMDD) is within *days_threshold* days of today."""
    if not air_date or len(air_date) < 8:
        return False
    try:
        air_dt = datetime.strptime(air_date[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        days_diff = (datetime.now(timezone.utc) - air_dt).days
        return 0 <= days_diff <= days_threshold
    except ValueError:
        return False

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_channels(channels: List[Channel], query: str) -> List[Channel]:
    """Filter channels by name (case-insensitive)."""
    q = query.lower()
    return [c for c in channels if q in c.get("name", "").lower()]


_tvg_map_cached_key: Optional[List[Channel]] = None
_tvg_map_cached_result: Dict[str, List[Channel]] = {}


def _build_tvg_map(channels: List[Channel]) -> Dict[str, List[Channel]]:
    global _tvg_map_cached_key, _tvg_map_cached_result
    if channels is not _tvg_map_cached_key:
        tvg_map: Dict[str, List[Channel]] = defaultdict(list)
        for ch in channels:
            tvg_id = ch.get("tvg-id")
            if tvg_id:
                tvg_map[tvg_id].append(ch)
        _tvg_map_cached_key = channels
        _tvg_map_cached_result = tvg_map
    return _tvg_map_cached_result


def _fmt_time_status(is_playing_now: bool, minutes_until: int) -> str:
    if is_playing_now:
        return "NOW PLAYING"
    if minutes_until == 0:
        return "Starting now"
    if minutes_until < 60:
        return f"In {minutes_until} min"
    return f"In {minutes_until // 60}h {minutes_until % 60}m"


def search_shows_in_timeframe(
    channels: List[Channel],
    epg: EpgData,
    query: str,
    hours_ahead: int = 3,
    groups: Optional[set] = None,
    max_results: int = 100,
) -> List[ShowResult]:
    """Search for shows matching *query* that are on now or start within *hours_ahead* hours.

    groups: optional set of group-title strings to restrict results to.
    """
    q = query.lower()
    now = datetime.now().astimezone()
    cutoff = now + timedelta(hours=hours_ahead)
    tvg_map = _build_tvg_map(channels)
    results: List[ShowResult] = []

    for channel_id, programs in epg.items():
        if channel_id not in tvg_map:
            continue
        for prog in programs:
            title = prog.get("title", "")
            if q not in title.lower():
                continue
            start_time = prog.get("start_time")
            stop_time  = prog.get("stop_time")
            if not start_time:
                continue
            is_playing_now = bool(stop_time and start_time <= now < stop_time)
            starts_soon    = now <= start_time <= cutoff
            if not (is_playing_now or starts_soon):
                continue
            minutes_until = int((start_time - now).total_seconds() / 60)
            air_date = prog.get("air_date", "")
            for ch in tvg_map[channel_id]:
                if groups and ch.get("group-title") not in groups:
                    continue
                results.append({
                    "channel":      ch,
                    "title":        title,
                    "subtitle":     prog.get("subtitle", ""),
                    "episode_num":  prog.get("episode_num", ""),
                    "description":  prog.get("description", ""),
                    "air_date":     air_date,
                    "start_time":   start_time,
                    "stop_time":    stop_time,
                    "start_str":    prog.get("start", ""),
                    "stop_str":     prog.get("stop", ""),
                    "time_status":  _fmt_time_status(is_playing_now, minutes_until),
                    "minutes_until": minutes_until,
                    "is_playing_now": is_playing_now,
                    "is_new":       is_new_episode(air_date),
                })

    results.sort(key=lambda x: (not x["is_playing_now"], x["start_time"]))
    if max_results and len(results) > max_results:
        logging.info(f"Search capped at {max_results} results (found {len(results)} total)")
        results = results[:max_results]
    return results


def find_alternative_streams(
    channels: List[Channel],
    epg: EpgData,
    show_title: str,
    episode_num: str,
    original_start_time: datetime,
    tolerance_minutes: int = 5,
) -> List[ShowResult]:
    """Find the same episode airing on alternative channels at approximately the same time."""
    logging.info(f"Searching for alternative streams: {show_title} {episode_num}")
    now = datetime.now().astimezone()
    tvg_map = _build_tvg_map(channels)
    alternatives: List[ShowResult] = []

    for channel_id, programs in epg.items():
        if channel_id not in tvg_map:
            continue
        for prog in programs:
            title          = prog.get("title", "")
            prog_ep        = prog.get("episode_num", "")
            start_time     = prog.get("start_time")
            stop_time      = prog.get("stop_time")
            if not start_time:
                continue
            title_match = show_title.lower() in title.lower() or title.lower() in show_title.lower()
            if not title_match:
                continue
            if episode_num and prog_ep and episode_num != prog_ep:
                continue
            if abs((start_time - original_start_time).total_seconds() / 60) > tolerance_minutes:
                continue
            is_playing_now = bool(stop_time and start_time <= now < stop_time)
            if not (is_playing_now or now <= start_time):
                continue
            minutes_until = int((start_time - now).total_seconds() / 60)
            for ch in tvg_map[channel_id]:
                alternatives.append({
                    "channel":      ch,
                    "title":        title,
                    "subtitle":     prog.get("subtitle", ""),
                    "episode_num":  prog_ep,
                    "description":  prog.get("description", ""),
                    "air_date":     prog.get("air_date", ""),
                    "start_time":   start_time,
                    "stop_time":    stop_time,
                    "start_str":    prog.get("start", ""),
                    "stop_str":     prog.get("stop", ""),
                    "time_status":  _fmt_time_status(is_playing_now, minutes_until),
                    "minutes_until": minutes_until,
                    "is_playing_now": is_playing_now,
                    "is_new":       False,
                })

    logging.info(f"Found {len(alternatives)} alternative streams")
    return alternatives


def find_future_reruns(
    channels: List[Channel],
    epg: EpgData,
    show_title: str,
    episode_num: str,
    hours_ahead: int = 24,
) -> List[ShowResult]:
    """Find future airings of the same episode for rescheduling."""
    logging.info(f"Searching for future reruns: {show_title} {episode_num} (next {hours_ahead}h)")
    now = datetime.now().astimezone()
    cutoff = now + timedelta(hours=hours_ahead)
    tvg_map = _build_tvg_map(channels)
    reruns: List[ShowResult] = []

    for channel_id, programs in epg.items():
        if channel_id not in tvg_map:
            continue
        for prog in programs:
            title      = prog.get("title", "")
            prog_ep    = prog.get("episode_num", "")
            start_time = prog.get("start_time")
            stop_time  = prog.get("stop_time")
            if not start_time:
                continue
            title_match = show_title.lower() in title.lower() or title.lower() in show_title.lower()
            if not title_match:
                continue
            if episode_num and prog_ep and episode_num != prog_ep:
                continue
            if not (now < start_time <= cutoff):
                continue
            minutes_until = int((start_time - now).total_seconds() / 60)
            for ch in tvg_map[channel_id]:
                reruns.append({
                    "channel":      ch,
                    "title":        title,
                    "subtitle":     prog.get("subtitle", ""),
                    "episode_num":  prog_ep,
                    "description":  prog.get("description", ""),
                    "air_date":     prog.get("air_date", ""),
                    "start_time":   start_time,
                    "stop_time":    stop_time,
                    "start_str":    prog.get("start", ""),
                    "stop_str":     prog.get("stop", ""),
                    "time_status":  _fmt_time_status(False, minutes_until),
                    "minutes_until": minutes_until,
                    "is_playing_now": False,
                    "is_new":       False,
                })

    reruns.sort(key=lambda x: x["start_time"])
    logging.info(f"Found {len(reruns)} future reruns")
    return reruns

# ---------------------------------------------------------------------------
# Watch history
# ---------------------------------------------------------------------------

def load_watch_history() -> List[Dict[str, Any]]:
    """Load watch history from file, migrating old formats on the fly."""
    if not WATCH_HISTORY_FILE.exists():
        return []
    try:
        with open(WATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
        migrated = False
        for entry in history:
            if "timestamp" in entry and "watch_count" not in entry:
                entry["watch_count"] = 1
                entry["last_watched"] = entry.pop("timestamp")
                migrated = True
            if "total_duration_seconds" not in entry:
                entry["total_duration_seconds"] = entry.get("watch_count", 0) * 600
                migrated = True
        if migrated:
            try:
                with open(WATCH_HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2)
            except Exception:
                pass
        return history
    except Exception:
        return []


def log_channel_watch(channel: Channel, duration_seconds: int):
    """Record a channel watch session (ignored if < 2 minutes)."""
    if duration_seconds < 120:
        return
    history = load_watch_history()
    tvg_id = channel.get("tvg-id", "")
    existing = next((h for h in history if h.get("tvg-id") == tvg_id), None)
    if existing:
        existing["total_duration_seconds"] = existing.get("total_duration_seconds", 0) + duration_seconds
        existing["watch_count"] = existing.get("watch_count", 0) + 1
        existing["last_watched"] = datetime.now().isoformat()
    else:
        history.append({
            "name":                   channel.get("name", "Unknown"),
            "tvg-id":                 tvg_id,
            "url":                    channel.get("url", ""),
            "total_duration_seconds": duration_seconds,
            "watch_count":            1,
            "last_watched":           datetime.now().isoformat(),
        })
    try:
        with open(WATCH_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save watch history. {e}", file=sys.stderr)


def get_frequent_channels(channels: List[Channel], epg: EpgData) -> List[Dict[str, Any]]:
    """Return top-3 channels by total watch time, enriched with current EPG info."""
    def _sort_key(x: Dict[str, Any]):
        ts = x.get("last_watched", "")
        try:
            dt = datetime.fromisoformat(ts) if ts else datetime.min
        except ValueError:
            dt = datetime.min
        return (x.get("total_duration_seconds", 0), dt)

    history = sorted(load_watch_history(), key=_sort_key, reverse=True)
    frequent: List[Dict[str, Any]] = []
    for entry in history[:3]:
        url    = entry.get("url")
        tvg_id = entry.get("tvg-id")
        ch = (
            next((c for c in channels if c.get("url") == url), None)
            if url else None
        ) or (
            next((c for c in channels if c.get("tvg-id") == tvg_id), None)
            if tvg_id else None
        )
        if not ch:
            continue
        current_show = None
        if tvg_id in epg:
            now = datetime.now().astimezone()
            for prog in epg[tvg_id]:
                st = prog.get("start_time")
                et = prog.get("stop_time")
                if st and et and st <= now < et:
                    current_show = prog
                    break
        frequent.append({
            "channel":                ch,
            "current_show":           current_show,
            "total_duration_seconds": entry.get("total_duration_seconds", 0),
            "watch_count":            entry.get("watch_count", 0),
            "last_watched":           entry.get("last_watched", ""),
        })
    return frequent


def display_frequent_channels(frequent: List[Dict[str, Any]]):
    """Print the frequently-watched panel to stdout."""
    if not frequent:
        return
    print("\n" + "=" * 80)
    print("FREQUENTLY WATCHED:")
    print("=" * 80)
    for i, item in enumerate(frequent, 1):
        ch    = item["channel"]
        show  = item["current_show"]
        secs  = item.get("total_duration_seconds", 0)
        hrs, rem = divmod(secs, 3600)
        mins = rem // 60
        time_str = f"{hrs}h {mins}m" if hrs else (f"{mins}m" if mins else "0m")
        print(f"{i}. {ch.get('name', 'Unknown')} [{ch.get('group-title', 'Unknown')}] ({time_str} watched)")
        if show:
            s = show.get("title", "Unknown")
            if show.get("episode_num"):
                s += f" ({show['episode_num']})"
            if show.get("subtitle"):
                s += f' - "{show["subtitle"]}"'
            if is_new_episode(show.get("air_date", "")):
                s += " +++"
            print(f"   NOW PLAYING: {s}")
        else:
            print("   (No EPG data available)")
        print()


def get_search_history_now_playing(channels: List[Channel], epg: EpgData) -> List[Dict[str, Any]]:
    """Check last 5 search terms and return any currently-airing matches (one per term)."""
    searches = load_search_history()
    if not searches or not epg:
        return []
    now_playing: List[Dict[str, Any]] = []
    seen_titles: set = set()
    for query in searches[:5]:
        for result in search_shows_in_timeframe(channels, epg, query, hours_ahead=0):
            if not result.get("is_playing_now"):
                continue
            key = result["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            now_playing.append({"query": query, "result": result})
            break
    return now_playing


def display_search_history_now_playing(now_playing: List[Dict[str, Any]], start_index: int = 4):
    """Print the 'now playing from your searches' panel."""
    if not now_playing:
        return
    print("\n" + "=" * 80)
    print("NOW PLAYING FROM YOUR SEARCHES:")
    print("=" * 80)
    for i, item in enumerate(now_playing, start_index):
        result = item["result"]
        ch = result["channel"]
        s = result["title"]
        if result.get("episode_num"):
            s += f" ({result['episode_num']})"
        if result.get("subtitle"):
            s += f' - "{result["subtitle"]}"'
        if result.get("is_new"):
            s += " +++"
        print(f"{i}. {s}")
        print(f"   {ch.get('name', 'Unknown')} [{ch.get('group-title', 'Unknown')}]  (from search: '{item['query']}')")
        print()

# ---------------------------------------------------------------------------
# Favorites (F1)
# ---------------------------------------------------------------------------

def load_favorites() -> List[Dict[str, str]]:
    """Return the saved favorites list."""
    if not FAVORITES_FILE.exists():
        return []
    try:
        with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load favorites: {e}")
        return []


def save_favorites(favs: List[Dict[str, str]]):
    """Persist the favorites list."""
    try:
        with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
            json.dump(favs, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save favorites. {e}", file=sys.stderr)


def toggle_favorite(channel: Channel) -> bool:
    """Add channel to favorites if absent; remove if present. Returns True if added."""
    favs = load_favorites()
    tvg_id = channel.get("tvg-id", "")
    url = channel.get("url", "")
    key = tvg_id or url
    existing = next(
        (f for f in favs if (f.get("tvg-id") or f.get("url")) == key), None
    )
    if existing:
        favs = [f for f in favs if (f.get("tvg-id") or f.get("url")) != key]
        save_favorites(favs)
        return False
    favs.append({
        "name":        channel.get("name", "Unknown"),
        "tvg-id":      tvg_id,
        "url":         url,
        "group-title": channel.get("group-title", ""),
    })
    save_favorites(favs)
    return True


def get_favorite_channels(channels: List[Channel], epg: EpgData) -> List[Dict[str, Any]]:
    """Return saved favorites matched against the live channel list, enriched with EPG."""
    favs = load_favorites()
    result: List[Dict[str, Any]] = []
    now = datetime.now().astimezone()
    for fav in favs:
        tvg_id = fav.get("tvg-id", "")
        url    = fav.get("url", "")
        ch = (
            next((c for c in channels if c.get("tvg-id") == tvg_id), None)
            if tvg_id else None
        ) or (
            next((c for c in channels if c.get("url") == url), None)
            if url else None
        )
        if not ch:
            continue
        current_show = None
        if tvg_id and tvg_id in epg:
            for prog in epg[tvg_id]:
                st = prog.get("start_time")
                et = prog.get("stop_time")
                if st and et and st <= now < et:
                    current_show = prog
                    break
        result.append({"channel": ch, "current_show": current_show})
    return result


def display_favorites(favorites: List[Dict[str, Any]], start_index: int = 1):
    """Print the favorites panel to stdout."""
    if not favorites:
        return
    print("\n" + "=" * 80)
    print("FAVORITES:")
    print("=" * 80)
    for i, item in enumerate(favorites, start_index):
        ch   = item["channel"]
        show = item["current_show"]
        print(f"{i}. {ch.get('name', 'Unknown')} [{ch.get('group-title', 'Unknown')}]")
        if show:
            s = show.get("title", "Unknown")
            if show.get("episode_num"):
                s += f" ({show['episode_num']})"
            if show.get("subtitle"):
                s += f' - "{show["subtitle"]}"'
            if is_new_episode(show.get("air_date", "")):
                s += " +++"
            print(f"   NOW PLAYING: {s}")
        else:
            print("   (No EPG data)")
        print()


# ---------------------------------------------------------------------------
# Channel groups (F5)
# ---------------------------------------------------------------------------

def get_channel_groups(channels: List[Channel]) -> List[tuple]:
    """Return a sorted list of (group_name, channel_count) tuples."""
    counts: Dict[str, int] = defaultdict(int)
    for ch in channels:
        group = ch.get("group-title", "")
        if group:
            counts[group] += 1
    return sorted(counts.items(), key=lambda x: x[0].lower())


# ---------------------------------------------------------------------------
# Channel schedule (F6)
# ---------------------------------------------------------------------------

def get_channel_schedule(epg: EpgData, tvg_id: str, upcoming: int = 3) -> List[Dict[str, Any]]:
    """Return the current programme and the next *upcoming* programmes for a tvg-id."""
    if not tvg_id or tvg_id not in epg:
        return []
    now = datetime.now().astimezone()
    cutoff = now + timedelta(hours=12)
    items: List[Dict[str, Any]] = []
    for prog in epg[tvg_id]:
        st = prog.get("start_time")
        et = prog.get("stop_time")
        if not st:
            continue
        if et and et < now:
            continue
        if st > cutoff:
            break
        items.append(prog)
        if len(items) >= upcoming + 1:
            break
    return items


# ---------------------------------------------------------------------------
# Desktop notifications (F2) — optional plyer dependency
# ---------------------------------------------------------------------------

def send_desktop_notification(title: str, message: str):
    """Fire a desktop notification via plyer if available; silently skip if not."""
    try:
        from plyer import notification  # type: ignore
        notification.notify(title=title, message=message, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Recording utilities
# ---------------------------------------------------------------------------

def ensure_recordings_dir():
    """Create recordings directory if it does not exist."""
    if not RECORDINGS_DIR.exists():
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Created recordings directory: {RECORDINGS_DIR}")


def get_safe_filename(channel_name: str, show_title: str = "") -> str:
    """Build a timestamped, filesystem-safe .mkv filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _clean(s: str) -> str:
        return re.sub(r"[-\s]+", "_", re.sub(r"[^\w\s-]", "", s).strip())

    base = _clean(channel_name)
    if show_title:
        return f"{base}_{_clean(show_title)}_{timestamp}.mkv"
    return f"{base}_{timestamp}.mkv"


def extract_subtitles_from_recording(mkv_path: Path) -> List[Path]:
    """Extract all subtitle tracks from an MKV file to individual SRT files via ffmpeg."""
    if not mkv_path.exists():
        print(f"Error: Recording file not found: {mkv_path}", file=sys.stderr)
        return []
    print(f"\n[SUBTITLE EXTRACTION] Checking for subtitles in {mkv_path.name}...")
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index:stream_tags=language",
             "-of", "csv=p=0", str(mkv_path)],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            print("[SUBTITLE EXTRACTION] No subtitle streams found or ffprobe error")
            return []
        streams = [
            (parts[0], parts[1] if len(parts) > 1 else "unknown")
            for line in probe.stdout.strip().splitlines()
            if line
            for parts in [line.split(",")]
        ]
        if not streams:
            print("[SUBTITLE EXTRACTION] No subtitle tracks found in recording")
            return []
        print(f"[SUBTITLE EXTRACTION] Found {len(streams)} subtitle track(s)")
        extracted: List[Path] = []
        for idx, (stream_index, language) in enumerate(streams):
            fname = (
                f"{mkv_path.stem}_{language}.srt"
                if language and language != "unknown"
                else f"{mkv_path.stem}_sub{idx}.srt"
            )
            srt_path = mkv_path.parent / fname
            print(f"[SUBTITLE EXTRACTION] Extracting stream {stream_index} ({language}) -> {fname}")
            result = subprocess.run(
                ["ffmpeg", "-i", str(mkv_path), "-map", f"0:{stream_index}", "-c:s", "srt", str(srt_path)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                print(f"[SUBTITLE EXTRACTION] ✓ Saved: {srt_path.name}")
                extracted.append(srt_path)
            else:
                print(f"[SUBTITLE EXTRACTION] ✗ Failed stream {stream_index}", file=sys.stderr)
        print(f"\n{'✓' if extracted else '⚠'} {len(extracted)} subtitle file(s) extracted")
        return extracted
    except FileNotFoundError:
        print("\nError: 'ffprobe'/'ffmpeg' not found. Install ffmpeg to extract subtitles.", file=sys.stderr)
        return []
    except Exception as e:
        print(f"\n[SUBTITLE EXTRACTION ERROR] {e}", file=sys.stderr)
        return []
