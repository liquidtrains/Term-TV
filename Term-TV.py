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
import shutil as _shutil


def _check_external_tools():
    """Warn at startup if external tools are missing."""
    if not _shutil.which("mpv"):
        print("Error: 'mpv' not found on PATH. mpv is required to play streams.", file=sys.stderr)
        sys.exit(1)
    for tool in ("ffmpeg", "ffprobe"):
        if not _shutil.which(tool):
            print(f"Warning: '{tool}' not found on PATH. Recording and subtitle features will be unavailable.", file=sys.stderr)


_check_external_tools()
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

import lib.term_tv_core as _core

from lib.term_tv_core import (
    Channel, EpgData, ShowResult,
    WATCH_HISTORY_FILE, SEARCH_HISTORY_FILE, RECORDINGS_DIR,
    EPG_CACHE_DIR, M3U_CACHE_DIR,
    load_m3u, load_m3u_cached, load_epg,
    parse_epg_time, is_new_episode,
    search_channels, search_shows_in_timeframe,
    find_alternative_streams, find_future_reruns,
    log_channel_watch, load_watch_history, get_frequent_channels,
    display_frequent_channels, get_search_history_now_playing,
    display_search_history_now_playing,
    load_search_history, save_search_history, add_to_search_history,
    ensure_recordings_dir, extract_subtitles_from_recording, get_safe_filename,
    clean_old_cache_files, get_public_ip, check_vpn_status, input_with_countdown,
)

# --- Constants ---
CONFIG_FILE = Path("config.json")
LOG_FILE = Path("term-tv.log")
MPV_LOG_FILE = Path("mpv-output.log")
RECORDINGS_LOG_FILE = Path("recordings.log")
MPV_LOG_ARCHIVE_DIR = Path("mpv-log-archive")
MPV_LOG_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per chunk

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


def log_mpv_output(channel_name: str, command: List[str], stdout: str = "", stderr: str = "", returncode: Optional[int] = None, log_path: Optional[Path] = None):
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
        with open(log_path or MPV_LOG_FILE, 'a', encoding='utf-8') as f:
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

def run_mpv_with_logging(mpv_args: List[str], channel_name: str, log_path: Optional[Path] = None) -> subprocess.CompletedProcess:
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
            log_file = open(log_path or MPV_LOG_FILE, 'a', encoding='utf-8')
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
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            encoding='utf-8',
            errors='replace',  # Replace undecodable characters with �
            bufsize=1  # Line buffered
        )

        # Read and display output line by line
        for line in process.stdout:
            stripped = line.strip()

            # Always write ALL output to log file for diagnostics
            if log_file:
                log_file.write(line)
                log_file.flush()

            # Determine if this line should be shown in terminal
            # Hide verbose/status lines, show only important messages
            show_in_terminal = True

            # Filter out verbose status lines
            if (stripped.startswith('AV:') or                              # Status lines
                stripped.startswith('(Buffering)') or                      # Buffering status
                stripped.startswith('(Paused)') or                         # Paused status
                stripped.startswith('●') or                                # Stream info (Video/Audio)
                stripped.startswith('○') or                                # Stream info (Subs)
                stripped.startswith('AO:') or                              # Audio output
                stripped.startswith('VO:') or                              # Video output
                "Can't load unknown script:" in stripped or                # Script loading warnings
                '[videoclip_master]' in stripped or                        # Plugin messages
                '[command_palette]' in stripped):                          # Plugin messages
                show_in_terminal = False

            # Always show critical messages
            if ('error' in stripped.lower() or                             # Errors
                'warning' in stripped.lower() or                           # Warnings (except script loading)
                'desynchronisation detected' in stripped.lower() or        # A/V sync issues
                'Invalid audio PTS' in stripped or                         # PTS issues
                'Invalid video PTS' in stripped or                         # PTS issues
                '[ffmpeg/' in stripped or                                  # FFmpeg messages
                'Exiting...' in stripped or                                # Exit messages
                'Failed' in stripped):                                     # Failures
                show_in_terminal = True

            # Exception: Don't show "Can't load unknown script" warnings even though they contain "warning"
            if "Can't load unknown script:" in stripped:
                show_in_terminal = False

            # Display to terminal if not filtered
            if show_in_terminal:
                print(line, end='')

        # Wait for process to complete
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

