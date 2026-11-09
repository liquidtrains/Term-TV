#!/usr/bin/env python3
"""Build a local XMLTV guide for US/CA playlists with a self-hosted iptv-org/epg.

Why: playlists carry iptv-org channel ids (`Buzzr.us`), while public EPG
endpoints either drop those ids or disappear. iptv-org/epg is the open-source
grabber whose channel database uses exactly those ids, so running it locally
produces a guide that matches the playlists without any third-party service.

Everything the tool downloads (the grabber checkout, npm cache, API data and
the generated guide) is kept in the app's runtime data directory — i.e. NOT in
this repository — so a cloud-synced checkout is not filled with build output.

Usage:
    python tools/update_epg.py
    python tools/update_epg.py --ids-from <us.m3u> --ids-from <ca.m3u>
    python tools/update_epg.py --days 3 --max-connections 12 --update

Then point a playlist's ``epg_url`` at the printed guide path.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from collections import deque
from typing import Dict, Iterable, List, Optional, Tuple

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import lib.term_tv_core as core  # noqa: E402  (needs REPO_ROOT on sys.path)

GRABBER_URL = "https://codeload.github.com/iptv-org/epg/zip/refs/heads/master"
API_BASE = "https://iptv-org.github.io/api"
API_FILES = (
    "blocklist", "categories", "channels", "cities", "countries", "feeds",
    "guides", "languages", "logos", "regions", "streams", "subdivisions",
    "timezones",
)

DEFAULT_SOURCES = (
    ("Free-TV USA", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_usa.m3u8"),
    ("Free-TV Canada", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_canada.m3u8"),
)

# When several sites carry the same channel, prefer the ones that have proven
# reliable and cheap to fetch. Anything not listed sorts last, alphabetically.
SITE_PRIORITY = (
    "plex.tv", "pluto.tv", "i.mjh.nz", "ontvtonight.com", "tvguide.com",
    "tvhebdo.com", "samsungtvplus.com", "roku.com", "xumo.tv", "tubitv.com",
    "watch.whaletvplus.com", "epg.iptvx.one", "epgshare01.online",
)

# Sites that failed for *every* channel the last time this was run (TLS chain
# errors / 404s), so they are only used when nothing else carries the id.
SITE_FALLBACK = {"tvpassport.com", "tvtv.us", "distro.tv"}

# Print one progress line per this many lines of grabber output.
REPORT_EVERY = 50

# Written into the grabber checkout once `npm install` has finished, so an
# interrupted install is redone instead of being mistaken for a complete one.
INSTALL_MARKER = ".term-tv-installed"


# Grabber output contains characters a redirected Windows console (cp1252)
# cannot encode; replace them instead of crashing mid-run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"Error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def normalize_id(xmltv_id: str) -> str:
    return (xmltv_id or "").split("@", 1)[0].strip()


feed_region = core.feed_region  # which part of an "@feed" suffix changes the schedule


def fetch_text(source: str) -> str:
    """Read *source* from a local path or an HTTP(S) URL."""
    path = core._local_source_path(source)
    if path is not None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            die(f"could not read {path}: {e}")
    try:
        r = requests.get(source, timeout=60, headers={"User-Agent": "term-tv-epg/1.0"})
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        die(f"could not download {source}: {e}")


def load_target_ids(sources: Iterable[Tuple[str, str]]) -> "Dict[str, str]":
    """Return {tvg-id: display name} for every playlist in *sources*.

    Ids keep their "@feed" suffix: "HBO.us@East" and "HBO.us@West" (or two
    local NBC stations) are different schedules and need their own guide entry.
    """
    ids: Dict[str, str] = {}
    for label, source in sources:
        text = fetch_text(source)
        found = 0
        for line in text.splitlines():
            if not line.startswith("#EXTINF"):
                continue
            m = re.search(r'tvg-id="([^"]+)"', line)
            if not m:
                continue
            tvg_id = m.group(1).strip()
            if not normalize_id(tvg_id):
                continue
            name = line.rsplit(",", 1)[-1].strip() or tvg_id
            if tvg_id not in ids:
                ids[tvg_id] = name
                found += 1
        log(f"  {label}: {found} new id(s) ({len(ids)} total)")
    return ids


def config_sources() -> List[Tuple[str, str]]:
    """(name, m3u_url) for every enabled playlist in config.json.

    Looks in the current directory first (where the apps read it), then next
    to the repository.
    """
    for path in (Path("config.json"), REPO_ROOT / "config.json"):
        if path.is_file():
            break
    else:
        die("--from-config: no config.json in the current directory or the repository")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"could not read {path}: {e}")
    playlists = core.validate_playlists(config.get("playlists", []))
    if not playlists:
        die(f"--from-config: {path} has no enabled playlists")
    return [(pl["name"], pl["m3u_url"]) for pl in playlists]


def download_grabber(grabber_dir: Path, force: bool) -> None:
    if grabber_dir.exists() and not force:
        log(f"Using existing grabber: {grabber_dir}")
        return
    log(f"Downloading iptv-org/epg -> {grabber_dir} ...")
    try:
        r = requests.get(GRABBER_URL, timeout=300, headers={"User-Agent": "term-tv-epg/1.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        die(f"could not download the grabber: {e}")
    # Extract next to the target and swap it in only once that worked, so a
    # failed download or extraction never destroys a working checkout.
    grabber_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = grabber_dir.parent / (grabber_dir.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        root = next(n for n in z.namelist() if n.endswith("/") and n.count("/") == 1)
        z.extractall(staging)
    if grabber_dir.exists():
        shutil.rmtree(grabber_dir)
    (staging / root.strip("/")).rename(grabber_dir)
    shutil.rmtree(staging, ignore_errors=True)
    log(f"  {len(r.content) / 1048576:.1f} MB extracted")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_GRAB_LINE_RE = re.compile(r"\[(\d+)/(\d+)\]")


def run(cmd: List[str], cwd: Path, env: Dict[str, str], timeout: Optional[int] = None,
        progress: bool = False) -> int:
    """Run *cmd*, killing it after *timeout* seconds.

    With *progress*, the grabber's "[n/total] site - id - date (N programs)"
    lines are followed and one is printed per REPORT_EVERY grabs while it runs
    (the grab takes minutes and used to print nothing until it exited), plus a
    count of grabs that came back empty. The last lines are always shown if
    the command fails.
    """
    log("  $ " + " ".join(str(c) for c in cmd))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        die(f"'{cmd[0]}' not found on PATH")
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    tail: deque = deque(maxlen=40)
    total = empty = 0
    try:
        for raw in proc.stdout:
            line = _ANSI_RE.sub("", raw).rstrip()
            # Skip blank lines and the indented bodies of dumped request errors.
            if not line or line == "}" or (line[0].isspace() and not _GRAB_LINE_RE.search(line)):
                continue
            tail.append(line)
            m = _GRAB_LINE_RE.search(line) if progress else None
            if not m:
                continue
            n, total = int(m.group(1)), int(m.group(2))
            if "(0 programs)" in line:
                empty += 1
            if n % REPORT_EVERY == 0 or n == total:
                log(f"  {n}/{total} | {line[m.end():].strip()[:100]}")
        code = proc.wait()
    finally:
        if timer:
            timer.cancel()
    if timed_out.is_set():
        print("\n".join(tail), file=sys.stderr)
        die(f"command timed out after {timeout}s")
    if code != 0:
        print("\n".join(tail), file=sys.stderr)
    elif not progress and tail:
        print("\n".join(list(tail)[-5:]))
    if progress and empty:
        log(f"  {empty} of {total} grabs returned no programmes (site errors or no listings)")
    return code


def ensure_node_modules(grabber_dir: Path, data_dir: Path) -> None:
    if (grabber_dir / INSTALL_MARKER).is_file():
        return
    npm = shutil.which("npm")
    if not npm:
        die("npm not found — install Node.js 18+ (https://nodejs.org) and retry")
    log("Installing grabber dependencies (one-off, ~30 s) ...")
    env = dict(os.environ)
    env["npm_config_cache"] = str(data_dir / "npm-cache")
    code = run(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=grabber_dir, env=env, timeout=1200,
    )
    if code != 0:
        die("npm install failed")
    (grabber_dir / INSTALL_MARKER).write_text("ok\n", encoding="utf-8")


def ensure_api_data(grabber_dir: Path, force: bool) -> None:
    data_dir = grabber_dir / "temp" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    missing = [n for n in API_FILES if force or not (data_dir / f"{n}.json").is_file()]
    if not missing:
        return
    log(f"Fetching iptv-org API data ({len(missing)} file(s)) ...")
    for name in missing:
        try:
            r = requests.get(f"{API_BASE}/{name}.json", timeout=120,
                             headers={"User-Agent": "term-tv-epg/1.0"})
            r.raise_for_status()
            (data_dir / f"{name}.json").write_bytes(r.content)
        except requests.RequestException as e:
            die(f"could not fetch API file {name}.json: {e}")


def build_site_index(grabber_dir: Path) -> Dict[str, List[Dict[str, str]]]:
    """Map normalized xmltv id -> candidate site entries from the grabber."""
    index: Dict[str, List[Dict[str, str]]] = {}
    files = sorted((grabber_dir / "sites").glob("*/*.channels.xml"))
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for ch in root.iter("channel"):
            xmltv_id = (ch.get("xmltv_id") or "").strip()
            site = ch.get("site") or f.parent.name
            site_id = ch.get("site_id") or ""
            if not xmltv_id or not site_id:
                continue
            index.setdefault(normalize_id(xmltv_id), []).append({
                "site": site,
                "site_id": site_id,
                "lang": ch.get("lang") or "en",
                "xmltv_id": xmltv_id,
                "name": (ch.text or "").strip(),
            })
    return index


def rank(entry: Dict[str, str]) -> Tuple[int, int, str]:
    site = entry["site"]
    if site in SITE_FALLBACK:
        return (1, 0, site)  # only if no other site carries this channel
    prio = SITE_PRIORITY.index(site) if site in SITE_PRIORITY else len(SITE_PRIORITY)
    return (0, prio, site)


def pick_source(tvg_id: str, options: List[Dict[str, str]], loose: bool) -> Optional[Dict[str, str]]:
    """Best site entry for *tvg_id* among the entries for its bare channel id.

    The exact feed wins, then any entry with the same schedule (see
    feed_region). An entry for a different region or station is only used
    with *loose*, because its programmes air at other times or not at all.
    """
    exact = [o for o in options if o["xmltv_id"] == tvg_id]
    if exact:
        return sorted(exact, key=rank)[0]
    region = feed_region(tvg_id)
    same = [o for o in options if feed_region(o["xmltv_id"]) == region]
    if same:
        return sorted(same, key=rank)[0]
    return sorted(options, key=rank)[0] if loose else None


def write_channels_file(
    path: Path, targets: Dict[str, str], index: Dict[str, List[Dict[str, str]]],
    loose: bool = False,
) -> Tuple[int, List[str], List[str]]:
    """Write one site entry per target id.

    Returns (matched count, ids with no source at all, ids skipped because the
    only sources are for a different regional feed or station).
    """
    root = ET.Element("channels")
    matched: List[str] = []
    missing: List[str] = []
    wrong_feed: List[str] = []
    for tvg_id, name in targets.items():
        options = index.get(normalize_id(tvg_id))
        if not options:
            missing.append(tvg_id)
            continue
        best = pick_source(tvg_id, options, loose)
        if best is None:
            wrong_feed.append(tvg_id)
            continue
        el = ET.SubElement(root, "channel", {
            "site": best["site"],
            "site_id": best["site_id"],
            "lang": best["lang"],
            # Emit the playlist's own id (feed suffix included), so the guide
            # matches the playlist exactly and feeds stay separate in the app.
            "xmltv_id": tvg_id,
        })
        el.text = name
        matched.append(tvg_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return len(matched), missing, wrong_feed


def run_grab(grabber_dir: Path, channels_file: Path, out_file: Path,
             days: int, max_connections: int, timeout_ms: int,
             data_dir: Path, grab_timeout: Optional[int] = None) -> None:
    node = shutil.which("node")
    if not node:
        die("node not found — install Node.js 18+ (https://nodejs.org) and retry")
    env = dict(os.environ)
    env["SWC_NATIVE_BINDING_CACHE"] = str(data_dir / "swc-cache")
    cmd = [
        node, "-r", "@swc-node/register", "scripts/commands/epg/grab.ts",
        f"--channels={channels_file}",
        f"--days={days}",
        f"--output={out_file}",
        f"--maxConnections={max_connections}",
        f"--timeout={timeout_ms}",
    ]
    log(f"Grabbing guide for {days} day(s) ...")
    code = run(cmd, cwd=grabber_dir, env=env, timeout=grab_timeout, progress=True)
    if code != 0:
        die(f"guide grab failed (exit {code})")


def guide_stats(path: Path) -> Tuple[int, int]:
    """Return (channel count, programme count) without holding the XML in memory."""
    channels = programmes = 0
    try:
        with gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb") as fh:
            for _event, elem in ET.iterparse(fh, events=("end",)):
                if elem.tag == "channel":
                    channels += 1
                elif elem.tag == "programme":
                    programmes += 1
                elem.clear()
    except Exception as e:
        log(f"  (could not count guide contents: {e})")
    return channels, programmes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a local XMLTV guide for US/CA playlists with iptv-org/epg")
    parser.add_argument("--ids-from", action="append", metavar="URL_OR_PATH",
                        help="M3U playlist to take tvg-ids from (repeatable; "
                             "default: Free-TV USA + Canada)")
    parser.add_argument("--from-config", action="store_true",
                        help="Take tvg-ids from the enabled playlists in config.json "
                             "(can be combined with --ids-from)")
    parser.add_argument("--grab-timeout", type=int, default=60, metavar="MINUTES",
                        help="Give up on the whole grab after this long (default 60, 0 = never)")
    parser.add_argument("--days", type=int, default=2,
                        help="Days of guide data to fetch (default 2)")
    parser.add_argument("--max-connections", type=int, default=8,
                        help="Concurrent grab requests (default 8)")
    parser.add_argument("--timeout", type=int, default=20000,
                        help="Per-request timeout in ms (default 20000)")
    parser.add_argument("--update", action="store_true",
                        help="Re-download the grabber checkout")
    parser.add_argument("--refresh-api", action="store_true",
                        help="Re-download the iptv-org API data")
    parser.add_argument("--keep-xml", action="store_true",
                        help="Keep the uncompressed guide.xml as well")
    parser.add_argument("--loose-feeds", action="store_true",
                        help="When a channel's own feed (e.g. @West, a local station) has "
                             "no source, use another feed's schedule instead of none")
    parser.add_argument("--data-dir", metavar="PATH",
                        help="Override the runtime data directory for this run")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else core.DATA_DIR
    grabber_dir = data_dir / "epg-grabber"
    guide_dir = data_dir / "guide"
    channels_file = guide_dir / "term-tv.channels.xml"
    out_file = guide_dir / "guide.xml"
    gz_file = guide_dir / "guide.xml.gz"

    log(f"Runtime data: {data_dir}")
    if args.data_dir:
        guide_dir.mkdir(parents=True, exist_ok=True)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)

    sources: List[Tuple[str, str]] = []
    if args.from_config:
        sources += config_sources()
    sources += [(Path(s).name if core._local_source_path(s) else s, s) for s in args.ids_from or ()]
    if not sources:
        sources = list(DEFAULT_SOURCES)
    log("Collecting channel ids from playlists ...")
    targets = load_target_ids(sources)
    if not targets:
        die("no tvg-ids found in the given playlists")

    download_grabber(grabber_dir, args.update)
    ensure_node_modules(grabber_dir, data_dir)
    ensure_api_data(grabber_dir, args.refresh_api)

    log("Mapping ids to grabber sites ...")
    index = build_site_index(grabber_dir)
    matched, missing, wrong_feed = write_channels_file(
        channels_file, targets, index, loose=args.loose_feeds)
    log(f"  {matched}/{len(targets)} ids have a guide source")
    if wrong_feed:
        log(f"  {len(wrong_feed)} id(s) skipped: only a different regional feed/station "
            "has a source (--loose-feeds uses it anyway): "
            + ", ".join(sorted(wrong_feed)[:10]) + (" ..." if len(wrong_feed) > 10 else ""))
    if missing:
        log("  no source for: " + ", ".join(sorted(missing)[:20])
            + (" ..." if len(missing) > 20 else ""))
    if matched == 0:
        die("none of the playlist ids are supported by iptv-org/epg sites")

    if out_file.exists():
        out_file.unlink()
    run_grab(grabber_dir, channels_file, out_file, args.days,
             args.max_connections, args.timeout, data_dir,
             grab_timeout=args.grab_timeout * 60 if args.grab_timeout > 0 else None)
    if not out_file.exists() or out_file.stat().st_size == 0:
        die("the grabber produced no guide file")

    channels, programmes = guide_stats(out_file)
    log(f"Guide: {channels} channels / {programmes} programmes "
        f"({out_file.stat().st_size / 1048576:.1f} MB XML)")
    if programmes == 0:
        die("the guide contains no programmes — try a larger --days or different ids")

    # Compress to a temporary name and rename over the old guide, so the app
    # never reads a half-written file and a failure here keeps the old guide.
    tmp_gz = gz_file.with_name(gz_file.name + ".tmp")
    with open(out_file, "rb") as src, gzip.open(tmp_gz, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp_gz, gz_file)
    if not args.keep_xml:
        out_file.unlink()
    log(f"\nWrote {gz_file} ({gz_file.stat().st_size / 1048576:.1f} MB)")
    log("Point the playlist's epg_url at it, e.g.:")
    log(f'  "epg_url": "{gz_file.as_posix()}"')
    log("Regenerate whenever the guide goes stale (the app reads the file directly).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
