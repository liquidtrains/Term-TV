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
import lzma
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
from typing import List, Dict, Any, Optional, Callable
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
CHANNEL_NOTES_FILE  = Path(".channel_notes.json")
SCHEDULED_TASKS_FILE = Path(".scheduled_tasks.json")
RECORDINGS_DIR      = Path.home() / "Videos" / "Recordings"
EPG_CACHE_DIR       = Path(".epg_cache")
M3U_CACHE_DIR       = Path(".m3u_cache")

# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_playlists(playlists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop playlist entries missing required keys, warning for each one dropped.

    A playlist dict needs at least "name" and "m3u_url" — everything downstream
    indexes these directly, so a malformed entry would otherwise crash with a
    raw KeyError instead of the friendly config errors used elsewhere.
    """
    valid = []
    for i, pl in enumerate(playlists, 1):
        missing = [k for k in ("name", "m3u_url") if not pl.get(k)]
        if missing:
            msg = f"Skipping playlist #{i} in config.json — missing required key(s): {', '.join(missing)}"
            logging.warning(msg)
            print(f"Warning: {msg}", file=sys.stderr)
            continue
        valid.append(pl)
    return valid


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
                    # Delete the data file and its .meta sidecar together.
                    # Sidecars are named {hash}.meta, so strip ALL suffixes
                    # (with_suffix only replaces the last one and would miss
                    # the .meta of a {hash}.xml.gz cache file).
                    meta_file = cache_dir / (file_path.name.split(".", 1)[0] + ".meta")
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
            m = re.search(r'tvg-logo="([^"]*)"', line)
            if m:
                current["tvg-logo"] = m.group(1)
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                current["group-title"] = m.group(1)
            # Channel name is everything after the comma that ends the
            # attribute section.  Splitting on the LAST comma would truncate
            # names like "NCIS, Los Angeles", so find the first comma after
            # the final quoted attribute instead.
            last_quote = line.rfind('"')
            comma_idx = line.find(",", last_quote + 1) if last_quote != -1 else line.find(",")
            name_part = line[comma_idx + 1:] if comma_idx != -1 else ""
            if name_part.strip():
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

    except (requests.RequestException, IOError, ValueError) as e:
        # requests.RequestException subclasses IOError/OSError, not the other way
        # around — the manual `raise IOError(...)` above (incomplete download) and
        # a malformed Content-Length (`int(expected)` -> ValueError) need their own
        # types listed here or they'd propagate uncaught past this handler.
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
    """Parse XMLTV time string (YYYYMMDDHHmmss [±HHmm]) to timezone-aware local datetime.

    The timezone offset is optional — some feeds omit it.  When absent the
    timestamp is treated as local time so programmes still display correctly.
    """
    if not time_str:
        return None
    try:
        parts = time_str.split()
        dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
        if len(parts) == 1:
            return dt.astimezone()
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


def ch_in_group(ch: Channel, groups: set) -> bool:
    """True if the channel belongs to any of *groups*, handling merged group labels.

    The M3U dedup step can produce group-title like "US HD, US SD" — split on
    comma so filtering by either component still matches.
    """
    raw = ch.get("group-title", "")
    if raw in groups:
        return True
    return any(g.strip() in groups for g in raw.split(","))


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
                if groups and not ch_in_group(ch, groups):
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
# Shared mpv resilience flags
# ---------------------------------------------------------------------------

# Reconnect on stream drop, tolerate a slow initial connect, and don't stall
# on a filling cache. Term-TV-Web.py's launch_mpv()/launch_recording() have
# always used these; the CLIs' scheduled tasks only had the timeout half and
# interactive playback had none of it — now applied consistently everywhere.
MPV_RESILIENCE_ARGS = [
    "--stream-lavf-o=reconnect=1,reconnect_delay_max=5,timeout=10000000",
    "--cache=yes",
    "--cache-pause=no",
]


# ---------------------------------------------------------------------------
# Log archiving
# ---------------------------------------------------------------------------

def archive_log_file(log_file: Path, archive_dir: Path, prefix: str,
                      chunk_size: int = 5 * 1024 * 1024, max_age_days: int = 365):
    """Archive *log_file* with LZMA compression (splitting into chunk_size
    pieces if needed), clear it, and purge archives older than max_age_days.

    Shared implementation behind archive_mpv_log() (mpv-output.log) and
    recordings.log archiving in Term-TV.py/Term-TV-VPN.py — previously only
    mpv-output.log was ever rotated; recordings.log grew unbounded.
    """
    if not log_file.exists() or log_file.stat().st_size == 0:
        return

    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        data = log_file.read_bytes()
    except Exception as e:
        logging.warning(f"archive_log_file({log_file.name}): could not read log: {e}")
        return

    try:
        if len(data) <= chunk_size:
            archive_path = archive_dir / f"{prefix}-{timestamp}.log.xz"
            with lzma.open(archive_path, "wb", preset=9) as xz_f:
                xz_f.write(data)
            orig_kb = len(data) // 1024
            comp_kb = archive_path.stat().st_size // 1024
            print(f"{log_file.name} archived: {archive_path.name}  ({orig_kb} KB → {comp_kb} KB compressed)")
            logging.info(f"{log_file.name} archived to {archive_path.name} ({orig_kb} KB raw, {comp_kb} KB compressed)")
        else:
            total = (len(data) + chunk_size - 1) // chunk_size
            for idx in range(total):
                chunk = data[idx * chunk_size:(idx + 1) * chunk_size]
                archive_path = archive_dir / f"{prefix}-{timestamp}-part{idx + 1:03d}of{total:03d}.log.xz"
                with lzma.open(archive_path, "wb", preset=9) as xz_f:
                    xz_f.write(chunk)
            orig_mb = len(data) / (1024 * 1024)
            print(f"{log_file.name} split into {total} chunk(s) and archived  ({orig_mb:.1f} MB total)")
            logging.info(f"{log_file.name} archived in {total} chunks ({orig_mb:.1f} MB)")
        log_file.write_bytes(b"")
    except Exception as e:
        logging.warning(f"archive_log_file({log_file.name}): compression failed: {e}")
        return

    cutoff = datetime.now() - timedelta(days=max_age_days)
    deleted = 0
    try:
        for archive in archive_dir.iterdir():
            if not archive.is_file() or not archive.name.startswith(f"{prefix}-"):
                continue
            try:
                if datetime.fromtimestamp(archive.stat().st_mtime) < cutoff:
                    archive.unlink()
                    deleted += 1
            except Exception:
                pass
    except Exception as e:
        logging.warning(f"archive_log_file({log_file.name}): purge scan failed: {e}")
    if deleted:
        print(f"{log_file.name} archive: Deleted {deleted} archive(s) older than {max_age_days} days.")


# ---------------------------------------------------------------------------
# Shared scheduled-task retry/failover engine
# ---------------------------------------------------------------------------

def run_scheduled_stream(
    kind: str,
    channel_url: str,
    delay_seconds: int,
    channel_name: str,
    show_title: str,
    provider: str,
    task_id: int,
    episode_num: str,
    original_start_time: Optional[datetime],
    channels: List[Channel],
    epg: EpgData,
    cancel_event,
    remove_task_fn: Callable[[int], None],
    mpv_cmd_builder: Callable[[str], List[str]],
    log_fn: Callable[..., None],
    on_success: Optional[Callable[[], None]] = None,
    vpn_check: Optional[Callable[[], bool]] = None,
    on_stream_start: Optional[Callable[[], None]] = None,
    on_stream_stop: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Wait, then try-and-fail-over a scheduled stream (playback or recording).

    Waits until the scheduled time (cancellable via cancel_event), fires a
    5-min-out desktop notification, tries the original URL (one retry), then
    up to a few same-episode alternative channels, then searches for a future
    rerun. This is the ~500-line engine that used to be duplicated between
    scheduled_playback_task/scheduled_recording_task in each of Term-TV.py and
    Term-TV-VPN.py — those differed only in the mpv command (mpv_cmd_builder),
    where output is logged (log_fn), and what happens on success (on_success,
    e.g. subtitle extraction for recordings).

    mpv_cmd_builder(url) must return the full mpv argv (including "mpv" and a
    trailing "--" before url). log_fn must accept the same keyword arguments
    as log_mpv_output (channel_name, command, stdout, stderr, returncode).
    vpn_check, if given, is called right before the first launch attempt —
    if it returns False the task aborts without touching mpv (Term-TV-VPN.py
    only, so a dropped tunnel doesn't let a scheduled task air unprotected).
    on_stream_start/on_stream_stop bracket each individual mpv attempt (used
    by Term-TV-VPN.py to gate its Ctrl+C VPN-teardown guard during recording).

    Returns {"status": "cancelled" | "success" | "failed"} or
    {"status": "rerun_found", "rerun": <dict from find_future_reruns>, "new_delay": int}
    — the caller is responsible for building/persisting the reschedule task
    (its shape differs between playback and recording) and recursing.
    """
    tag = kind.upper()
    logging.info(f"Scheduled {kind} task created: {show_title} on {channel_name} [{provider}]")
    logging.info(f"  URL: {channel_url}")
    logging.info(f"  Delay: {delay_seconds} seconds ({delay_seconds // 60} minutes)")
    logging.info(f"  Episode: {episode_num}")

    print(f"\n[SCHEDULED] {kind.capitalize()} will start in {delay_seconds // 60} minutes...")
    print(f"[SCHEDULED] Channel: {channel_name} [{provider}]")
    print(f"[SCHEDULED] Show: {show_title}")
    print(f"[SCHEDULED] Will auto-launch when show starts\n")

    notify_wait = max(0, delay_seconds - 300)
    if cancel_event.wait(timeout=notify_wait):
        logging.info(f"{kind.capitalize()} task cancelled: {show_title}")
        return {"status": "cancelled"}
    if notify_wait > 0:
        send_desktop_notification("Term-TV", f"{kind.capitalize()} in 5 min: {show_title}")
        logging.info(f"Desktop notification sent for: {show_title}")
    if cancel_event.wait(timeout=delay_seconds - notify_wait):
        logging.info(f"{kind.capitalize()} task cancelled after notification: {show_title}")
        return {"status": "cancelled"}

    remove_task_fn(task_id)

    if vpn_check is not None and not vpn_check():
        logging.warning(f"VPN not connected — aborting scheduled {kind}: {show_title}")
        print(f"\n[{tag} FAILED] VPN is not connected — refusing to start {kind} for {show_title}")
        send_desktop_notification("Term-TV", f"{kind.capitalize()} skipped (VPN down): {show_title}")
        return {"status": "failed"}

    print(f"\n[{tag} STARTED] {channel_name} [{provider}] - {show_title}")
    logging.info(f"Starting {kind}: {show_title}")

    def _try_url(url: str, label: str) -> bool:
        """Launch mpv for *url*; wait up to 10s for an early failure, then wait
        for exit. Returns True on success (returncode 0 or 4 == user quit)."""
        mpv_cmd = mpv_cmd_builder(url)
        if on_stream_start:
            on_stream_start()
        try:
            proc = subprocess.Popen(mpv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = proc.communicate(timeout=10)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                stdout, stderr = proc.communicate()
                returncode = proc.returncode
        finally:
            if on_stream_stop:
                on_stream_stop()

        log_fn(channel_name=label, command=mpv_cmd, stdout=stdout, stderr=stderr, returncode=returncode)

        if returncode in (0, 4):
            logging.info(f"{kind.capitalize()} completed successfully (exit code: {returncode})")
            if on_success:
                on_success()
            return True

        logging.warning(f"mpv exited with code {returncode}")
        logging.debug(f"mpv stderr: {(stderr or '')[:500]}")
        return False

    # Try original URL (with one retry)
    for attempt in range(2):
        attempt_num = attempt + 1
        logging.info(f"Attempt {attempt_num}/2: Trying original URL {channel_url}")
        print(f"[{tag}] Attempt {attempt_num}/2: {channel_name} [{provider}]")
        try:
            if _try_url(channel_url, f"{channel_name} [{provider}] - {show_title}"):
                print(f"\n[{tag} COMPLETE]")
                return {"status": "success"}
        except FileNotFoundError:
            logging.error("mpv command not found")
            print("\nError: 'mpv' command not found. Is mpv installed?", file=sys.stderr)
            return {"status": "failed"}
        except Exception as e:
            logging.error(f"Attempt {attempt_num} failed with exception: {e}")
            print(f"[{tag}] Error: {e}")
        if attempt == 0:
            print(f"[{tag}] Retrying in 5 seconds...")
            time.sleep(5)

    # Original URL failed, try alternatives
    logging.warning("Original stream failed after 2 attempts, searching for alternatives")
    print(f"\n[{tag}] Original stream failed, searching for alternative providers...")

    if episode_num and original_start_time and channels and epg:
        alternatives = find_alternative_streams(
            channels, epg, show_title, episode_num, original_start_time, tolerance_minutes=5)
        for alt in alternatives:
            alt_channel = alt["channel"]
            alt_url = alt_channel.get("url", "")
            alt_provider = alt_channel.get("group-title", "Unknown Provider")
            alt_name = alt_channel.get("name", "Unknown")
            logging.info(f"Trying alternative: {alt_name} [{alt_provider}] - {alt_url}")
            print(f"[{tag}] Trying alternative: {alt_name} [{alt_provider}]")
            try:
                if _try_url(alt_url, f"{alt_name} [{alt_provider}] - {show_title} (alternative)"):
                    print(f"\n[{tag} COMPLETE]")
                    print(f"[{tag}] Used alternative provider: {alt_provider}")
                    return {"status": "success"}
                logging.warning("Alternative failed")
            except Exception as e:
                logging.error(f"Alternative stream failed: {e}")

    # All streams failed, search for future reruns
    logging.warning("All streams failed, searching for future reruns")
    print(f"\n[{tag}] All streams failed, searching for future reruns...")

    if episode_num and channels and epg:
        reruns = find_future_reruns(channels, epg, show_title, episode_num, hours_ahead=24)
        if reruns:
            next_rerun = reruns[0]
            next_start = next_rerun["start_time"]
            new_delay = max(0, int((next_start - datetime.now().astimezone()).total_seconds()))
            next_channel = next_rerun["channel"]
            logging.info(f"Found future rerun in {new_delay // 60} minutes on "
                         f"{next_channel.get('name', 'Unknown')} [{next_channel.get('group-title', 'Unknown Provider')}]")
            print(f"[{tag}] Found future rerun:")
            print(f"  Channel: {next_channel.get('name', 'Unknown')} [{next_channel.get('group-title', 'Unknown Provider')}]")
            print(f"  Time: {next_rerun['time_status']}")
            return {"status": "rerun_found", "rerun": next_rerun, "new_delay": new_delay}

    logging.error(f"{kind.capitalize()} failed completely: {show_title} - no alternatives or reruns found")
    print(f"\n[{tag} FAILED] Could not {kind} {show_title}")
    print(f"  All stream URLs failed and no future reruns found in the next 24 hours")
    return {"status": "failed"}


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
    except Exception as e:
        logging.warning(f"Failed to load watch history: {e}")
        return []


def log_channel_watch(channel: Channel, duration_seconds: int):
    """Record a channel watch session (ignored if < 2 minutes)."""
    if duration_seconds < 120:
        return
    history = load_watch_history()
    tvg_id = channel.get("tvg-id", "")
    url = channel.get("url", "")
    # Match by tvg-id when present; fall back to URL to avoid merging unrelated
    # channels that share an empty tvg-id.
    if tvg_id:
        existing = next((h for h in history if h.get("tvg-id") == tvg_id), None)
    else:
        existing = next((h for h in history if h.get("url") == url and h.get("tvg-id", "") == ""), None)
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
    """Return a sorted list of (group_name, channel_count) tuples.

    The M3U dedup step can merge group-title labels for duplicate-URL
    channels into e.g. "US HD, US SD" — split on comma so each real
    group is counted under its own name, matching ch_in_group().
    """
    counts: Dict[str, int] = defaultdict(int)
    for ch in channels:
        raw = ch.get("group-title", "")
        if not raw:
            continue
        for group in {g.strip() for g in raw.split(",") if g.strip()}:
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
# Channel notes (F2)
# ---------------------------------------------------------------------------

def load_channel_notes() -> Dict[str, str]:
    """Return saved channel notes as {key: note_text}."""
    if not CHANNEL_NOTES_FILE.exists():
        return {}
    try:
        with open(CHANNEL_NOTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load channel notes: {e}")
        return {}


def _channel_note_key(channel: Channel) -> str:
    return channel.get("tvg-id") or channel.get("url", "")


def get_channel_note(channel: Channel) -> str:
    """Return saved note for this channel, or empty string if none."""
    return load_channel_notes().get(_channel_note_key(channel), "")


def set_channel_note(channel: Channel, note: str):
    """Save (or clear) a personal note for this channel."""
    notes = load_channel_notes()
    key = _channel_note_key(channel)
    if note:
        notes[key] = note
    else:
        notes.pop(key, None)
    try:
        with open(CHANNEL_NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(notes, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save channel notes. {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Scheduled-task persistence (CLI)
# ---------------------------------------------------------------------------

def save_scheduled_tasks(tasks: List[Dict[str, Any]]):
    """Persist scheduled tasks so a CLI restart can re-arm them.

    Serialises only the JSON-safe payload fields; live objects
    (cancel_event threads) are skipped. Pass a snapshot of the list.
    """
    data = []
    for t in tasks:
        entry: Dict[str, Any] = {
            k: t[k]
            for k in ("id", "type", "channel_name", "provider", "show_title",
                      "url", "episode_num", "duration_seconds", "extract_subs")
            if k in t
        }
        st = t.get("scheduled_time")
        entry["scheduled_time"] = st.isoformat() if st else None
        ost = t.get("original_start_time")
        if ost:
            entry["original_start_time"] = ost.isoformat()
        if "output_path" in t:
            entry["output_path"] = str(t["output_path"])
        data.append(entry)
    try:
        with open(SCHEDULED_TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.warning(f"Could not save scheduled tasks: {e}")


def load_scheduled_tasks() -> List[Dict[str, Any]]:
    """Load persisted scheduled tasks, dropping ones whose time has passed.

    ISO datetime strings are parsed back into aware datetimes.
    """
    if not SCHEDULED_TASKS_FILE.exists():
        return []
    try:
        with open(SCHEDULED_TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logging.warning(f"Could not load scheduled tasks: {e}")
        return []
    now = datetime.now().astimezone()
    tasks: List[Dict[str, Any]] = []
    for t in data:
        try:
            st_raw = t.get("scheduled_time")
            st = datetime.fromisoformat(st_raw) if st_raw else None
            if not st or st <= now:
                continue  # expired while the CLI was closed
            t["scheduled_time"] = st
            if t.get("original_start_time"):
                t["original_start_time"] = datetime.fromisoformat(t["original_start_time"])
            tasks.append(t)
        except Exception as e:
            logging.warning(f"Skipping bad saved task: {e}")
    return tasks


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
