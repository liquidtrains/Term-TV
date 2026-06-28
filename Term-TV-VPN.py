#!/usr/bin/env python3
import sys
import subprocess

# Force UTF-8 output on Windows (avoids CP1252 UnicodeEncodeError for emoji/symbols)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _ensure_dependencies():
    """Auto-install required packages if missing."""
    try:
        import requests  # noqa: F401
    except ImportError:
        print("Required package 'requests' not found. Installing...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
            print("Successfully installed 'requests'.\n")
        except subprocess.CalledProcessError:
            print("Error: Failed to install 'requests'. Please run: pip install requests", file=sys.stderr)
            sys.exit(1)

_ensure_dependencies()

import requests
import re
import lzma
import json
import argparse
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
import threading
import time
import hashlib
import logging
import atexit
import platform
import shutil
import os

from lib.term_tv_core import (
    Channel, EpgData, ShowResult,
    WATCH_HISTORY_FILE, SEARCH_HISTORY_FILE, FAVORITES_FILE, RECORDINGS_DIR,
    EPG_CACHE_DIR, M3U_CACHE_DIR,
    load_m3u, load_m3u_cached, load_epg,
    parse_epg_time, is_new_episode,
    search_channels, search_shows_in_timeframe,
    find_alternative_streams, find_future_reruns,
    log_channel_watch, load_watch_history, get_frequent_channels,
    display_frequent_channels, get_search_history_now_playing,
    display_search_history_now_playing,
    load_search_history, save_search_history, add_to_search_history,
    load_favorites, save_favorites, toggle_favorite,
    get_favorite_channels, display_favorites,
    get_channel_groups, get_channel_schedule,
    send_desktop_notification,
    ensure_recordings_dir, extract_subtitles_from_recording, get_safe_filename,
    clean_old_cache_files, get_public_ip, check_vpn_status, input_with_countdown,
    _ch_in_group,
    get_channel_note, set_channel_note,
)

# --- Constants ---
CONFIG_FILE = Path("config.json")
LOG_FILE = Path("term-tv.log")
MPV_LOG_FILE = Path("mpv-output.log")
MPV_LOG_ARCHIVE_DIR = Path("mpv-log-archive")
MPV_LOG_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per chunk

# --- Shared global state (for scheduled background tasks) ---
SCHEDULED_TASKS: list = []
SCHEDULED_TASKS_LOCK = threading.Lock()
channels_global: list = []
epg_global: dict = {}

# --- Logging Configuration ---
def setup_logging():
    """Configure logging to both file and console with detailed formatting."""
    # Create formatter with detailed information
    log_format = '%(asctime)s - [%(levelname)s] - %(funcName)s:%(lineno)d - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'

    # Configure root logger
    logging.basicConfig(
        level=logging.DEBUG,
        format=log_format,
        datefmt=date_format,
        handlers=[
            # File handler - captures everything
            logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8'),
            # Console handler - only warnings and above
            logging.StreamHandler(sys.stderr)
        ]
    )

    # Set console handler to WARNING level (file still gets DEBUG)
    console_handler = logging.getLogger().handlers[1]
    console_handler.setLevel(logging.WARNING)

    # Log startup
    logging.info("="*80)
    logging.info("Term-TV started")
    logging.info(f"Log file: {LOG_FILE.absolute()}")
    logging.info(f"MPV output log: {MPV_LOG_FILE.absolute()}")


def archive_mpv_log():
    """
    Called on exit. Archives mpv-output.log using LZMA (.xz) compression — the
    highest-ratio format available in the Python standard library, and fully
    recoverable with any xz-compatible tool or `lzma.open()`.

    Behaviour:
      - If log is <= 5 MB  : compressed into a single timestamped .log.xz file.
      - If log is >  5 MB  : split into 5 MB chunks, each compressed separately.
      - Active log is cleared after successful archiving.
      - Archives older than 1 year are deleted from mpv-log-archive/.
    """
    if not MPV_LOG_FILE.exists() or MPV_LOG_FILE.stat().st_size == 0:
        return

    MPV_LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        data = MPV_LOG_FILE.read_bytes()
    except Exception as e:
        logging.warning(f"archive_mpv_log: could not read log: {e}")
        return

    try:
        if len(data) <= MPV_LOG_CHUNK_SIZE:
            # Fits in a single archive
            archive_path = MPV_LOG_ARCHIVE_DIR / f"mpv-output-{timestamp}.log.xz"
            with lzma.open(archive_path, "wb", preset=9) as xz_f:
                xz_f.write(data)
            orig_kb = len(data) // 1024
            comp_kb = archive_path.stat().st_size // 1024
            print(f"MPV log archived: {archive_path.name}  ({orig_kb} KB → {comp_kb} KB compressed)")
            logging.info(f"MPV log archived to {archive_path.name} ({orig_kb} KB raw, {comp_kb} KB compressed)")
        else:
            # Too large — split into 5 MB chunks
            total = (len(data) + MPV_LOG_CHUNK_SIZE - 1) // MPV_LOG_CHUNK_SIZE
            for idx in range(total):
                chunk = data[idx * MPV_LOG_CHUNK_SIZE : (idx + 1) * MPV_LOG_CHUNK_SIZE]
                archive_path = (
                    MPV_LOG_ARCHIVE_DIR
                    / f"mpv-output-{timestamp}-part{idx + 1:03d}of{total:03d}.log.xz"
                )
                with lzma.open(archive_path, "wb", preset=9) as xz_f:
                    xz_f.write(chunk)
            orig_mb = len(data) / (1024 * 1024)
            print(f"MPV log split into {total} chunk(s) and archived  ({orig_mb:.1f} MB total)")
            logging.info(f"MPV log archived in {total} chunks ({orig_mb:.1f} MB)")

        # Clear the active log so the next session starts fresh
        MPV_LOG_FILE.write_bytes(b"")

    except Exception as e:
        logging.warning(f"archive_mpv_log: compression failed: {e}")
        return

    # Purge archives older than 1 year
    cutoff = datetime.now() - timedelta(days=365)
    deleted = 0
    try:
        for archive in MPV_LOG_ARCHIVE_DIR.iterdir():
            if not archive.is_file() or not archive.name.startswith("mpv-output-"):
                continue
            try:
                if datetime.fromtimestamp(archive.stat().st_mtime) < cutoff:
                    archive.unlink()
                    deleted += 1
                    logging.info(f"Deleted old MPV log archive: {archive.name}")
            except Exception:
                pass
    except Exception as e:
        logging.warning(f"archive_mpv_log: purge scan failed: {e}")

    if deleted:
        print(f"MPV log archive: Deleted {deleted} archive(s) older than 1 year.")



def log_mpv_output(channel_name: str, command: List[str], stdout: str = "", stderr: str = "", returncode: Optional[int] = None):
    """
    Logs mpv console output to the dedicated mpv log file.

    Args:
        channel_name: Name of the channel being played
        command: The mpv command that was executed
        stdout: Standard output from mpv
        stderr: Standard error from mpv
        returncode: Exit code from mpv process
    """
    try:
        with open(MPV_LOG_FILE, 'a', encoding='utf-8') as f:
            # Write header with timestamp
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"\n{'='*80}\n")
            f.write(f"[{timestamp}] Channel: {channel_name}\n")
            f.write(f"Command: {' '.join(command)}\n")
            if returncode is not None:
                f.write(f"Exit Code: {returncode}\n")
            f.write(f"{'-'*80}\n")

            # Write stdout if present
            if stdout:
                f.write("STDOUT:\n")
                f.write(stdout)
                f.write("\n")

            # Write stderr if present
            if stderr:
                f.write("STDERR:\n")
                f.write(stderr)
                f.write("\n")

            f.write(f"{'='*80}\n")

    except Exception as e:
        logging.warning(f"Failed to write mpv output to log: {e}")

def run_mpv_with_logging(mpv_args: List[str], channel_name: str) -> subprocess.CompletedProcess:
    """
    Runs mpv with output displayed in terminal AND logged to file.

    Args:
        mpv_args: Command arguments for mpv
        channel_name: Channel name for logging

    Returns:
        subprocess.CompletedProcess with returncode
    """
    import subprocess

    log_file = None
    try:
        try:
            log_file = open(MPV_LOG_FILE, 'a', encoding='utf-8')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_file.write(f"\n{'='*80}\n")
            log_file.write(f"[{timestamp}] Channel: {channel_name}\n")
            log_file.write(f"Command: {' '.join(mpv_args)}\n")
            log_file.write(f"{'-'*80}\n")
            log_file.write("OUTPUT (combined stdout/stderr):\n")
            log_file.flush()
        except Exception as e:
            logging.warning(f"Failed to open mpv log file: {e}")
            log_file = None

        process = subprocess.Popen(
            mpv_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )

        for line in process.stdout:
            stripped = line.strip()

            if log_file:
                log_file.write(line)
                log_file.flush()

            show_in_terminal = True

            if (stripped.startswith('AV:') or
                stripped.startswith('(Buffering)') or
                stripped.startswith('(Paused)') or
                stripped.startswith('●') or
                stripped.startswith('○') or
                stripped.startswith('AO:') or
                stripped.startswith('VO:') or
                "Can't load unknown script:" in stripped or
                '[videoclip_master]' in stripped or
                '[command_palette]' in stripped):
                show_in_terminal = False

            if ('error' in stripped.lower() or
                'warning' in stripped.lower() or
                'desynchronisation detected' in stripped.lower() or
                'Invalid audio PTS' in stripped or
                'Invalid video PTS' in stripped or
                '[ffmpeg/' in stripped or
                'Exiting...' in stripped or
                'Failed' in stripped):
                show_in_terminal = True

            if "Can't load unknown script:" in stripped:
                show_in_terminal = False

            if show_in_terminal:
                print(line, end='')

        returncode = process.wait()

        if log_file:
            log_file.write(f"\n{'-'*80}\n")
            log_file.write(f"Exit Code: {returncode}\n")
            log_file.write(f"{'='*80}\n")

        return subprocess.CompletedProcess(mpv_args, returncode, None, None)

    except Exception as e:
        if log_file:
            try:
                log_file.write(f"\nEXCEPTION: {e}\n{'='*80}\n")
            except Exception:
                pass
        raise
    finally:
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass


def display_scheduled_tasks():
    """Displays pending scheduled playback/recording tasks."""
    with SCHEDULED_TASKS_LOCK:
        tasks_snapshot = list(SCHEDULED_TASKS)

    if not tasks_snapshot:
        return

    print("\n" + "="*80)
    print("SCHEDULED TASKS:")
    print("="*80)

    now = datetime.now().astimezone()

    for task in tasks_snapshot:
        task_type = task.get("type", "unknown")
        channel_name = task.get("channel_name", "Unknown")
        provider = task.get("provider", "Unknown Provider")
        show_title = task.get("show_title", "Unknown")
        scheduled_time = task.get("scheduled_time")

        if scheduled_time:
            time_diff = scheduled_time - now
            minutes_until = int(time_diff.total_seconds() / 60)

            if minutes_until < 0:
                time_str = "Starting now..."
            elif minutes_until == 0:
                time_str = "Starting now"
            elif minutes_until < 60:
                time_str = f"In {minutes_until} min"
            else:
                hours = minutes_until // 60
                mins = minutes_until % 60
                time_str = f"In {hours}h {mins}m"

            # Format task type for display
            if task_type == "playback":
                type_icon = ">"
                type_label = "PLAYBACK"
            elif task_type == "recording":
                type_icon = "o"
                type_label = "RECORD"
            elif task_type == "reminder":
                type_icon = "*"
                type_label = "REMIND"
            else:
                type_icon = "-"
                type_label = task_type.upper()

            print(f"{type_icon} [{type_label}] {time_str}")
            print(f"   {show_title}")
            print(f"   Channel: {channel_name} [{provider}]")
            print()


