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
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
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

REPORT_EVERY = 50


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"Error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def normalize_id(xmltv_id: str) -> str:
    return (xmltv_id or "").split("@", 1)[0].strip()


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
    """Return {normalized tvg-id: display name} for every playlist in *sources*."""
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
            tvg_id = normalize_id(m.group(1))
            if not tvg_id:
                continue
            name = line.rsplit(",", 1)[-1].strip() or tvg_id
            if tvg_id not in ids:
                ids[tvg_id] = name
                found += 1
        log(f"  {label}: {found} new id(s) ({len(ids)} total)")
    return ids


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
    if grabber_dir.exists():
        shutil.rmtree(grabber_dir)
    grabber_dir.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        root = next(n for n in z.namelist() if n.endswith("/") and n.count("/") == 1)
        z.extractall(grabber_dir.parent)
    extracted = grabber_dir.parent / root.strip("/")
    extracted.rename(grabber_dir)
    log(f"  {len(r.content) / 1048576:.1f} MB extracted")


def run(cmd: List[str], cwd: Path, env: Dict[str, str], timeout: Optional[int] = None) -> int:
    log("  $ " + " ".join(str(c) for c in cmd))
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, timeout=timeout,
            capture_output=True, text=True, errors="replace",
        )
    except FileNotFoundError:
        die(f"'{cmd[0]}' not found on PATH")
    except subprocess.TimeoutExpired:
        die(f"command timed out after {timeout}s")
    if proc.stdout.strip():
        print(proc.stdout.strip()[-4000:])
    if proc.returncode != 0 and proc.stderr.strip():
        print(proc.stderr.strip()[-4000:], file=sys.stderr)
    return proc.returncode


def ensure_node_modules(grabber_dir: Path, data_dir: Path) -> None:
    if (grabber_dir / "node_modules").is_dir():
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


def write_channels_file(
    path: Path, targets: Dict[str, str], index: Dict[str, List[Dict[str, str]]],
) -> Tuple[int, List[str]]:
    """Write one site entry per target id; return (matched count, missing ids)."""
    root = ET.Element("channels")
    matched: List[str] = []
    missing: List[str] = []
    for tvg_id, name in targets.items():
        options = index.get(tvg_id)
        if not options:
            missing.append(tvg_id)
            continue
        best = sorted(options, key=rank)[0]
        el = ET.SubElement(root, "channel", {
            "site": best["site"],
            "site_id": best["site_id"],
            "lang": best["lang"],
            # Emit the bare id: the guide then matches playlists directly and
            # variants from different sites cannot collide in the app.
            "xmltv_id": tvg_id,
        })
        el.text = name
        matched.append(tvg_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return len(matched), missing


def run_grab(grabber_dir: Path, channels_file: Path, out_file: Path,
             days: int, max_connections: int, timeout_ms: int,
             data_dir: Path) -> None:
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
    code = run(cmd, cwd=grabber_dir, env=env)
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

    sources = DEFAULT_SOURCES if not args.ids_from else tuple(
        (Path(s).name if core._local_source_path(s) else s, s) for s in args.ids_from)
    log("Collecting channel ids from playlists ...")
    targets = load_target_ids(sources)
    if not targets:
        die("no tvg-ids found in the given playlists")

    download_grabber(grabber_dir, args.update)
    ensure_node_modules(grabber_dir, data_dir)
    ensure_api_data(grabber_dir, args.refresh_api)

    log("Mapping ids to grabber sites ...")
    index = build_site_index(grabber_dir)
    matched, missing = write_channels_file(channels_file, targets, index)
    log(f"  {matched}/{len(targets)} ids have a guide source")
    if missing:
        log("  no source for: " + ", ".join(sorted(missing)[:20])
            + (" ..." if len(missing) > 20 else ""))
    if matched == 0:
        die("none of the playlist ids are supported by iptv-org/epg sites")

    if out_file.exists():
        out_file.unlink()
    run_grab(grabber_dir, channels_file, out_file, args.days,
             args.max_connections, args.timeout, data_dir)
    if not out_file.exists() or out_file.stat().st_size == 0:
        die("the grabber produced no guide file")

    channels, programmes = guide_stats(out_file)
    log(f"Guide: {channels} channels / {programmes} programmes "
        f"({out_file.stat().st_size / 1048576:.1f} MB XML)")
    if programmes == 0:
        die("the guide contains no programmes — try a larger --days or different ids")

    with open(out_file, "rb") as src, gzip.open(gz_file, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    if not args.keep_xml:
        out_file.unlink()
    log(f"\nWrote {gz_file} ({gz_file.stat().st_size / 1048576:.1f} MB)")
    log("Point the playlist's epg_url at it, e.g.:")
    log(f'  "epg_url": "{gz_file.as_posix()}"')
    log("Regenerate whenever the guide goes stale (the app reads the file directly).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