# --- Global State ---
SCHEDULED_TASKS = []  # List of scheduled playback/recording tasks
SCHEDULED_TASKS_LOCK = threading.Lock()  # Protects SCHEDULED_TASKS from concurrent access
DATA_LOCK = threading.RLock()  # Protects channels_global / epg_global during EPG refresh
channels_global = []  # Global reference to channels (for scheduled task retry logic)
epg_global = {}  # Global reference to EPG data (for scheduled task retry logic)


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
                type_icon = "▶"
                type_label = "PLAYBACK"
            elif task_type == "recording":
                type_icon = "⏺"
                type_label = "RECORD"
            else:
                type_icon = "•"
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

    Args:
        channel_url: Stream URL to play
        delay_seconds: How long to wait before starting
        channel_name: Name of channel for display
        show_title: Show title for display
        provider: Provider/category for display
        task_id: ID for tracking this task
        episode_num: Episode number (e.g., "S03E05") for finding alternatives
        original_start_time: Original start time for finding alternatives
    """
    global SCHEDULED_TASKS, channels_global, epg_global

    logging.info(f"Scheduled playback task created: {show_title} on {channel_name} [{provider}]")
    logging.info(f"  URL: {channel_url}")
    logging.info(f"  Delay: {delay_seconds} seconds ({delay_seconds // 60} minutes)")
    logging.info(f"  Episode: {episode_num}")

    # Snapshot globals once at the start so the task works with a consistent dataset
    # even if an EPG refresh happens while this thread is running.
    with DATA_LOCK:
        _channels = channels_global
        _epg = epg_global

    print(f"\n[SCHEDULED] Playback will start in {delay_seconds // 60} minutes...")
    print(f"[SCHEDULED] Channel: {channel_name} [{provider}]")
    print(f"[SCHEDULED] Show: {show_title}")
    print(f"[SCHEDULED] Will auto-launch when show starts\n")

    # Wait for the scheduled time; cancel_event.wait returns True if cancelled early
    _evt = cancel_event or threading.Event()
    if _evt.wait(timeout=delay_seconds):
        logging.info(f"Playback task cancelled: {show_title}")
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

            # Wait briefly to detect early startup failures (bad URL, stream down, etc.)
            # If mpv is still running after 10s it means the stream is working fine.
            try:
                stdout, stderr = proc.communicate(timeout=10)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # Still running — stream is working, wait for user to close mpv
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

    if episode_num and original_start_time and _channels and _epg:
        alternatives = find_alternative_streams(
            _channels,
            _epg,
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

    if episode_num and _channels and _epg:
        reruns = find_future_reruns(
            _channels,
            _epg,
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

    Args:
        channel_url: Stream URL to record
        output_path: Where to save the recording
        delay_seconds: How long to wait before starting
        channel_name: Name of channel for display
        show_title: Show title for display
        provider: Provider/category for display
        extract_subs: Whether to extract subtitles after recording
        task_id: ID for tracking this task
        episode_num: Episode number (e.g., "S03E05") for finding alternatives
        original_start_time: Original start time for finding alternatives
    """
    global SCHEDULED_TASKS, channels_global, epg_global

    logging.info(f"Scheduled recording task created: {show_title} on {channel_name} [{provider}]")
    logging.info(f"  URL: {channel_url}")
    logging.info(f"  Delay: {delay_seconds} seconds ({delay_seconds // 60} minutes)")
    logging.info(f"  Episode: {episode_num}")

    with DATA_LOCK:
        _channels = channels_global
        _epg = epg_global

    print(f"\n[SCHEDULED] Recording will start in {delay_seconds // 60} minutes...")
    print(f"[SCHEDULED] Channel: {channel_name} [{provider}]")
    print(f"[SCHEDULED] Show: {show_title}")
    print(f"[SCHEDULED] Output: {output_path}")
    print(f"[SCHEDULED] Press Ctrl+C in mpv window to stop recording\n")

    # Wait for the scheduled time; cancel_event.wait returns True if cancelled early
    _evt = cancel_event or threading.Event()
    if _evt.wait(timeout=delay_seconds):
        logging.info(f"Recording task cancelled: {show_title}")
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

            # Wait briefly to detect early startup failures; if still running after 10s,
            # the stream is working — wait indefinitely for user to close mpv.
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
                log_path=RECORDINGS_LOG_FILE,
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

    if episode_num and original_start_time and _channels and _epg:
        alternatives = find_alternative_streams(
            _channels,
            _epg,
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
                    log_path=RECORDINGS_LOG_FILE,
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

    if episode_num and _channels and _epg:
        reruns = find_future_reruns(
            _channels,
            _epg,
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

    print("\nRecording options:")
    print("  w: Watch only (no recording)")
    print("  r: Record while watching")
    print("  s: Schedule recording for later")
    print("  b: Back")

    record_choice = input("\nYour choice (default: w): ").strip().lower()

    if not record_choice:
        record_choice = 'w'

    if record_choice == 'b':
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
        result = run_mpv_with_logging(mpv_args, f"{channel_name} [{provider}]",
                                      log_path=RECORDINGS_LOG_FILE if record_choice == 'r' else None)
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
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logging.info(f"Session duration: {duration:.1f} seconds")

        if duration >= 2:
            log_channel_watch(channel, int(duration))

            if duration >= 120:
                logging.info(f"Session logged: {int(duration // 60)} minutes")
                if record_choice == 'w':
                    print(f"\nWatched for {int(duration // 60)} minutes - session logged!")
            else:
                logging.debug(f"Session too short to log: {int(duration)} seconds")
                if record_choice == 'w':
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

    selection = input(f"Select show to watch (1-{len(results)}, or 'b' for back): ").strip()

    if selection.lower() == 'b':
        return None

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


# --- Main Application ---

def main():
    """Main application entry point."""
    global channels_global, epg_global

    parser = argparse.ArgumentParser(description="Term-TV: CLI IPTV player")
    parser.add_argument("--playlist", type=int, metavar="N", help="Auto-select playlist N (1-based, skips menu)")
    parser.add_argument("--skip-vpn", action="store_true", help="Skip VPN check")
    parser.add_argument("--search", metavar="QUERY", help="Jump directly to show search on startup")
    parser.add_argument("--no-epg", action="store_true", help="Skip EPG loading (channel browsing only)")
    args = parser.parse_args()

    # --- Logging Setup ---
    setup_logging()
    logging.info("Starting main application")

    # --- Register cleanup on exit ---
    atexit.register(clean_old_cache_files)
    atexit.register(archive_mpv_log)
    logging.debug("Registered cache cleanup and MPV log archiving on exit")

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

    # Apply optional recordings_dir override from config
    global RECORDINGS_DIR
    if "recordings_dir" in config:
        RECORDINGS_DIR = Path(config["recordings_dir"]).expanduser()
        _core.RECORDINGS_DIR = RECORDINGS_DIR

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

    # --- VPN Check ---
    if not args.skip_vpn and not check_vpn_status(expected_vpn_ip):
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

    with DATA_LOCK:
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

    with DATA_LOCK:
        epg_global = epg

    # If --search was passed, run the search once before entering the loop
    _startup_search = args.search

    # --- Main Interaction Loop ---
    while True:
        # Display frequently watched channels
        frequent = get_frequent_channels(channels, epg)
        display_frequent_channels(frequent)

        # Display currently-airing matches from recent search history
        search_now_playing = get_search_history_now_playing(channels, epg)
        display_search_history_now_playing(search_now_playing, start_index=len(frequent) + 1)

        # Display scheduled tasks
        display_scheduled_tasks()

        print("\nOptions:")
        if frequent:
            print(f"  1-{len(frequent)}: Watch frequent channel")
        if search_now_playing:
            s_start = len(frequent) + 1
            s_end = len(frequent) + len(search_now_playing)
            label = str(s_start) if s_start == s_end else f"{s_start}-{s_end}"
            print(f"  {label}: Watch from recent searches")
        print("  s: Search for show/movie")
        print("  c: Search for channel")
        print("  epg: Refresh EPG data")
        if len(playlists) > 1:
            print("  pl: Switch playlist")
        if SCHEDULED_TASKS:
            print("  t: Manage scheduled tasks")
        print("  quit: Exit")

        if _startup_search:
            choice = 's'
        else:
            choice = input("\nYour choice (default: s): ").strip().lower()

        if not choice:
            choice = 's'

        if choice in ("quit", "exit"):
            break

        if choice == "t":
            if not SCHEDULED_TASKS:
                print("No scheduled tasks.")
            else:
                manage_scheduled_tasks()
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
                    chosen_playlist = playlists[pl_idx]
                    print(f"\nSwitching to: {chosen_playlist['name']}")
                    channels = load_m3u_cached(chosen_playlist["m3u_url"])
                    if not channels:
                        print("Could not load channels.", file=sys.stderr)
                    else:
                        with DATA_LOCK:
                            channels_global = channels
                        epg = {}
                        if not args.no_epg and chosen_playlist.get("epg_url"):
                            print("Loading EPG data...")
                            epg = load_epg(chosen_playlist["epg_url"])
                            if epg:
                                print(f"✓ Loaded EPG for {len(epg)} channels.")
                            else:
                                print("⚠ EPG unavailable.")
                        with DATA_LOCK:
                            epg_global = epg
                        print(f"✓ Switched to {chosen_playlist['name']} ({len(channels)} channels)")
                else:
                    print("Invalid selection.", file=sys.stderr)
            continue

        # Refresh EPG
        if choice == "epg":
            if chosen_playlist.get("epg_url"):
                print("\nRefreshing EPG data...")
                epg = load_epg(chosen_playlist["epg_url"])
                with DATA_LOCK:
                    epg_global = epg
                if epg:
                    logging.info(f"EPG refreshed and global state updated")
                    print(f"✓ Refreshed EPG for {len(epg)} channels.")
                else:
                    print("⚠ EPG still unavailable.")
            else:
                print("No EPG URL configured for this playlist.", file=sys.stderr)
            continue

        # Select from frequent channels or search-history now-playing
        if choice.isdigit():
            num = int(choice)
            if frequent and 1 <= num <= len(frequent):
                play_channel(frequent[num - 1]["channel"])
            elif search_now_playing and len(frequent) + 1 <= num <= len(frequent) + len(search_now_playing):
                item = search_now_playing[num - len(frequent) - 1]
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