def manage_scheduled_tasks():
    """Interactive menu to cancel pending scheduled tasks."""
    with SCHEDULED_TASKS_LOCK:
        tasks = list(SCHEDULED_TASKS)

    if not tasks:
        print("No scheduled tasks to manage.")
        return

    now = datetime.now().astimezone()
    print("\n" + "="*60)
    print("MANAGE SCHEDULED TASKS  (enter number to cancel)")
    print("="*60)
    for i, task in enumerate(tasks, 1):
        task_type = task.get("type", "unknown").upper()
        show_title = task.get("show_title", "Unknown")
        channel_name = task.get("channel_name", "Unknown")
        scheduled_time = task.get("scheduled_time")
        if scheduled_time:
            minutes_until = max(0, int((scheduled_time - now).total_seconds() / 60))
            time_str = f"in {minutes_until}m" if minutes_until < 60 else f"in {minutes_until // 60}h {minutes_until % 60}m"
        else:
            time_str = "unknown time"
        print(f"  {i}. [{task_type}] {show_title} on {channel_name} ({time_str})")
    print("  0. Back")

    raw = input("\nCancel task #: ").strip()
    if not raw or not raw.isdigit():
        return
    idx = int(raw) - 1
    if idx < 0:
        return
    if idx >= len(tasks):
        print("Invalid selection.")
        return

    task = tasks[idx]
    cancel_event = task.get("cancel_event")
    if cancel_event:
        cancel_event.set()
    with SCHEDULED_TASKS_LOCK:
        SCHEDULED_TASKS[:] = [t for t in SCHEDULED_TASKS if t.get("id") != task.get("id")]
    print(f"✓ Cancelled: {task.get('show_title', 'task')}")


def scheduled_playback_task(channel_url: str, delay_seconds: int, channel_name: str, show_title: str, provider: str = "Unknown Provider", task_id: int = 0, episode_num: str = "", original_start_time: Optional[datetime] = None, cancel_event: Optional[threading.Event] = None):
    """
    Background task that waits then launches playback with retry logic.

    If the original stream fails:
    1. Retries once on the same URL
    2. Searches for alternative streams (same episode, different providers)
    3. If all fail, searches for future reruns and schedules the next one
    """
    global SCHEDULED_TASKS, channels_global, epg_global

    logging.info(f"Scheduled playback task created: {show_title} on {channel_name} [{provider}]")
    logging.info(f"  URL: {channel_url}")
    logging.info(f"  Delay: {delay_seconds} seconds ({delay_seconds // 60} minutes)")
    logging.info(f"  Episode: {episode_num}")

    print(f"\n[SCHEDULED] Playback will start in {delay_seconds // 60} minutes...")
    print(f"[SCHEDULED] Channel: {channel_name} [{provider}]")
    print(f"[SCHEDULED] Show: {show_title}")
    print(f"[SCHEDULED] Will auto-launch when show starts\n")

    # Wait for the scheduled time; fire a desktop notification 5 min before start
    _evt = cancel_event or threading.Event()
    _notify_wait = max(0, delay_seconds - 300)
    if _evt.wait(timeout=_notify_wait):
        logging.info(f"Playback task cancelled: {show_title}")
        return
    if _notify_wait > 0:
        send_desktop_notification("Term-TV", f"Starting in 5 min: {show_title}")
        logging.info(f"Desktop notification sent for: {show_title}")
    if _evt.wait(timeout=delay_seconds - _notify_wait):
        logging.info(f"Playback task cancelled after notification: {show_title}")
        return

    # Remove from scheduled tasks list
    with SCHEDULED_TASKS_LOCK:
        SCHEDULED_TASKS[:] = [t for t in SCHEDULED_TASKS if t.get("id") != task_id]

    # Start playback with retry logic
    print(f"\n[PLAYBACK STARTED] {channel_name} [{provider}] - {show_title}")
    logging.info(f"Starting playback: {show_title}")

    # Try original URL (with one retry)
    for attempt in range(2):
        attempt_num = attempt + 1
        logging.info(f"Attempt {attempt_num}/2: Trying original URL {channel_url}")
        print(f"[PLAYBACK] Attempt {attempt_num}/2: {channel_name} [{provider}]")

        try:
            mpv_cmd = ["mpv", "--stream-lavf-o=timeout=10000000", channel_url]
            proc = subprocess.Popen(
                mpv_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            try:
                stdout, stderr = proc.communicate(timeout=10)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                stdout, stderr = proc.communicate()
                returncode = proc.returncode

            # Log mpv output to dedicated log file
            log_mpv_output(
                channel_name=f"{channel_name} [{provider}] - {show_title}",
                command=mpv_cmd,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode
            )

            # If mpv exited successfully or user quit (returncode 0 or 4), consider it success
            if returncode in (0, 4):
                logging.info(f"Playback completed successfully (exit code: {returncode})")
                print(f"\n[PLAYBACK COMPLETE]")
                return  # Success!

            # Log failure details
            logging.warning(f"mpv exited with code {returncode}")
            logging.debug(f"mpv stderr: {stderr[:500]}")  # First 500 chars

        except FileNotFoundError:
            logging.error("mpv command not found")
            print("\nError: 'mpv' command not found. Is mpv installed?", file=sys.stderr)
            return
        except Exception as e:
            logging.error(f"Attempt {attempt_num} failed with exception: {e}")
            print(f"[PLAYBACK] Error: {e}")

        if attempt == 0:
            print(f"[PLAYBACK] Retrying in 5 seconds...")
            time.sleep(5)

    # Original URL failed, try alternatives
    logging.warning(f"Original stream failed after 2 attempts, searching for alternatives")
    print(f"\n[PLAYBACK] Original stream failed, searching for alternative providers...")

    if episode_num and original_start_time and channels_global and epg_global:
        alternatives = find_alternative_streams(
            channels_global,
            epg_global,
            show_title,
            episode_num,
            original_start_time,
            tolerance_minutes=5
        )

        for alt in alternatives:
            alt_channel = alt["channel"]
            alt_url = alt_channel.get("url", "")
            alt_provider = alt_channel.get("group-title", "Unknown Provider")
            alt_name = alt_channel.get("name", "Unknown")

            logging.info(f"Trying alternative: {alt_name} [{alt_provider}] - {alt_url}")
            print(f"[PLAYBACK] Trying alternative: {alt_name} [{alt_provider}]")

            try:
                mpv_cmd = ["mpv", "--stream-lavf-o=timeout=10000000", alt_url]
                proc = subprocess.Popen(
                    mpv_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                    returncode = proc.returncode
                except subprocess.TimeoutExpired:
                    stdout, stderr = proc.communicate()
                    returncode = proc.returncode

                # Log mpv output to dedicated log file
                log_mpv_output(
                    channel_name=f"{alt_name} [{alt_provider}] - {show_title} (alternative)",
                    command=mpv_cmd,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode
                )

                if returncode in (0, 4):
                    logging.info(f"Alternative stream succeeded: {alt_name} [{alt_provider}]")
                    print(f"\n[PLAYBACK COMPLETE]")
                    print(f"[PLAYBACK] Used alternative provider: {alt_provider}")
                    return  # Success!

                logging.warning(f"Alternative failed with exit code {returncode}")

            except Exception as e:
                logging.error(f"Alternative stream failed: {e}")

    # All streams failed, search for future reruns
    logging.warning(f"All streams failed, searching for future reruns")
    print(f"\n[PLAYBACK] All streams failed, searching for future reruns...")

    if episode_num and channels_global and epg_global:
        reruns = find_future_reruns(
            channels_global,
            epg_global,
            show_title,
            episode_num,
            hours_ahead=24
        )

        if reruns:
            next_rerun = reruns[0]
            next_channel = next_rerun["channel"]
            next_url = next_channel.get("url", "")
            next_provider = next_channel.get("group-title", "Unknown Provider")
            next_name = next_channel.get("name", "Unknown")
            next_start = next_rerun["start_time"]
            new_delay = max(0, int((next_start - datetime.now().astimezone()).total_seconds()))
            minutes_until = new_delay // 60

            logging.info(f"Found future rerun in {minutes_until} minutes on {next_name} [{next_provider}]")
            print(f"[PLAYBACK] Found future rerun:")
            print(f"  Channel: {next_name} [{next_provider}]")
            print(f"  Time: {next_rerun['time_status']}")

            # Schedule new playback
            new_task_id = int(time.time() * 1000)
            new_cancel_event = threading.Event()

            with SCHEDULED_TASKS_LOCK:
                SCHEDULED_TASKS.append({
                    "id": new_task_id,
                    "type": "playback",
                    "channel_name": next_name,
                    "provider": next_provider,
                    "show_title": show_title,
                    "scheduled_time": datetime.now().astimezone() + timedelta(seconds=new_delay),
                    "cancel_event": new_cancel_event,
                })

            thread = threading.Thread(
                target=scheduled_playback_task,
                args=(next_url, new_delay, next_name, show_title, next_provider, new_task_id, episode_num, next_start, new_cancel_event),
                daemon=True
            )
            thread.start()

            print(f"[PLAYBACK] Rescheduled for {next_rerun['time_status']}")
            logging.info(f"Rescheduled playback for {minutes_until} minutes from now")
            return

    # Complete failure
    logging.error(f"Playback failed completely: {show_title} - no alternatives or reruns found")
    print(f"\n[PLAYBACK FAILED] Could not play {show_title}")
    print(f"  All stream URLs failed and no future reruns found in the next 24 hours")


def scheduled_recording_task(channel_url: str, output_path: Path, delay_seconds: int, channel_name: str, show_title: str, provider: str = "Unknown Provider", extract_subs: bool = True, task_id: int = 0, episode_num: str = "", original_start_time: Optional[datetime] = None, duration_seconds: int = 0, cancel_event: Optional[threading.Event] = None):
    """
    Background task that waits then starts recording with retry logic.

    If the original stream fails:
    1. Retries once on the same URL
    2. Searches for alternative streams (same episode, different providers)
    3. If all fail, searches for future reruns and schedules the next one
    """
    global SCHEDULED_TASKS, channels_global, epg_global

    logging.info(f"Scheduled recording task created: {show_title} on {channel_name} [{provider}]")
    logging.info(f"  URL: {channel_url}")
    logging.info(f"  Delay: {delay_seconds} seconds ({delay_seconds // 60} minutes)")
    logging.info(f"  Episode: {episode_num}")

    print(f"\n[SCHEDULED] Recording will start in {delay_seconds // 60} minutes...")
    print(f"[SCHEDULED] Channel: {channel_name} [{provider}]")
    print(f"[SCHEDULED] Show: {show_title}")
    print(f"[SCHEDULED] Output: {output_path}")
    print(f"[SCHEDULED] Press Ctrl+C in mpv window to stop recording\n")

    # Wait for the scheduled time; fire a desktop notification 5 min before start
    _evt = cancel_event or threading.Event()
    _notify_wait = max(0, delay_seconds - 300)
    if _evt.wait(timeout=_notify_wait):
        logging.info(f"Recording task cancelled: {show_title}")
        return
    if _notify_wait > 0:
        send_desktop_notification("Term-TV", f"Recording in 5 min: {show_title}")
        logging.info(f"Desktop notification sent for recording: {show_title}")
    if _evt.wait(timeout=delay_seconds - _notify_wait):
        logging.info(f"Recording task cancelled after notification: {show_title}")
        return

    # Remove from scheduled tasks list
    with SCHEDULED_TASKS_LOCK:
        SCHEDULED_TASKS[:] = [t for t in SCHEDULED_TASKS if t.get("id") != task_id]

    # Start recording with retry logic
    print(f"\n[RECORDING STARTED] {channel_name} [{provider}] - {show_title}")
    print(f"[RECORDING] Output: {output_path}")
    logging.info(f"Starting recording: {show_title}")

    # Try original URL (with one retry)
    for attempt in range(2):
        attempt_num = attempt + 1
        logging.info(f"Attempt {attempt_num}/2: Trying original URL {channel_url}")
        print(f"[RECORDING] Attempt {attempt_num}/2: {channel_name} [{provider}]")

        try:
            mpv_cmd = [
                "mpv",
                f"--stream-record={output_path}",
                "--stream-lavf-o=timeout=10000000",
                "--sid=auto",  # Auto-select subtitles at start
                "--no-sub-visibility",  # Start with subs hidden (user can toggle with 'v')
            ]
            if duration_seconds:
                mpv_cmd.append(f"--length={duration_seconds}")
            mpv_cmd.append(channel_url)
            proc = subprocess.Popen(
                mpv_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            try:
                stdout, stderr = proc.communicate(timeout=10)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                stdout, stderr = proc.communicate()
                returncode = proc.returncode

            # Log mpv output to dedicated log file
            log_mpv_output(
                channel_name=f"{channel_name} [{provider}] - {show_title} (recording)",
                command=mpv_cmd,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode
            )

            # If mpv exited successfully or user quit (returncode 0 or 4), consider it success
            if returncode in (0, 4):
                logging.info(f"Recording completed successfully (exit code: {returncode})")
                print(f"\n[RECORDING COMPLETE] Saved to: {output_path}")

                # Extract subtitles if requested
                if extract_subs:
                    extract_subtitles_from_recording(output_path)
                return  # Success!

            # Log failure details
            logging.warning(f"mpv exited with code {returncode}")
            logging.debug(f"mpv stderr: {stderr[:500]}")  # First 500 chars

        except FileNotFoundError:
            logging.error("mpv command not found")
            print("\nError: 'mpv' command not found. Is mpv installed?", file=sys.stderr)
            return
        except Exception as e:
            logging.error(f"Attempt {attempt_num} failed with exception: {e}")
            print(f"[RECORDING] Error: {e}")

        if attempt == 0:
            print(f"[RECORDING] Retrying in 5 seconds...")
            time.sleep(5)

    # Original URL failed, try alternatives
    logging.warning(f"Original stream failed after 2 attempts, searching for alternatives")
    print(f"\n[RECORDING] Original stream failed, searching for alternative providers...")

    if episode_num and original_start_time and channels_global and epg_global:
        alternatives = find_alternative_streams(
            channels_global,
            epg_global,
            show_title,
            episode_num,
            original_start_time,
            tolerance_minutes=5
        )

        for alt in alternatives:
            alt_channel = alt["channel"]
            alt_url = alt_channel.get("url", "")
            alt_provider = alt_channel.get("group-title", "Unknown Provider")
            alt_name = alt_channel.get("name", "Unknown")

            logging.info(f"Trying alternative: {alt_name} [{alt_provider}] - {alt_url}")
            print(f"[RECORDING] Trying alternative: {alt_name} [{alt_provider}]")

            try:
                mpv_cmd = [
                    "mpv",
                    f"--stream-record={output_path}",
                    "--stream-lavf-o=timeout=10000000",
                    "--sid=auto",  # Auto-select subtitles at start
                    "--no-sub-visibility",  # Start with subs hidden (user can toggle with 'v')
                ]
                if duration_seconds:
                    mpv_cmd.append(f"--length={duration_seconds}")
                mpv_cmd.append(alt_url)
                proc = subprocess.Popen(
                    mpv_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                    returncode = proc.returncode
                except subprocess.TimeoutExpired:
                    stdout, stderr = proc.communicate()
                    returncode = proc.returncode

                # Log mpv output to dedicated log file
                log_mpv_output(
                    channel_name=f"{alt_name} [{alt_provider}] - {show_title} (recording alternative)",
                    command=mpv_cmd,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode
                )

                if returncode in (0, 4):
                    logging.info(f"Alternative stream succeeded: {alt_name} [{alt_provider}]")
                    print(f"\n[RECORDING COMPLETE] Saved to: {output_path}")
                    print(f"[RECORDING] Used alternative provider: {alt_provider}")

                    if extract_subs:
                        extract_subtitles_from_recording(output_path)
                    return  # Success!

                logging.warning(f"Alternative failed with exit code {returncode}")

            except Exception as e:
                logging.error(f"Alternative stream failed: {e}")

    # All streams failed, search for future reruns
    logging.warning(f"All streams failed, searching for future reruns")
    print(f"\n[RECORDING] All streams failed, searching for future reruns...")

    if episode_num and channels_global and epg_global:
        reruns = find_future_reruns(
            channels_global,
            epg_global,
            show_title,
            episode_num,
            hours_ahead=24
        )

        if reruns:
            next_rerun = reruns[0]
            next_channel = next_rerun["channel"]
            next_url = next_channel.get("url", "")
            next_provider = next_channel.get("group-title", "Unknown Provider")
            next_name = next_channel.get("name", "Unknown")
            next_start = next_rerun["start_time"]
            new_delay = max(0, int((next_start - datetime.now().astimezone()).total_seconds()))
            minutes_until = new_delay // 60

            logging.info(f"Found future rerun in {minutes_until} minutes on {next_name} [{next_provider}]")
            print(f"[RECORDING] Found future rerun:")
            print(f"  Channel: {next_name} [{next_provider}]")
            print(f"  Time: {next_rerun['time_status']}")

            # Schedule new recording
            new_task_id = int(time.time() * 1000)
            new_cancel_event = threading.Event()

            with SCHEDULED_TASKS_LOCK:
                SCHEDULED_TASKS.append({
                    "id": new_task_id,
                    "type": "recording",
                    "channel_name": next_name,
                    "provider": next_provider,
                    "show_title": show_title,
                    "scheduled_time": datetime.now().astimezone() + timedelta(seconds=new_delay),
                    "cancel_event": new_cancel_event,
                })

            thread = threading.Thread(
                target=scheduled_recording_task,
                args=(next_url, output_path, new_delay, next_name, show_title, next_provider, extract_subs, new_task_id, episode_num, next_start, duration_seconds, new_cancel_event),
                daemon=True
            )
            thread.start()

            print(f"[RECORDING] Rescheduled for {next_rerun['time_status']}")
            logging.info(f"Rescheduled recording for {minutes_until} minutes from now")
            return

    # Complete failure
    logging.error(f"Recording failed completely: {show_title} - no alternatives or reruns found")
    print(f"\n[RECORDING FAILED] Could not record {show_title}")
    print(f"  All stream URLs failed and no future reruns found in the next 24 hours")


def scheduled_reminder_task(show_title: str, start_time: datetime, channel_name: str, task_id: int, cancel_event: Optional[threading.Event] = None):
    """Background task: fires a desktop notification ~1 minute before a show starts."""
    logging.info(f"Reminder task created: {show_title} on {channel_name}")
    delay_seconds = max(0, int((start_time - datetime.now().astimezone()).total_seconds()) - 60)
    _evt = cancel_event or threading.Event()
    if _evt.wait(timeout=delay_seconds):
        logging.info(f"Reminder cancelled: {show_title}")
        return
    with SCHEDULED_TASKS_LOCK:
        SCHEDULED_TASKS[:] = [t for t in SCHEDULED_TASKS if t.get("id") != task_id]
    send_desktop_notification("Term-TV Reminder", f"Starting in ~1 min: {show_title}")
    print(f"\n[REMINDER] '{show_title}' starts in ~1 minute on {channel_name}")


# --- Playback & Selection Functions ---

def play_channel(channel: Channel, show_result: Optional[ShowResult] = None):
    """
    Launches the selected channel's URL in mpv and logs the watch with duration.
    Optionally records the stream if user chooses to.

    Args:
        channel: Channel dictionary
        show_result: Optional show result for better recording metadata
    """
    channel_name = channel.get('name', 'Unknown Channel')
    channel_url = channel.get('url', '')
    provider = channel.get('group-title', 'Unknown Provider')

    logging.info(f"play_channel called: {channel_name} [{provider}]")
    logging.info(f"  URL: {channel_url}")
    if show_result:
        logging.info(f"  Show: {show_result.get('title', 'Unknown')}")

    if not channel_url:
        logging.error(f"Channel has no URL: {channel_name}")
        print("Error: Channel has no URL.", file=sys.stderr)
        return

    # Ask if user wants to record
    print(f"\nSelected: {channel_name} [{provider}]")
    if show_result:
        print(f"Show: {show_result['title']}")

    # F6: show what's currently on / coming up for this channel
    if not show_result:
        tvg_id = channel.get("tvg-id", "")
        schedule = get_channel_schedule(epg_global, tvg_id, upcoming=3)
        if schedule:
            now = datetime.now().astimezone()
            print()
            for prog in schedule:
                st = prog.get("start_time")
                et = prog.get("stop_time")
                label = "NOW" if (st and et and st <= now < et) else (st.strftime("%I:%M %p") if st else "?")
                title = prog.get("title", "Unknown")
                ep = prog.get("episode_num", "")
                dur = f" ({int((et - st).total_seconds() / 60)}m)" if st and et else ""
                new_badge = " +++" if is_new_episode(prog.get("air_date", "")) else ""
                print(f"  {label:>10}: {title}{' (' + ep + ')' if ep else ''}{dur}{new_badge}")
            print()

    _note = get_channel_note(channel)
    if _note:
        print(f"Note: {_note}")

    print("Recording options:")
    print("  w: Watch only (no recording)")
    print("  r: Record while watching")
    print("  s: Schedule recording for later")
    print("  n: Add/edit note for this channel")
    print("  b: Back")

    record_choice = input("\nYour choice (default: w): ").strip().lower()

    if not record_choice:
        record_choice = 'w'

    if record_choice == 'b':
        return

    if record_choice == 'n':
        _current = get_channel_note(channel)
        if _current:
            print(f"Current note: {_current}")
        _new_note = input("Enter note (Enter to clear): ").strip()
        set_channel_note(channel, _new_note)
        print(f"  Note {'saved' if _new_note else 'cleared'}.")
        return

    # Handle scheduled recording
    if record_choice == 's':
        if not show_result:
            print("Error: Scheduled recording requires show information from search.", file=sys.stderr)
            return

        minutes_until = show_result.get("minutes_until", 0)
        if minutes_until < 0:
            print("Error: Cannot schedule recording for a show that's already playing.", file=sys.stderr)
            print("Use 'r' to record while watching instead.")
            return

        # Get show duration or ask user
        start_time = show_result.get("start_time")
        stop_time = show_result.get("stop_time")
        episode_num = show_result.get("episode_num", "")

        if start_time and stop_time:
            duration_minutes = int((stop_time - start_time).total_seconds() / 60)
            print(f"\nShow duration: {duration_minutes} minutes")
            print(f"Starts in: {minutes_until} minutes")

        # Generate filename
        ensure_recordings_dir()
        show_title = show_result.get("title", "Unknown")
        filename = get_safe_filename(channel_name, show_title)
        output_path = RECORDINGS_DIR / filename

        print(f"\nRecording will be saved to: {output_path}")
        duration_cap_input = input("Duration in minutes (Enter for unlimited): ").strip()
        rec_duration_seconds = int(duration_cap_input) * 60 if duration_cap_input and duration_cap_input.isdigit() else 0
        confirm = input("Schedule this recording? (y/n): ").strip().lower()

        if confirm != 'y':
            return

        # Start background thread for scheduled recording
        delay_seconds = minutes_until * 60

        # Generate unique task ID
        task_id = int(time.time() * 1000)  # Millisecond timestamp
        cancel_event = threading.Event()

        # Add to scheduled tasks list
        scheduled_time = datetime.now().astimezone() + timedelta(seconds=delay_seconds)
        with SCHEDULED_TASKS_LOCK:
            SCHEDULED_TASKS.append({
                "id": task_id,
                "type": "recording",
                "channel_name": channel_name,
                "provider": provider,
                "show_title": show_title,
                "scheduled_time": scheduled_time,
                "cancel_event": cancel_event,
            })

        thread = threading.Thread(
            target=scheduled_recording_task,
            args=(channel_url, output_path, delay_seconds, channel_name, show_title, provider, True, task_id, episode_num, start_time, rec_duration_seconds, cancel_event),
            daemon=True
        )
        thread.start()

        print(f"✓ Recording scheduled!")
        print(f"  Will start in {minutes_until} minutes")
        print(f"  Keep this terminal open until recording starts")
        print(f"  ⚠️  Subtitles pre-loaded - do NOT switch tracks or recording will stop!\n")
        return

    # Handle watch-only or record-while-watching
    mpv_args = ["mpv"]

    if record_choice == 'r':
        ensure_recordings_dir()

        # Generate default filename
        show_title = show_result.get("title", "") if show_result else ""
        default_filename = get_safe_filename(channel_name, show_title)

        print(f"\nDefault filename: {default_filename}")
        custom_name = input("Press Enter to use default, or enter custom name (.mkv will be added): ").strip()

        if custom_name:
            if not custom_name.endswith('.mkv'):
                custom_name += '.mkv'
            # Strip any directory components to prevent path traversal
            filename = Path(custom_name).name
        else:
            filename = default_filename

        output_path = RECORDINGS_DIR / filename
        mpv_args.append(f"--stream-record={output_path}")
        mpv_args.append("--sid=auto")  # Auto-select subtitles at start
        mpv_args.append("--no-sub-visibility")  # Start with subs hidden (toggle with 'v')

        duration_cap = input("Duration in minutes (Enter for unlimited): ").strip()
        if duration_cap and duration_cap.isdigit():
            mpv_args.append(f"--length={int(duration_cap) * 60}")

        print(f"\n📹 Recording to: {output_path}")
        print("⚠️  WARNING: Do NOT change subtitle tracks during recording or it will stop!")
        print("   Subtitles are pre-loaded - press 'v' to show/hide (safe)")
        print("   Press 'q' in mpv window to stop recording and playback")

    if record_choice == 'w':
        mpv_args.append("--save-position-on-quit")
        _sleep_input = input("Sleep timer? (minutes, Enter for none): ").strip()
        if _sleep_input and _sleep_input.isdigit() and int(_sleep_input) > 0:
            mpv_args.append(f"--length={int(_sleep_input) * 60}")

    mpv_args.append(channel_url)

    print(f"\nLaunching: {channel_name} [{provider}]")
    logging.info(f"Launching mpv with args: {mpv_args}")

    # Debug: Show command being run
    print(f"Command: {' '.join(mpv_args[:2])}..." if len(mpv_args) > 2 else f"Command: {' '.join(mpv_args)}")
    print(f"Starting mpv... (if it hangs, the stream may be down - press Ctrl+C to cancel)")

    # Track start time
    start_time = datetime.now()

    try:
        # Run mpv (blocks until player exits or fails to connect)
        # If mpv hangs connecting to stream, press Ctrl+C to cancel
        logging.info("Starting mpv subprocess...")
        result = run_mpv_with_logging(mpv_args, f"{channel_name} [{provider}]")
        logging.info(f"mpv exited with return code: {result.returncode}")

        # Check if mpv exited with an error
        if result.returncode != 0 and result.returncode != -2:  # -2 is Ctrl+C
            logging.warning(f"mpv exited with error code {result.returncode}")
            print(f"\nmpv exited with code {result.returncode}", file=sys.stderr)

        if record_choice == 'r':
            print(f"\n✓ Recording saved to: {output_path}")

            # Extract subtitles from recording
            extract_subtitles_from_recording(output_path)

    except FileNotFoundError:
        logging.error("mpv command not found in PATH")
        print("\nError: 'mpv' command not found. Is mpv installed and in your system's PATH?", file=sys.stderr)
        return
    except KeyboardInterrupt:
        logging.info("Playback interrupted by user (Ctrl+C)")
        print("\nPlayback interrupted by user.")
        if record_choice == 'r':
            print(f"Recording saved to: {output_path}")

            # Extract subtitles from recording
            extract_subtitles_from_recording(output_path)
    except Exception as e:
        logging.error(f"Exception launching mpv: {e}", exc_info=True)
        print(f"\nError launching mpv: {e}", file=sys.stderr)
        return
    finally:
        # Log watch history for both watch and record sessions
        if record_choice in ('w', 'r'):
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logging.info(f"Session duration: {duration:.1f} seconds (mode={record_choice})")

            if duration >= 2:
                log_channel_watch(channel, int(duration))

                if duration >= 120:
                    logging.info(f"Session logged: {int(duration // 60)} minutes")
                    print(f"\nWatched for {int(duration // 60)} minutes - session logged!")
                else:
                    logging.debug(f"Session too short to log: {int(duration)} seconds")
                    print(f"\nWatched for {int(duration)} seconds (need 2 min to log frequency)")


def select_from_show_results(results: List[ShowResult]) -> Optional[ShowResult]:
    """Displays show results and lets user select one."""
    print(f"\nFound {len(results)} result(s):")
    print("-" * 80)

    for i, result in enumerate(results, 1):
        channel_name = result["channel"].get("name", "Unknown")
        title = result["title"]
        time_status = result["time_status"]
        start_time = result["start_time"].strftime("%I:%M %p") if result.get("start_time") else "?"
        is_playing = result.get("is_playing_now", False)
        is_new = result.get("is_new", False)
        episode_num = result.get("episode_num", "")
        subtitle = result.get("subtitle", "")

        # Build the display string
        display_parts = [f"{i}. [{time_status:>12}] {start_time} - {title}"]

        if episode_num:
            display_parts.append(f" ({episode_num})")

        if is_new:
            display_parts.append(" +++")

        if is_playing:
            display_parts.append(" ◄◄◄")

        stop_time = result.get("stop_time")
        if stop_time and result.get("start_time"):
            end_str = stop_time.strftime("%I:%M %p")
            dur = int((stop_time - result["start_time"]).total_seconds() / 60)
            display_parts.append(f"  → {end_str} ({dur}m)")

        print("".join(display_parts))

        # Get provider/category
        provider = result["channel"].get("group-title", "Unknown Provider")

        if subtitle:
            print(f"   \"{subtitle}\" - {channel_name} [{provider}]")
        else:
            print(f"   Channel: {channel_name} [{provider}]")
        print()

    selection = input(
        f"Select show (1-{len(results)}, 'b' for back, or '1 3 5' to schedule multiple): "
    ).strip()

    if selection.lower() == 'b':
        return None

    # Multi-select: space-separated numbers (F3)
    parts = selection.split()
    if len(parts) > 1:
        chosen = []
        for p in parts:
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(results):
                    chosen.append(results[idx])
        return chosen if chosen else None

    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(results):
            return results[index]

    print("Invalid selection.", file=sys.stderr)
    return None


def select_from_channel_list(results: List[Channel], epg: EpgData) -> Optional[Channel]:
    """Displays channel results and lets user select one."""
    print(f"\nFound {len(results)} channel(s):")
    print("-" * 80)

    for i, channel in enumerate(results, 1):
        channel_name = channel.get("name", "Unknown")
        tvg_id = channel.get("tvg-id")
        provider = channel.get("group-title", "Unknown Provider")

        print(f"{i}. {channel_name} [{provider}]")

        # Show current program if available
        if tvg_id and tvg_id in epg:
            now = datetime.now().astimezone()
            for program in epg[tvg_id][:5]:
                start_time = program.get("start_time")
                stop_time = program.get("stop_time")

                if start_time:
                    time_str = start_time.strftime("%I:%M %p")

                    if stop_time and start_time <= now < stop_time:
                        status = " [NOW PLAYING]"
                    else:
                        status = ""

                    title = program.get("title", "Unknown")
                    print(f"   {time_str}{status}: {title}")
        print()

    selection = input(f"Select channel (1-{len(results)}, or 'b' for back): ").strip()

    if selection.lower() == 'b':
        return None

    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(results):
            return results[index]

    print("Invalid selection.", file=sys.stderr)
    return None


# --- VPN & Tool Management ---

def find_openvpn_executable(config_exe: str = "") -> Optional[str]:
    """
    Locate the OpenVPN executable.
    Checks: config-specified path → PATH → common install locations.
    """
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
        candidates = [
            "/usr/local/sbin/openvpn",
            "/opt/homebrew/sbin/openvpn",
            "/usr/sbin/openvpn",
        ]
    else:  # Linux
        candidates = [
            "/usr/sbin/openvpn",
            "/usr/local/sbin/openvpn",
        ]

    for path in candidates:
        if Path(path).exists():
            return path

    return None


def check_admin_privileges() -> bool:
    """Return True if the process has admin/root privileges (required for OpenVPN)."""
    try:
        if platform.system() == "Windows":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        else:
            return os.getuid() == 0
    except Exception:
        return False


def _try_install_mpv() -> bool:
    """
    Attempt to install mpv using the system package manager.
    Returns True if installation succeeded.
    """
    system = platform.system()
    print("Attempting to install mpv automatically...")

    try:
        if system == "Windows":
            if shutil.which("winget"):
                result = subprocess.run(
                    ["winget", "install", "--id", "mpv.net",
                     "--silent", "--accept-package-agreements", "--accept-source-agreements"],
                    check=False, timeout=120
                )
                return result.returncode == 0

        elif system == "Linux":
            for pkg_mgr, cmd in [
                ("apt-get", ["sudo", "apt-get", "install", "-y", "mpv"]),
                ("dnf",     ["sudo", "dnf",     "install", "-y", "mpv"]),
                ("pacman",  ["sudo", "pacman",  "-S", "--noconfirm", "mpv"]),
                ("zypper",  ["sudo", "zypper",  "install", "-y", "mpv"]),
            ]:
                if shutil.which(pkg_mgr):
                    result = subprocess.run(cmd, check=False, timeout=120)
                    if result.returncode == 0:
                        return True

        elif system == "Darwin":
            if shutil.which("brew"):
                result = subprocess.run(["brew", "install", "mpv"], check=False, timeout=180)
                return result.returncode == 0

    except (subprocess.TimeoutExpired, Exception) as e:
        logging.warning(f"mpv auto-install failed: {e}")

    return False


def check_required_tools():
    """
    Verify required external tools are available.
    Attempts to auto-install mpv if missing.
    Prints a warning for optional ffmpeg.
    """
    system = platform.system()

    # --- mpv (required) ---
    if not shutil.which("mpv"):
        print("\n⚠  mpv not found — it is required for playback.")
        installed = _try_install_mpv()
        if installed and shutil.which("mpv"):
            print("✓ mpv installed successfully.\n")
        else:
            print("\nCould not auto-install mpv. Please install it manually:", file=sys.stderr)
            if system == "Windows":
                print("  winget install mpv", file=sys.stderr)
                print("  or download from: https://mpv.io/installation/", file=sys.stderr)
            elif system == "Linux":
                print("  sudo apt-get install mpv   (Debian/Ubuntu)", file=sys.stderr)
                print("  sudo dnf install mpv        (Fedora)", file=sys.stderr)
                print("  sudo pacman -S mpv           (Arch)", file=sys.stderr)
            elif system == "Darwin":
                print("  brew install mpv", file=sys.stderr)
            sys.exit(1)

    # --- ffmpeg (optional, subtitle extraction) ---
    if not shutil.which("ffmpeg"):
        logging.info("ffmpeg not found — subtitle extraction disabled")
        print("Note: ffmpeg not found. Subtitle extraction from recordings will be disabled.")
        print("      Install ffmpeg to enable: https://ffmpeg.org/download.html\n")


# Global handle to the running OpenVPN process
_vpn_process: Optional[subprocess.Popen] = None
# Stored so the VPN can be reconnected from the main menu
_vpn_exe: Optional[str] = None
_vpn_config_file: Optional[str] = None
_vpn_expected_ip: Optional[str] = None
# Keeps the Windows console-ctrl callback alive (must not be garbage-collected)
_win_console_handler_ref = None


def connect_vpn(openvpn_exe: str, config_file: str, expected_ip: Optional[str] = None) -> bool:
    """
    Launch OpenVPN with the given config and wait for the VPN connection to establish.
    Polls the public IP every 2 seconds for up to 40 seconds.
    Returns True on success, False on failure.
    """
    global _vpn_process, _vpn_exe, _vpn_config_file, _vpn_expected_ip
    _vpn_exe = openvpn_exe
    _vpn_config_file = config_file
    _vpn_expected_ip = expected_ip

    config_path = Path(config_file)
    if not config_path.exists():
        print(f"Error: OpenVPN config file not found: {config_file}", file=sys.stderr)
        return False

    print(f"\nConnecting to VPN using: {config_path.name}")
    logging.info(f"Launching OpenVPN: {openvpn_exe} --config {config_file}")

    try:
        vpn_log_file = open("openvpn.log", "a", encoding="utf-8", errors="replace")
        vpn_log_file.write(f"\n{'='*60}\n[{datetime.now()}] OpenVPN started\n{'='*60}\n")
        vpn_log_file.flush()

        popen_kwargs: dict = {
            "stdout": vpn_log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _vpn_process = subprocess.Popen(
            [openvpn_exe, "--config", str(config_path)],
            **popen_kwargs,
        )
    except FileNotFoundError:
        print(f"Error: OpenVPN not found at: {openvpn_exe}", file=sys.stderr)
        return False
    except PermissionError:
        print("Error: Permission denied — OpenVPN requires elevated privileges.", file=sys.stderr)
        if platform.system() == "Windows":
            print("  Right-click the script and choose 'Run as Administrator'.", file=sys.stderr)
        else:
            print("  Run with: sudo python Term-TV-VPN.py", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error starting OpenVPN: {e}", file=sys.stderr)
        return False

    # Poll until connected or timeout
    print("Waiting for VPN connection", end="", flush=True)
    start = time.time()
    timeout = 40
    connected = False

    while time.time() - start < timeout:
        print(".", end="", flush=True)
        time.sleep(2)

        # Check if the process died early
        if _vpn_process.poll() is not None:
            print(f"\nError: OpenVPN exited unexpectedly (code {_vpn_process.returncode}).", file=sys.stderr)
            print("Check openvpn.log for details.", file=sys.stderr)
            return False

        current_ip = get_public_ip()

        if expected_ip:
            if current_ip == expected_ip:
                print(f"\n✓ VPN Connected! (IP: {current_ip})")
                print("  VPN will auto-disconnect when this window is closed.")
                logging.info(f"VPN connected — IP: {current_ip}")
                connected = True
                break
        else:
            # No IP to verify against — assume connected after process has been stable 8s
            if time.time() - start >= 8:
                print(f"\n✓ VPN process running (current IP: {current_ip or 'unknown'})")
                print("  Tip: Set 'vpn_ip' in config.json to verify the VPN IP automatically.")
                print("  VPN will auto-disconnect when this window is closed.")
                logging.info("VPN assumed connected (no expected_ip configured)")
                connected = True
                break

    if not connected:
        print(f"\n⚠  VPN did not confirm connection within {timeout}s.", file=sys.stderr)
        if expected_ip:
            current_ip = get_public_ip()
            print(f"  Expected IP : {expected_ip}", file=sys.stderr)
            print(f"  Current IP  : {current_ip or 'unknown'}", file=sys.stderr)
        print("Check openvpn.log for details.", file=sys.stderr)
        while True:
            choice = input("Continue anyway? (y/n): ").strip().lower()
            if choice == 'y':
                return True
            elif choice == 'n':
                return False

    return True


def disconnect_vpn():
    """Gracefully terminate the OpenVPN process. Called automatically on exit via atexit."""
    global _vpn_process
    if _vpn_process is None or _vpn_process.poll() is not None:
        return
    logging.info("Disconnecting VPN on exit...")
    print("\nDisconnecting VPN...")
    try:
        _vpn_process.terminate()
        _vpn_process.wait(timeout=5)
        print("✓ VPN disconnected.")
        logging.info("VPN disconnected cleanly.")
    except subprocess.TimeoutExpired:
        _vpn_process.kill()
        logging.warning("VPN process killed (did not exit cleanly).")
    except Exception as e:
        logging.warning(f"Error disconnecting VPN: {e}")


# Known VPN processes that may conflict with our own OpenVPN tunnel.
# Key: lowercase exe/process name.  Value: human-friendly label.
# Omits permanent background services (openvpnserv.exe, openvpnserv2.exe)
# that are part of the OpenVPN installation and are harmless.
_KNOWN_VPN_PROCESSES: Dict[str, str] = {
    # OpenVPN clients / GUIs
    "openvpn.exe":          "OpenVPN tunnel",
    "openvpn-gui.exe":      "OpenVPN GUI",
    "viscosity.exe":        "Viscosity (OpenVPN client)",
    "privatetunnel.exe":    "Private Tunnel",
    # WireGuard
    "wireguard.exe":        "WireGuard",
    # NordVPN
    "nordvpn.exe":          "NordVPN",
    "nordvpnd.exe":         "NordVPN daemon",
    # ExpressVPN
    "expressvpn.exe":       "ExpressVPN",
    # Mullvad
    "mullvad-gui.exe":      "Mullvad VPN GUI",
    "mullvad-daemon.exe":   "Mullvad VPN daemon",
    "mullvad.exe":          "Mullvad VPN",
    # ProtonVPN
    "protonvpn.exe":        "ProtonVPN",
    "protonvpn-app.exe":    "ProtonVPN",
    # Surfshark
    "surfshark.exe":        "Surfshark",
    # CyberGhost
    "cyberghost.exe":       "CyberGhost",
    "cyberghostservice.exe":"CyberGhost service",
    # IPVanish
    "ipvanish.exe":         "IPVanish",
    # Windscribe
    "windscribe.exe":       "Windscribe",
    # TunnelBear
    "tunnelbear.exe":       "TunnelBear",
    # PureVPN
    "purevpn.exe":          "PureVPN",
    # Hotspot Shield
    "hotspotshield.exe":    "Hotspot Shield",
    # VPN Unlimited / KeepSolid
    "vpnunlimited.exe":     "VPN Unlimited",
    # Cisco AnyConnect
    "vpnui.exe":            "Cisco AnyConnect UI",
    "vpnagent.exe":         "Cisco AnyConnect agent",
    # OpenConnect (AnyConnect-compatible)
    "openconnect.exe":      "OpenConnect",
    # FortiClient
    "forticlient.exe":      "FortiClient VPN",
    # --- Linux / macOS process names (no extension) ---
    "openvpn":              "OpenVPN tunnel",
    "nordvpnd":             "NordVPN daemon",
    "expressvpnd":          "ExpressVPN daemon",
    "mullvad-daemon":       "Mullvad VPN daemon",
    "protonvpn":            "ProtonVPN",
    "wireguard":            "WireGuard",
    "wg-quick":             "WireGuard (wg-quick)",
    "openconnect":          "OpenConnect",
}


def detect_conflicting_vpn_processes() -> List[Dict[str, str]]:
    """
    Scans running processes for known VPN software that may conflict with our
    OpenVPN tunnel.

    Returns:
        List of dicts with keys: 'pid', 'name', 'label'
    """
    conflicts: List[Dict[str, str]] = []

    try:
        if platform.system() == "Windows":
            # tasklist /FO CSV /NH  →  "Image Name","PID","Session Name",...
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, check=False
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) < 2:
                    continue
                proc_name = parts[0].lower()
                pid = parts[1]
                if proc_name in _KNOWN_VPN_PROCESSES:
                    conflicts.append({
                        "pid": pid,
                        "name": parts[0],
                        "label": _KNOWN_VPN_PROCESSES[proc_name],
                    })
        else:
            # ps -eo pid,comm  →  works on Linux and macOS
            result = subprocess.run(
                ["ps", "-eo", "pid,comm"],
                capture_output=True, text=True, check=False
            )
            for line in result.stdout.splitlines()[1:]:  # skip header
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid, proc_name = parts[0], parts[1].strip().lower()
                if proc_name in _KNOWN_VPN_PROCESSES:
                    conflicts.append({
                        "pid": pid,
                        "name": parts[1].strip(),
                        "label": _KNOWN_VPN_PROCESSES[proc_name],
                    })
    except Exception as e:
        logging.warning(f"VPN conflict scan failed: {e}")

    return conflicts


def check_and_resolve_vpn_conflicts():
    """
    Detects conflicting VPN processes, lists them, and optionally kills them
    before our own OpenVPN tunnel is started.
    """
    conflicts = detect_conflicting_vpn_processes()
    if not conflicts:
        return

    print("\n" + "="*80)
    print("WARNING: OTHER VPN SOFTWARE DETECTED")
    print("="*80)
    print("The following VPN processes are already running and may conflict")
    print("with the Term-TV VPN connection:\n")

    for c in conflicts:
        print(f"  PID {c['pid']:>6}  {c['name']:<30}  {c['label']}")

    print()
    print("  k: Kill all listed processes and continue")
    print("  i: Ignore and connect anyway")
    print("  q: Quit")

    while True:
        choice = input("\nYour choice: ").strip().lower()

        if choice == 'k':
            print()
            for c in conflicts:
                try:
                    if platform.system() == "Windows":
                        subprocess.run(
                            ["taskkill", "/PID", c["pid"], "/F"],
                            capture_output=True, check=False
                        )
                    else:
                        subprocess.run(
                            ["kill", "-9", c["pid"]],
                            capture_output=True, check=False
                        )
                    print(f"  ✓ Killed {c['name']} (PID {c['pid']})")
                    logging.info(f"Killed conflicting VPN process: {c['name']} PID {c['pid']}")
                except Exception as e:
                    print(f"  ✗ Could not kill {c['name']} (PID {c['pid']}): {e}", file=sys.stderr)
            print()
            break

        elif choice == 'i':
            print("Proceeding with existing VPN processes running.")
            break

        elif choice in ('q', 'quit', 'exit'):
            print("Exiting.")
            sys.exit(0)

        else:
            print("Invalid choice. Enter k, i, or q.")


def vpn_is_connected() -> bool:
    """Returns True if the OpenVPN process is currently running."""
    return _vpn_process is not None and _vpn_process.poll() is None


def toggle_vpn_menu():
    """
    Interactive VPN toggle shown when the user types 'vpn' at the main menu.
    Lets the user disconnect a live connection or reconnect after disconnecting.
    """
    print("\n" + "="*80)
    print("VPN STATUS")
    print("="*80)

    connected = vpn_is_connected()
    current_ip = get_public_ip()

    if connected:
        print(f"  Status : Connected")
        print(f"  Config : {_vpn_config_file}")
        print(f"  Current IP : {current_ip or 'unknown'}")
        print()
        print("  d: Disconnect VPN")
        print("  b: Back")
        choice = input("\nYour choice: ").strip().lower()
        if choice == 'd':
            disconnect_vpn()
            print("VPN disconnected. You can reconnect with 'vpn' from the main menu.")
    else:
        print(f"  Status : Disconnected")
        print(f"  Current IP : {current_ip or 'unknown'}")
        if _vpn_exe and _vpn_config_file:
            print(f"  Config : {_vpn_config_file}")
            print()
            print("  r: Reconnect VPN")
            print("  b: Back")
            choice = input("\nYour choice: ").strip().lower()
            if choice == 'r':
                connect_vpn(_vpn_exe, _vpn_config_file, _vpn_expected_ip)
        else:
            print()
            print("  No VPN config available to reconnect.")
            input("  Press Enter to go back...")


def _register_vpn_signal_handlers():
    """
    Register signal and console-control handlers so that OpenVPN is always
    terminated — even when the terminal window is closed with the X button.

    On Windows the OS fires CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT /
    CTRL_SHUTDOWN_EVENT which bypass Python's atexit machinery.  We install a
    SetConsoleCtrlHandler callback to catch those events.
    """
    import signal

    def _on_signal(signum, frame):
        disconnect_vpn()
        sys.exit(0)

    # Ctrl+C and SIGTERM (covers most normal termination paths)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (OSError, AttributeError):
        pass  # SIGTERM not available on all Windows builds

    if platform.system() == "Windows":
        import ctypes

        # Keep a module-level reference so the callback isn't garbage-collected
        global _win_console_handler_ref

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        def _console_ctrl_handler(event):
            # Events: 0=CTRL_C, 1=CTRL_BREAK, 2=CTRL_CLOSE, 5=CTRL_LOGOFF, 6=CTRL_SHUTDOWN
            if event in (2, 5, 6):
                disconnect_vpn()
            return False  # Let Windows continue with default handling

        _win_console_handler_ref = _console_ctrl_handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler, True)


# --- Main Application ---

def main():
    """Main application entry point."""
    global channels_global, epg_global

    parser = argparse.ArgumentParser(description="Term-TV VPN: CLI IPTV player with OpenVPN")
    parser.add_argument("--playlist", type=int, metavar="N", help="Auto-select playlist N (1-based, skips menu)")
    parser.add_argument("--skip-vpn", action="store_true", help="Skip VPN connection/prompt at startup")
    parser.add_argument("--no-epg", action="store_true", help="Skip EPG loading (channel browsing only)")
    parser.add_argument("--search", metavar="QUERY", help="Jump directly to show search on startup")
    parser.add_argument("--record", metavar="QUERY", help="Headless: search for show and record immediately")
    parser.add_argument("--record-channel", metavar="NAME", help="Headless: record a channel by name")
    parser.add_argument("--duration", type=int, metavar="MINUTES", help="Duration for headless recording (minutes)")
    args = parser.parse_args()

    # --- Logging Setup ---
    setup_logging()
    logging.info("Starting main application")

    # --- Register cleanup on exit ---
    atexit.register(clean_old_cache_files)
    atexit.register(archive_mpv_log)
    logging.debug("Registered cache cleanup and MPV log archiving on exit")

    # --- Tool Checks (mpv required, ffmpeg optional) ---
    check_required_tools()

    # --- Configuration Loading ---
    logging.info(f"Loading configuration from {CONFIG_FILE}")
    if not CONFIG_FILE.exists():
        logging.error(f"Configuration file not found: {CONFIG_FILE}")
        print(f"Error: Configuration file '{CONFIG_FILE}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: '{CONFIG_FILE}' contains invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    playlists = config.get("playlists", [])
    if not playlists:
        print("Error: No playlists defined in config.json.", file=sys.stderr)
        sys.exit(1)

    # Get expected VPN IP from config (optional)
    expected_vpn_ip = config.get("vpn_ip")

    # --- Playlist Selection ---
    if len(playlists) == 1:
        # Auto-select if only one playlist
        chosen_playlist = playlists[0]
        print(f"Loading: {chosen_playlist['name']}")
    elif args.playlist is not None:
        index = args.playlist - 1
        if not (0 <= index < len(playlists)):
            print(f"Error: --playlist {args.playlist} is out of range (1-{len(playlists)}).", file=sys.stderr)
            sys.exit(1)
        chosen_playlist = playlists[index]
        print(f"Loading: {chosen_playlist['name']}")
    else:
        # Show selection menu for multiple playlists
        print("Available Playlists:")
        for i, playlist in enumerate(playlists, 1):
            print(f"{i}. {playlist['name']}")

        # Use countdown input with 15 second timeout, auto-select playlist 1
        selection = input_with_countdown(
            prompt=f"Select Playlist (1-{len(playlists)}): ",
            timeout=15,
            default="1"
        ).strip()

        if not selection:
            # Empty input after timeout - use default
            selection = "1"

        if not selection.isdigit():
            print("Invalid selection.", file=sys.stderr)
            return

        index = int(selection) - 1
        if not (0 <= index < len(playlists)):
            print("Invalid selection.", file=sys.stderr)
            return

        chosen_playlist = playlists[index]
        print(f"\nLoading: {chosen_playlist['name']}")

    # --- VPN Connection ---
    openvpn_cfg = config.get("openvpn", {})
    vpn_enabled = openvpn_cfg.get("enabled", False)
    auto_connect = openvpn_cfg.get("auto_connect", False)

    if args.skip_vpn:
        pass  # skip all VPN logic
    elif vpn_enabled and auto_connect:
        ovpn_file = openvpn_cfg.get("config_file", "")
        config_exe = openvpn_cfg.get("executable", "")

        if not ovpn_file:
            print("Error: 'openvpn.config_file' is not set in config.json.", file=sys.stderr)
            sys.exit(1)

        # Find OpenVPN executable
        openvpn_exe = find_openvpn_executable(config_exe)
        if not openvpn_exe:
            print("Error: OpenVPN executable not found.", file=sys.stderr)
            print("Install OpenVPN or set 'openvpn.executable' in config.json.", file=sys.stderr)
            if platform.system() == "Windows":
                print("  Download from: https://openvpn.net/community-downloads/", file=sys.stderr)
                print("  Or run: winget install OpenVPN.OpenVPN", file=sys.stderr)
            elif platform.system() == "Linux":
                print("  sudo apt-get install openvpn   (Debian/Ubuntu)", file=sys.stderr)
                print("  sudo dnf install openvpn        (Fedora)", file=sys.stderr)
            elif platform.system() == "Darwin":
                print("  brew install openvpn", file=sys.stderr)
            sys.exit(1)

        # Warn if not running with elevated privileges
        if not check_admin_privileges():
            print("⚠  Warning: Not running with administrator/root privileges.")
            if platform.system() == "Windows":
                print("   OpenVPN may fail — please right-click and 'Run as Administrator'.")
            else:
                print("   OpenVPN may fail — please run with: sudo python Term-TV-VPN.py")

        # Check for conflicting VPN software before connecting
        check_and_resolve_vpn_conflicts()

        # Connect
        if not connect_vpn(openvpn_exe, ovpn_file, expected_vpn_ip):
            sys.exit(1)

        # Ensure VPN disconnects on clean exit (atexit) AND on window-close / signals
        atexit.register(disconnect_vpn)
        _register_vpn_signal_handlers()

    elif not args.skip_vpn:
        # Auto-connect disabled — fall back to manual prompt
        if not check_vpn_status(expected_vpn_ip):
            sys.exit(0)

    # --- Data Loading ---
    logging.info(f"Loading M3U from {chosen_playlist['m3u_url']}")
    channels = load_m3u_cached(chosen_playlist["m3u_url"])
    if not channels:
        logging.error("Failed to load channels from M3U")
        print("Could not load any channels. Exiting.", file=sys.stderr)
        sys.exit(1)
    logging.info(f"Loaded {len(channels)} channels")
    print(f"Loaded {len(channels)} channels.")

    # Store in global for scheduled tasks
    channels_global = channels

    epg = {}
    if args.no_epg:
        print("EPG skipped (--no-epg) — show search unavailable, channel browsing only.")
    elif chosen_playlist.get("epg_url"):
        print("\nLoading EPG data...")
        logging.info(f"Loading EPG from {chosen_playlist['epg_url']}")
        epg = load_epg(chosen_playlist["epg_url"])
        if epg:
            logging.info(f"Loaded EPG for {len(epg)} channels")
            print(f"✓ Loaded EPG for {len(epg)} channels.")
        else:
            logging.warning("EPG data unavailable")
            print("⚠ EPG unavailable - continuing without program guide data.")
            print("  (Show search and EPG features will be limited)\n")

    # Store in global for scheduled tasks
    epg_global = epg

    # If --search was passed, run the search once before entering the loop
    _startup_search = args.search

    # --- F7: Headless recording mode (--record / --record-channel) ---
    if args.record or args.record_channel:
        ensure_recordings_dir()
        if args.record:
            _h_results = search_shows_in_timeframe(channels, epg, args.record, hours_ahead=3)
            if not _h_results:
                print(f"No show found matching '{args.record}'.", file=sys.stderr)
                sys.exit(1)
            _h_result = next((r for r in _h_results if r.get("is_playing_now")), _h_results[0])
            _h_channel = _h_result["channel"]
            _h_url = _h_channel.get("url", "")
            _h_filename = get_safe_filename(_h_channel.get("name", ""), _h_result.get("title", ""))
            print(f"Recording: {_h_result['title']} on {_h_channel.get('name', '?')}")
        else:
            _h_matches = search_channels(channels, args.record_channel)
            if not _h_matches:
                print(f"No channel found matching '{args.record_channel}'.", file=sys.stderr)
                sys.exit(1)
            _h_channel = _h_matches[0]
            _h_url = _h_channel.get("url", "")
            _h_filename = get_safe_filename(_h_channel.get("name", ""))
            print(f"Recording: {_h_channel.get('name', '?')}")
        _h_path = RECORDINGS_DIR / _h_filename
        print(f"Output: {_h_path}")
        _h_dur = (args.duration or 0) * 60
        _h_mpv = ["mpv", f"--stream-record={_h_path}", "--sid=auto", "--no-sub-visibility"]
        if _h_dur:
            _h_mpv.append(f"--length={_h_dur}")
        _h_mpv.append(_h_url)
        try:
            run_mpv_with_logging(_h_mpv, _h_channel.get("name", ""))
            extract_subtitles_from_recording(_h_path)
        except KeyboardInterrupt:
            print("\nRecording stopped.")
            extract_subtitles_from_recording(_h_path)
        sys.exit(0)

    # --- Main Interaction Loop ---
    while True:
        # Display favorites
        favorites = get_favorite_channels(channels, epg)
        display_favorites(favorites, start_index=1)

        # Display frequently watched channels
        fav_count = len(favorites)
        frequent = get_frequent_channels(channels, epg)
        display_frequent_channels(frequent)

        # Display currently-airing matches from recent search history
        search_now_playing = get_search_history_now_playing(channels, epg)
        display_search_history_now_playing(
            search_now_playing, start_index=fav_count + len(frequent) + 1
        )

        # Display scheduled tasks
        display_scheduled_tasks()

        print("\nOptions:")
        if favorites:
            print(f"  1-{fav_count}: Watch favorite")
        if frequent:
            f_start = fav_count + 1
            f_end   = fav_count + len(frequent)
            print(f"  {f_start if f_start == f_end else str(f_start) + '-' + str(f_end)}: Watch frequent channel")
        if search_now_playing:
            s_start = fav_count + len(frequent) + 1
            s_end   = s_start + len(search_now_playing) - 1
            label   = str(s_start) if s_start == s_end else f"{s_start}-{s_end}"
            print(f"  {label}: Watch from recent searches")
        if not args.no_epg:
            print("  s: Search for show/movie")
        print("  c: Search for channel")
        print("  g: Browse by channel group")
        print("  fav: Manage favorites")
        print("  notes: Manage channel notes")
        print("  rec: Browse and play recordings")
        print("  export: Export watch history to CSV")
        print("  hc: Check channel health")
        if not args.no_epg:
            print("  epg: Refresh EPG data")
        print("  cfg: Reload config")
        if len(playlists) > 1:
            print("  pl: Switch playlist")
        if SCHEDULED_TASKS:
            print("  t: Manage scheduled tasks")
        vpn_label = "Connected" if vpn_is_connected() else "Disconnected"
        print(f"  vpn: VPN status/toggle [{vpn_label}]")
        print("  quit: Exit")

        if _startup_search:
            choice = 's'
        else:
            default_choice = 'c' if args.no_epg else 's'
            choice = input(f"\nYour choice (default: {default_choice}): ").strip().lower()
            if not choice:
                choice = default_choice

        if choice in ("quit", "exit"):
            break

        # VPN toggle
        if choice == "vpn":
            toggle_vpn_menu()
            continue

        # Task cancellation
        if choice == "t":
            manage_scheduled_tasks()
            continue

        # F2: Channel notes management
        if choice == "notes":
            _nq = input("Search for channel to view/edit note: ").strip()
            if not _nq:
                continue
            _nresults = search_channels(channels, _nq)
            if not _nresults:
                print("No channels found.")
                continue
            _nch = select_from_channel_list(_nresults, epg)
            if _nch:
                _existing_note = get_channel_note(_nch)
                if _existing_note:
                    print(f"Current note: {_existing_note}")
                _new_note = input("Enter note (Enter to clear): ").strip()
                set_channel_note(_nch, _new_note)
                print(f"  Note {'saved' if _new_note else 'cleared'}.")
            continue

        # F5: Browse and play recordings
        if choice == "rec":
            ensure_recordings_dir()
            _recs = sorted(RECORDINGS_DIR.glob("*.mkv"), key=lambda _f: _f.stat().st_mtime, reverse=True)
            if not _recs:
                print("No recordings found.")
                continue
            _page = _recs[:20]
            print(f"\nRecordings ({len(_recs)} total, showing latest 20):")
            for _ri, _rf in enumerate(_page, 1):
                _sz = _rf.stat().st_size / (1024 * 1024)
                _mt = datetime.fromtimestamp(_rf.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  {_ri:>2}. {_rf.name}  ({_sz:.1f} MB, {_mt})")
            _rsel = input(f"\nSelect recording (1-{len(_page)}, Enter to cancel): ").strip()
            if _rsel.isdigit():
                _ridx = int(_rsel) - 1
                if 0 <= _ridx < len(_page):
                    _rp = _page[_ridx]
                    print(f"\nPlaying: {_rp.name}")
                    run_mpv_with_logging(["mpv", "--save-position-on-quit", str(_rp)], _rp.name)
            continue

        # F6: Export watch history to CSV
        if choice == "export":
            import csv as _csv
            _hist = load_watch_history()
            if not _hist:
                print("No watch history to export.")
                continue
            _efn = f"watch_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            try:
                with open(_efn, "w", newline="", encoding="utf-8") as _ef:
                    _fields = ["name", "tvg-id", "url", "watch_count", "total_duration_seconds", "last_watched"]
                    _wr = _csv.DictWriter(_ef, fieldnames=_fields, extrasaction="ignore")
                    _wr.writeheader()
                    _wr.writerows(_hist)
                print(f"  Exported {len(_hist)} entries to {_efn}")
            except Exception as _e:
                print(f"Error exporting history: {_e}", file=sys.stderr)
            continue

        # F4: Playlist health check
        if choice == "hc":
            from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
            _hc_sample = channels
            if len(channels) > 200:
                _hc_groups = get_channel_groups(channels)
                print(f"\n{len(channels)} channels total.")
                print("  a: Check all (slow)")
                for _hci, (_hgn, _hgc) in enumerate(_hc_groups[:15], 1):
                    print(f"  {_hci}: {_hgn} ({_hgc})")
                _hc_sel = input("Select group number or 'a' for all: ").strip().lower()
                if _hc_sel == 'a':
                    print(f"Checking all {len(channels)} channels...")
                elif _hc_sel.isdigit() and 0 <= int(_hc_sel) - 1 < len(_hc_groups):
                    _hc_grp = _hc_groups[int(_hc_sel) - 1][0]
                    _hc_sample = [c for c in channels if _ch_in_group(c, {_hc_grp})]
                    print(f"Checking {len(_hc_sample)} channels in '{_hc_grp}'...")
                else:
                    continue
            else:
                print(f"Checking {len(_hc_sample)} channels...")

            def _check_url(ch):
                url = ch.get("url", "")
                try:
                    import requests as _req
                    r = _req.head(url, timeout=5, allow_redirects=True)
                    return ch, r.status_code < 400
                except Exception:
                    return ch, False

            _hc_alive = 0
            _hc_dead = []
            _hc_done = 0
            with ThreadPoolExecutor(max_workers=20) as _hc_ex:
                _hc_futs = {_hc_ex.submit(_check_url, c): c for c in _hc_sample}
                for _hc_fut in _as_completed(_hc_futs):
                    _hc_ch, _hc_ok = _hc_fut.result()
                    _hc_done += 1
                    if _hc_ok:
                        _hc_alive += 1
                    else:
                        _hc_dead.append(_hc_ch)
                    if _hc_done % 50 == 0:
                        print(f"  Checked {_hc_done}/{len(_hc_sample)}...")
            print(f"\nHealth Check ({len(_hc_sample)} channels):")
            print(f"  Alive: {_hc_alive}  Dead: {len(_hc_dead)}")
            if _hc_dead:
                _hc_show = input(f"Show {len(_hc_dead)} dead channel(s)? (y/n): ").strip().lower()
                if _hc_show == 'y':
                    for _dc in _hc_dead[:50]:
                        print(f"  x {_dc.get('name', '?')} [{_dc.get('group-title', '?')}]")
                    if len(_hc_dead) > 50:
                        print(f"  ... and {len(_hc_dead) - 50} more")
            continue

        # Switch playlist
        if choice == "pl":
            print("\nAvailable Playlists:")
            for i, pl_item in enumerate(playlists, 1):
                marker = " (current)" if pl_item is chosen_playlist else ""
                print(f"  {i}. {pl_item['name']}{marker}")
            pl_sel = input(f"Select playlist (1-{len(playlists)}): ").strip()
            if pl_sel.isdigit():
                pl_idx = int(pl_sel) - 1
                if 0 <= pl_idx < len(playlists):
                    if playlists[pl_idx] is chosen_playlist:
                        print("Already on this playlist.")
                    else:
                        chosen_playlist = playlists[pl_idx]
                        print(f"\nSwitching to: {chosen_playlist['name']}")
                        channels = load_m3u_cached(chosen_playlist["m3u_url"])
                        if not channels:
                            print("Could not load channels.", file=sys.stderr)
                        else:
                            channels_global = channels
                            epg = {}
                            if not args.no_epg and chosen_playlist.get("epg_url"):
                                print("Loading EPG data...")
                                epg = load_epg(chosen_playlist["epg_url"])
                                if epg:
                                    print(f"✓ Loaded EPG for {len(epg)} channels.")
                                else:
                                    print("⚠ EPG unavailable.")
                            epg_global = epg
                            print(f"✓ Switched to {chosen_playlist['name']} ({len(channels)} channels)")
                else:
                    print("Invalid selection.", file=sys.stderr)
            continue

        # Refresh EPG
        if choice == "epg":
            if args.no_epg:
                print("EPG is disabled (--no-epg). Restart without this flag to load guide data.")
            elif chosen_playlist.get("epg_url"):
                print("\nRefreshing EPG data...")
                epg = load_epg(chosen_playlist["epg_url"])
                epg_global = epg
                if epg:
                    logging.info(f"EPG refreshed and global state updated")
                    print(f"✓ Refreshed EPG for {len(epg)} channels.")
                else:
                    print("⚠ EPG still unavailable.")
            else:
                print("No EPG URL configured for this playlist.", file=sys.stderr)
            continue

        # F7: Config hot-reload
        if choice == "cfg":
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
                    _new_cfg = json.load(_f)
                playlists = _new_cfg.get("playlists", playlists)
                expected_vpn_ip = _new_cfg.get("vpn_ip", expected_vpn_ip)
                print(f"✓ Config reloaded — {len(playlists)} playlist(s) found.")
                logging.info("Config reloaded from disk")
            except Exception as _e:
                print(f"Error reloading config: {_e}", file=sys.stderr)
            continue

        # F1: Favorites management
        if choice == "fav":
            _favs = load_favorites()
            if _favs:
                print("\nCurrent favorites:")
                for _i, _f in enumerate(_favs, 1):
                    print(f"  {_i}. {_f.get('name', 'Unknown')} [{_f.get('group-title', '')}]")
            else:
                print("\nNo favorites yet.")
            print("\nEnter a channel name to search and add, or '-N' to remove, or Enter to cancel:")
            _fav_input = input("  > ").strip()
            if _fav_input.startswith("-") and _fav_input[1:].isdigit():
                _rm_idx = int(_fav_input[1:]) - 1
                if 0 <= _rm_idx < len(_favs):
                    _removed = _favs.pop(_rm_idx)
                    save_favorites(_favs)
                    print(f"✓ Removed from favorites: {_removed.get('name', 'Unknown')}")
                else:
                    print("Invalid selection.")
            elif _fav_input:
                _fav_results = search_channels(channels, _fav_input)
                if not _fav_results:
                    print("No channels found.")
                else:
                    _chosen_fav = select_from_channel_list(_fav_results, epg)
                    if _chosen_fav:
                        _added = toggle_favorite(_chosen_fav)
                        print(f"{'✓ Added to' if _added else '✓ Removed from'} favorites: {_chosen_fav.get('name')}")
            continue

        # F5: Group browser
        if choice == "g":
            groups = get_channel_groups(channels)
            if not groups:
                print("No channel groups found.")
                continue
            print(f"\nChannel Groups ({len(groups)} total):")
            print("-" * 60)
            for _gi, (_gname, _gcnt) in enumerate(groups, 1):
                print(f"  {_gi:>3}. {_gname} ({_gcnt})")
            _gsel = input(f"\nSelect group (1-{len(groups)}, or 'b' to back): ").strip()
            if _gsel.lower() == 'b' or not _gsel:
                continue
            if not _gsel.isdigit():
                print("Invalid selection.", file=sys.stderr)
                continue
            _gidx = int(_gsel) - 1
            if not (0 <= _gidx < len(groups)):
                print("Invalid selection.", file=sys.stderr)
                continue
            _selected_group = groups[_gidx][0]
            _group_channels = [c for c in channels if _ch_in_group(c, {_selected_group})]
            print(f"\n{_selected_group} — {len(_group_channels)} channel(s):")
            _gc = select_from_channel_list(_group_channels, epg)
            if _gc:
                play_channel(_gc)
            continue

        # Select from favorites, frequent channels, or search-history now-playing
        if choice.isdigit():
            num = int(choice)
            if favorites and 1 <= num <= fav_count:
                play_channel(favorites[num - 1]["channel"])
            elif frequent and fav_count + 1 <= num <= fav_count + len(frequent):
                play_channel(frequent[num - fav_count - 1]["channel"])
            elif search_now_playing and fav_count + len(frequent) + 1 <= num <= fav_count + len(frequent) + len(search_now_playing):
                item = search_now_playing[num - fav_count - len(frequent) - 1]
                play_channel(item["result"]["channel"], item["result"])
            else:
                print("Invalid selection.", file=sys.stderr)
            continue

        # Show search
        if choice == 's':
            if _startup_search:
                query_input = _startup_search
                _startup_search = None  # only auto-run once
                print(f"Auto-searching: '{query_input}'")
            else:
                # Load and display recent searches
                recent_searches = load_search_history()

                if recent_searches:
                    print("\nRecent searches:")
                    for i, search in enumerate(recent_searches, 1):
                        print(f"  {i}. {search}")
                    print("\nEnter a number to repeat a search, or type a new search term")

                query_input = input("Search for a show/movie: ").strip()
                if not query_input:
                    continue

            # Check if user selected a recent search
            if query_input.isdigit() and recent_searches:
                index = int(query_input) - 1
                if 0 <= index < len(recent_searches):
                    query = recent_searches[index]
                    print(f"Using recent search: '{query}'")
                else:
                    print("Invalid selection.", file=sys.stderr)
                    continue
            else:
                query = query_input

            hours_input = input("Search how many hours ahead? (1-9, default 3): ").strip()
            hours_ahead = 3
            if hours_input and hours_input.isdigit():
                hours_ahead = max(1, min(int(hours_input), 9))

            groups_input = input("Filter by group (comma-separated, Enter for all): ").strip()
            groups_filter = {g.strip() for g in groups_input.split(",") if g.strip()} if groups_input else None

            print(f"\nSearching for '{query}' in the next {hours_ahead} hour(s)...")
            results = search_shows_in_timeframe(channels, epg, query, hours_ahead, groups=groups_filter)

            if not results:
                print("No shows found matching your search in the specified timeframe.")
                continue

            chosen_result = select_from_show_results(results)

            # F3: multi-select — schedule all chosen future shows at once
            if isinstance(chosen_result, list):
                add_to_search_history(query)
                scheduled_count = 0
                for _mr in chosen_result:
                    _mu = _mr.get("minutes_until", 0)
                    if _mu <= 5:
                        print(f"Skipped (already started): {_mr['title']}")
                        continue
                    _ch = _mr["channel"]
                    _title = _mr.get("title", "Unknown")
                    _ep = _mr.get("episode_num", "")
                    _st = _mr.get("start_time")
                    _delay = _mu * 60
                    _task_id = int(time.time() * 1000) + scheduled_count
                    _cancel = threading.Event()
                    _sched_time = datetime.now().astimezone() + timedelta(seconds=_delay)
                    with SCHEDULED_TASKS_LOCK:
                        SCHEDULED_TASKS.append({
                            "id": _task_id, "type": "playback",
                            "channel_name": _ch.get("name", "?"),
                            "provider": _ch.get("group-title", ""),
                            "show_title": _title,
                            "scheduled_time": _sched_time,
                            "cancel_event": _cancel,
                        })
                    threading.Thread(
                        target=scheduled_playback_task,
                        args=(_ch.get("url", ""), _delay, _ch.get("name", "?"), _title,
                              _ch.get("group-title", ""), _task_id, _ep, _st, _cancel),
                        daemon=True,
                    ).start()
                    print(f"✓ Scheduled: {_title} in {_mu} min")
                    scheduled_count += 1
                if scheduled_count:
                    print(f"\n{scheduled_count} show(s) scheduled. Keep this terminal open.")
                continue

            if chosen_result:
                # Add to search history since user selected a result
                add_to_search_history(query)
                channel = chosen_result["channel"]
                title = chosen_result["title"]
                time_status = chosen_result["time_status"]
                is_playing_now = chosen_result.get("is_playing_now", False)
                minutes_until = chosen_result.get("minutes_until", 0)

                print(f"\nYou selected: {title} ({time_status})")
                provider = channel.get('group-title', 'Unknown Provider')
                print(f"Channel: {channel.get('name', 'Unknown')} [{provider}]")

                # For future shows only (not currently playing or already started), offer scheduling
                # If show already started (minutes_until <= 0) or is playing now, use normal playback
                if not is_playing_now and minutes_until > 5:
                    start_time = chosen_result.get("start_time")
                    stop_time = chosen_result.get("stop_time")

                    if start_time and stop_time:
                        duration_minutes = int((stop_time - start_time).total_seconds() / 60)
                        print(f"\nShow details:")
                        print(f"  Starts in: {minutes_until} minutes")
                        print(f"  Duration: {duration_minutes} minutes")

                    print("\nOptions:")
                    print("  p: Pop open when show starts (auto-watch)")
                    print("  r: Schedule recording")
                    print("  n: Notify me when show starts (reminder only)")
                    print("  w: Watch channel now (show not started yet)")
                    print("  b: Back")

                    future_choice = input("\nYour choice (default: p): ").strip().lower()

                    if not future_choice:
                        future_choice = 'p'

                    if future_choice == 'b':
                        continue
                    elif future_choice == 'p':
                        # Schedule playback to auto-launch when show starts
                        channel_name = channel.get('name', 'Unknown Channel')
                        channel_url = channel.get('url', '')

                        print(f"\nPlayback will auto-launch when show starts")
                        confirm = input("Schedule this playback? (y/n): ").strip().lower()

                        if confirm == 'y':
                            delay_seconds = minutes_until * 60
                            show_title = chosen_result.get("title", "Unknown")
                            episode_num = chosen_result.get("episode_num", "")
                            start_time = chosen_result.get("start_time")

                            # Generate unique task ID
                            task_id = int(time.time() * 1000)  # Millisecond timestamp
                            cancel_event = threading.Event()

                            # Add to scheduled tasks list
                            scheduled_time = datetime.now().astimezone() + timedelta(seconds=delay_seconds)
                            with SCHEDULED_TASKS_LOCK:
                                SCHEDULED_TASKS.append({
                                    "id": task_id,
                                    "type": "playback",
                                    "channel_name": channel_name,
                                    "provider": provider,
                                    "show_title": show_title,
                                    "scheduled_time": scheduled_time,
                                    "cancel_event": cancel_event,
                                })

                            thread = threading.Thread(
                                target=scheduled_playback_task,
                                args=(channel_url, delay_seconds, channel_name, show_title, provider, task_id, episode_num, start_time, cancel_event),
                                daemon=True
                            )
                            thread.start()

                            print(f"✓ Playback scheduled!")
                            print(f"  Will auto-launch in {minutes_until} minutes")
                            print(f"  Keep this terminal open until show starts\n")
                        else:
                            print("Playback not scheduled.")
                    elif future_choice == 'r':
                        # Schedule recording directly
                        channel_name = channel.get('name', 'Unknown Channel')
                        channel_url = channel.get('url', '')

                        ensure_recordings_dir()
                        show_title = chosen_result.get("title", "Unknown")
                        episode_num = chosen_result.get("episode_num", "")
                        start_time = chosen_result.get("start_time")
                        filename = get_safe_filename(channel_name, show_title)
                        output_path = RECORDINGS_DIR / filename

                        print(f"\nRecording will be saved to: {output_path}")
                        future_dur_input = input("Duration in minutes (Enter for unlimited): ").strip()
                        future_rec_duration = int(future_dur_input) * 60 if future_dur_input and future_dur_input.isdigit() else 0
                        confirm = input("Schedule this recording? (y/n): ").strip().lower()

                        if confirm == 'y':
                            delay_seconds = minutes_until * 60

                            # Generate unique task ID
                            task_id = int(time.time() * 1000)  # Millisecond timestamp
                            cancel_event = threading.Event()

                            # Add to scheduled tasks list
                            scheduled_time = datetime.now().astimezone() + timedelta(seconds=delay_seconds)
                            with SCHEDULED_TASKS_LOCK:
                                SCHEDULED_TASKS.append({
                                    "id": task_id,
                                    "type": "recording",
                                    "channel_name": channel_name,
                                    "provider": provider,
                                    "show_title": show_title,
                                    "scheduled_time": scheduled_time,
                                    "cancel_event": cancel_event,
                                })

                            thread = threading.Thread(
                                target=scheduled_recording_task,
                                args=(channel_url, output_path, delay_seconds, channel_name, show_title, provider, True, task_id, episode_num, start_time, future_rec_duration, cancel_event),
                                daemon=True
                            )
                            thread.start()

                            print(f"✓ Recording scheduled!")
                            print(f"  Will start in {minutes_until} minutes")
                            print(f"  Keep this terminal open until recording starts\n")
                        else:
                            print("Recording not scheduled.")
                    elif future_choice == 'n':
                        _r_start = chosen_result.get("start_time")
                        _r_title = chosen_result.get("title", "Unknown")
                        _r_chname = channel.get("name", "Unknown")
                        _r_task_id = int(time.time() * 1000)
                        _r_cancel = threading.Event()
                        _r_sched_t = (_r_start - timedelta(seconds=60)) if _r_start else datetime.now().astimezone()
                        with SCHEDULED_TASKS_LOCK:
                            SCHEDULED_TASKS.append({
                                "id": _r_task_id, "type": "reminder",
                                "channel_name": _r_chname,
                                "provider": provider,
                                "show_title": _r_title,
                                "scheduled_time": _r_sched_t,
                                "cancel_event": _r_cancel,
                            })
                        threading.Thread(
                            target=scheduled_reminder_task,
                            args=(_r_title, _r_start, _r_chname, _r_task_id, _r_cancel),
                            daemon=True,
                        ).start()
                        print(f"  Reminder set! You'll be notified 1 minute before '{_r_title}' starts.")
                    elif future_choice == 'w':
                        # Watch channel now even though show hasn't started
                        play_channel(channel, chosen_result)
                    else:
                        print("Invalid choice.", file=sys.stderr)
                else:
                    # Currently playing show - use normal flow
                    play_channel(channel, chosen_result)

        # Channel search
        elif choice == 'c':
            query = input("Search for a channel: ").strip()
            if not query:
                continue

            results = search_channels(channels, query)
            if not results:
                print("No channels found matching your search.")
                continue

            chosen_channel = select_from_channel_list(results, epg)
            if chosen_channel:
                play_channel(chosen_channel)

        else:
            print("Invalid option.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)
