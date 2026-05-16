# Term-TV: Advanced IPTV CLI Player with Smart Search

A powerful, feature-rich command-line Python application for browsing and playing IPTV channels from M3U playlists, with comprehensive EPG (Electronic Program Guide) support, intelligent show search, and watch history tracking. 📺

---

## Features

### Core Functionality
- **Dual Search Modes**:
  - **Show/Movie Search** (default): Search across all channels for specific content currently playing or airing soon
  - **Channel Search**: Traditional channel name search with EPG preview
  - **Search History Cache**: Quick re-search from last 5 successful searches
- **Intelligent Watch Time Tracking**: Automatically tracks your top 3 most-watched channels by total viewing time (min 2 minutes watch time)
- **Provider Labels**: All channels display their provider/source (e.g., [MoveOnJoy], [A1xmedia US Channels]) for quality comparison
- **Playlist Management**:
  - Load multiple IPTV playlists from a central `config.json` file
  - **Auto-select with countdown**: 15-second timer auto-selects playlist 1 if no input
  - Auto-loads if only one playlist configured
- **M3U Parsing**: Downloads and robustly parses remote `.m3u` playlist files with provider categorization
  - **Smart M3U Caching**: Hash-based caching with conditional HTTP (ETag/Last-Modified)
  - Only re-downloads when playlist changes
  - Falls back to cache on network errors
- **Comprehensive EPG Support**:
  - Fetches and parses EPG data from `.xml` or compressed `.xml.gz` files
  - **Smart EPG Caching**: Stores EPG locally in `.epg_cache/` directory with conditional HTTP requests (only re-downloads when updated)
  - **Automatic Cache Cleanup**: Removes cache files older than 15 days on program exit
  - Displays episode numbers (e.g., S2025E12), subtitles, air dates, and descriptions
  - Proper timezone handling (converts UTC to your local time)
- **MPV Integration**: Launches selected streams directly in `mpv` media player
  - **Filtered Output**: Hides verbose status lines from terminal while logging everything to file
  - **Complete Diagnostics**: All mpv output saved to `mpv-output.log` for troubleshooting
- **Recording Capabilities**:
  - Record streams while watching or schedule recordings for future shows
  - **Subtitle Pre-loading**: Prevents recording from stopping when toggling subtitles
  - Auto-extracts subtitles from recordings to separate SRT files
  - Recordings saved to `~/Videos/Recordings/` with metadata-based filenames
- **Comprehensive Logging**:
  - **DEBUG** level logs to `term-tv.log` for diagnostics
  - **WARNING** level logs to console for important messages
  - **MPV output** logged to `mpv-output.log` with full command details and exit codes

### Advanced Show Search
- **Time-Based Search**: Find shows currently playing or airing within 1-9 hours (default: 3 hours)
- **Currently Playing Detection**: Shows marked with `◄◄◄` indicator
- **New Episode Detection**: Episodes aired within the last 7 days marked with `+++`
- **Multi-Channel Support**: Shows all channel variants with same EPG (e.g., "FXX" and "FXX (alternate)")
- **Intelligent Retry Logic**: Automatic failover when streams fail
  - **Tier 1**: Retry original URL once
  - **Tier 2**: Search for same episode on alternative channels/providers
  - **Tier 3**: Try each alternative channel
  - **Tier 4**: Search for future reruns (next 24 hours)
  - **Tier 5**: Auto-reschedule if rerun found
- **Rich Metadata Display**:
  - Season/Episode numbers (e.g., S03E05)
  - Episode subtitles/names
  - Countdown timers ("NOW PLAYING", "In 15 min", "In 2h 30m")
  - Channel names with provider labels
  - All channel variants shown for same show

### Scheduled Tasks
- **Auto-Launch Playback**: Schedule future shows to automatically open in mpv when they start (default option)
- **Scheduled Recording**: Schedule recordings for shows that haven't started yet
- **Live Task Monitoring**: Main menu displays all pending scheduled tasks with:
  - Task type indicators (▶ for playback, ⏺ for recording)
  - Countdown timers showing time until task starts
  - Show title, channel name, and provider
- **Background Execution**: Tasks run in background threads while you continue using the app
- **Keep-Alive Reminder**: Terminal must stay open for scheduled tasks to execute

### User Experience
- **Interactive Interface**: Clean, intuitive command-line interface
- **VPN Check**: Displays your public IP on launch with option to exit if VPN is off
- **Frequently Watched**: Quick-launch your top 3 most-watched channels ranked by total watch time (e.g., "2h 15m watched")
- **Default to Search**: Press Enter to immediately search for shows
- **Watch Time Tracking**: Accumulates total viewing time per channel (only sessions ≥ 2 minutes count)
- **Provider Visibility**: All channels show their source provider for quality comparison
- **Robust Error Handling**: Graceful network request and file parsing error recovery

---

## Prerequisites

Before you begin, ensure you have the following installed:

1. **Python 3.7+**: Uses modern Python features (type hints, datetime.timezone, etc.)
2. **mpv Media Player**: Required to play video streams
   - **macOS**: `brew install mpv`
   - **Linux (Debian/Ubuntu)**: `sudo apt-get install mpv`
   - **Windows**: Download from [mpv.io](https://mpv.io/installation/) and add to `PATH`
3. **ffmpeg** (optional): Required for subtitle extraction from recordings
   - **macOS**: `brew install ffmpeg`
   - **Linux (Debian/Ubuntu)**: `sudo apt-get install ffmpeg`
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to `PATH`
4. **OpenVPN** (optional, required for Term-TV-VPN.py only):
   - **macOS**: `brew install openvpn`
   - **Linux (Debian/Ubuntu)**: `sudo apt-get install openvpn`
   - **Windows**: Download from [openvpn.net](https://openvpn.net/community-downloads/)
5. **Python Packages**:
   - `requests` - HTTP library for downloading playlists/EPG

---

## Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/Term-TV.git
   cd Term-TV
   ```

2. **Install required Python packages:**
   ```bash
   pip install requests
   ```

3. **Create configuration file:**
   Create `config.json` in the same directory as the script.

---

## Configuration

The `config.json` file defines your IPTV playlists and optional VPN settings. Each playlist object requires `name` and `m3u_url`. The `epg_url` is optional but highly recommended for full functionality.

**Example `config.json`:**

```json
{
  "vpn_ip": "137.184.129.16",
  "openvpn": {
    "enabled": true,
    "executable": "",
    "config_file": "C:\\Users\\YourName\\OpenVPN\\config\\profile.ovpn",
    "auto_connect": true
  },
  "playlists": [
    {
      "name": "DREW",
      "m3u_url": "http://drewlive24.duckdns.org:8081/DrewLive/MergedPlaylist.m3u8",
      "epg_url": "https://raw.githubusercontent.com/DrewLiveTemp/DrewskiTemp24/main/DrewLive.xml.gz"
    },
    {
      "name": "Backup Channels",
      "m3u_url": "https://example.com/channels.m3u",
      "epg_url": "https://example.com/guide.xml.gz"
    }
  ]
}
```

**Configuration Notes:**
- `vpn_ip` (optional): Your VPN's public IP address for auto-confirmation
- `openvpn` (optional): OpenVPN configuration for **Term-TV-VPN.py** only
  - `enabled`: Set to `true` to enable auto-connect
  - `executable`: Path to openvpn executable (auto-detected if empty)
  - `config_file`: Path to your `.ovpn` profile (required if enabled)
  - `auto_connect`: Automatically connect on startup
- Multiple playlists supported
- EPG files can be `.xml` or `.xml.gz` (gzipped)
- M3U URLs can be HTTP or local file paths
- **Note**: Use double backslashes (`\\`) for Windows paths in JSON

---

## Usage

### Starting the Application

**Standard Version:**
```bash
python3 Term-TV.py
```

**VPN-Integrated Version** (requires OpenVPN):
```bash
# Windows (run as Administrator)
python3 Term-TV-VPN.py

# Linux/macOS (requires sudo for VPN)
sudo python3 Term-TV-VPN.py
```

### Term-TV-VPN.py Features

The VPN-integrated version automatically connects to OpenVPN before loading IPTV content:

- **Cross-Platform**: Works on Windows, macOS, and Linux
- **Auto-Detection**: Finds OpenVPN executable automatically in common install locations
- **Automatic Connection**: Connects to VPN on startup
- **Automatic Disconnect**: Disconnects VPN on exit (Ctrl+C or normal exit)
- **Graceful Degradation**: Continues without VPN if OpenVPN is not installed or configured
- **VPN Logging**: Creates `openvpn.log` for diagnostics (Windows)

**Requirements:**
- OpenVPN must be installed on your system
- Administrator/root privileges required to run VPN
- `.ovpn` config file must be specified in `config.json`

**Setup:**
1. Install OpenVPN for your platform
2. Add `openvpn` section to `config.json` (see Configuration above)
3. Set `config_file` to your `.ovpn` profile path
4. Run with elevated privileges (Administrator on Windows, sudo on Linux/macOS)

### Main Menu Flow

```
Loading: DREW

================================================================================
VPN CHECK
================================================================================
Your current public IP: 137.184.129.16
✓ VPN Confirmed (matches 137.184.129.16)
Auto-continuing...

Downloading M3U from http://drewlive24.duckdns.org:8081...
Loaded 823 channels.

Loading EPG data...
Checking EPG updates from https://raw.githubusercontent.com...
✓ EPG is up to date (using cached version)
Processed 45000 programs (25000 past, 20000 current/upcoming)
✓ Loaded EPG for 823 channels.

================================================================================
FREQUENTLY WATCHED:
================================================================================
1. NFL Channel [MoveOnJoy] (2h 15m watched)
   NOW PLAYING: NFL Daily (S2025E12) - "Week 12 Preview" +++

2. AMC HD [A1xmedia US Channels] (1h 30m watched)
   NOW PLAYING: Breaking Bad (S03E05) - "Mas"

3. ESPN [MoveOnJoy] (45m watched)
   (No EPG data available)

================================================================================
SCHEDULED TASKS:
================================================================================
▶ [PLAYBACK] In 25 min
   The Big Bang Theory (S04E12)
   Channel: TBS HD [MoveOnJoy]

⏺ [RECORD] In 1h 15m
   Breaking Bad (S05E14)
   Channel: AMC HD [A1xmedia US Channels]

Options:
  1-3: Watch frequent channel
  s: Search for show/movie
  c: Search for channel
  epg: Refresh EPG data
  quit: Exit

Your choice (default: s):
```

### Usage Modes

#### 1. Quick Launch Frequent Channels
```
Your choice: 1
```
Instantly launches your most frequently watched channel.

#### 2. Show/Movie Search (Default)
```
Your choice: s  (or just press Enter)

Recent searches:
  1. Breaking Bad
  2. King of the Hill
  3. South Park

Enter a number to repeat a search, or type a new search term
Search for a show/movie: Breaking Bad
Search how many hours ahead? (1-9, default 3): 5

Found 4 result(s):
--------------------------------------------------------------------------------
1. [ NOW PLAYING] 07:00 PM - Breaking Bad (S03E05) +++ ◄◄◄
   "Mas" - AMC HD [A1xmedia US Channels]

2. [   In 45 min] 08:00 PM - Breaking Bad (S03E06)
   "Sunset" - AMC HD [A1xmedia US Channels]

3. [   In 2h 30m] 10:00 PM - Breaking Bad Marathon
   Channel: AMC Classic [MoveOnJoy]

Select show to watch (1-4, or 'b' for back): 2

You selected: Breaking Bad (S03E06) (In 45 min)
Channel: AMC HD [A1xmedia US Channels]

Show details:
  Starts in: 45 minutes
  Duration: 60 minutes

Options:
  p: Pop open when show starts (auto-watch)
  r: Schedule recording
  w: Watch channel now (show not started yet)
  b: Back

Your choice (default: p): p

Playback will auto-launch when show starts
Schedule this playback? (y/n): y
✓ Playback scheduled!
  Will auto-launch in 45 minutes
  Keep this terminal open until show starts
```

**Visual Indicators:**
- `◄◄◄` = Currently playing
- `+++` = New episode (aired within 7 days)
- `(S03E05)` = Season/Episode number
- `[Provider]` = Stream source/provider
- Time shows in your local timezone

#### 3. Channel Search
```
Your choice: c
Search for a channel: ESPN

Found 3 channel(s):
--------------------------------------------------------------------------------
1. ESPN HD [MoveOnJoy]
   07:00 PM [NOW PLAYING]: NFL Live
   08:00 PM: SportsCenter
   09:00 PM: Monday Night Football

2. ESPN 2 [A1xmedia US Channels]
   07:30 PM [NOW PLAYING]: College Basketball

Select channel (1-2, or 'b' for back): 1

Selected: ESPN HD [MoveOnJoy]

Recording options:
  w: Watch only (no recording)
  r: Record while watching
  s: Schedule recording for later
  b: Back

Your choice (default: w): w

Launching: ESPN HD [MoveOnJoy]
Starting mpv...
```

#### 4. Refresh EPG Data
```
Your choice: epg

Refreshing EPG data...
Checking EPG updates from https://raw.githubusercontent.com...
✓ EPG is up to date (using cached version)
Processed 45000 programs (25000 past, 20000 current/upcoming)
✓ Refreshed EPG for 823 channels.
```

**EPG Caching Behavior:**
- First run: Downloads and saves EPG to `.epg_cache/` directory
- Subsequent runs: Uses HTTP conditional requests (ETag/Last-Modified headers)
- If EPG updated: Downloads new version and updates cache
- If EPG unchanged: Uses local cached copy (much faster)
- On network error: Falls back to cached version

**When to use:**
- EPG server was down on initial load
- You've been using the app for a while and want updated schedules
- Show search isn't finding current programs

---

## Features in Detail

### VPN Privacy Check
- Displays your current public IP address before loading any data
- Uses ipify.org (primary) and icanhazip.com (fallback) services
- **Auto-confirms if IP matches `vpn_ip` in config.json** (no prompt needed)
- Interactive prompt to confirm VPN status if IP doesn't match
- Shows warning if IP doesn't match expected VPN IP
- Option to exit gracefully if VPN is off
- Prevents accidental streaming over your real IP

### Watch Time Tracking
- Automatically tracks total viewing time for every channel
- **Only counts sessions ≥ 2 minutes** (prevents accidental clicks from inflating stats)
- Accumulates total watch duration across all sessions
- Stored in `.watch_history.json` (auto-created, includes both watch count and total duration)
- Displays top 3 most-watched channels ranked by total watch time with live EPG data
- Shows formatted watch time (e.g., "2h 15m watched", "45m watched")
- Sorted by total viewing time, then by last watched date
- Seamlessly migrates old watch history format to new time-based tracking

### New Episode Detection
Episodes are marked as "new" (`+++`) if they:
- Have an air date within the last 7 days
- Are not re-runs or old content

Based on the `<date>` field in EPG XML data.

### Timezone Handling
- EPG times are in UTC with timezone offsets (e.g., `+0000`, `-0500`)
- Automatically converts to your system's local timezone
- All displayed times reflect your local time
- "NOW PLAYING" detection accounts for timezone differences

### Search Algorithm
**Show Search:**
- Case-insensitive title matching
- Searches currently playing shows (based on start/stop times)
- Searches upcoming shows (within specified hour range)
- Sorts results: currently playing first, then by start time

**Channel Search:**
- Case-insensitive channel name matching
- Shows upcoming EPG schedule (next 5 programs)
- Highlights currently playing program

---

## Technical Details

### File Structure
```
Term-TV/
├── Term-TV.py              # Main application
├── Term-TV-VPN.py          # VPN-integrated version with OpenVPN auto-connect
├── config.json             # User configuration (playlists + optional VPN settings)
├── .watch_history.json     # Watch history with total time tracking (auto-generated)
├── .search_history.json    # Search history cache (last 5 searches, auto-generated)
├── term-tv.log             # Debug log (DEBUG level, auto-generated)
├── mpv-output.log          # MPV console output log (auto-generated)
├── openvpn.log             # OpenVPN log (Windows only, auto-generated by VPN version)
├── .epg_cache/             # EPG cache directory (auto-created)
│   ├── {hash}.xml.gz       # Cached EPG files (gzipped)
│   └── {hash}.meta         # Cache metadata (ETag, Last-Modified)
├── .m3u_cache/             # M3U cache directory (auto-created)
│   ├── {hash}.m3u          # Cached M3U playlists
│   └── {hash}.meta         # Cache metadata (ETag, Last-Modified)
├── ~/Videos/Recordings/    # Recordings directory (auto-created)
│   ├── Channel_Show_timestamp.mkv
│   └── Channel_Show_timestamp_lang.srt  # Extracted subtitles
├── working/                # Archived development files
│   └── Term-TV-ShowSearch.py
├── README.md
└── LICENSE
```

**Cache Cleanup:**
- Files in `.epg_cache/` and `.m3u_cache/` older than 15 days are automatically deleted on program exit
- Logs cleanup status and space freed

### EPG XML Format Support
Supports XMLTV format with these fields:
- `<title>` - Show/movie title
- `<sub-title>` - Episode name
- `<episode-num>` - Season/Episode (e.g., S2025E12)
- `<desc>` - Description
- `<date>` - Original air date (YYYYMMDD)
- `start` attribute - Program start time with timezone
- `stop` attribute - Program end time with timezone

### Data Models
```python
Channel = Dict[str, str]          # tvg-id, name, url, group-title (provider)
EpgData = Dict[str, List[Dict]]   # channel_id -> programs
ShowResult = Dict[str, Any]       # Search result with metadata
WatchHistory = Dict[str, Any]     # name, tvg-id, url, total_duration_seconds, watch_count
```

---

## Troubleshooting

### MPV Not Found
```
Error: 'mpv' command not found
```
**Solution**: Install mpv and ensure it's in your system PATH.

### Stream Playback Issues
If certain streams don't work but work in other players:
1. Check `mpv-output.log` for detailed error messages
2. Look for HTTP 403 (server blocking), connection timeouts, or A/V desync
3. Try alternative channel variants (e.g., "FXX" vs "FXX (alternate)")
4. Intelligent retry logic will automatically try alternatives if configured

### Recording Stops When Enabling Subtitles
This has been fixed in v2.3:
- Subtitles are now pre-loaded at recording start
- Use 'v' key to toggle subtitle visibility (safe)
- Do NOT use 'c' key to change subtitle tracks (will stop recording)

### EPG Gzip/XML Errors
```
EOFError: Compressed file ended before the end-of-stream marker was reached
ParseError: no element found
```
**Cause**: Some EPG servers generate `.xml.gz` files without properly closing the gzip stream, resulting in missing gzip footers or truncated XML content.

**Solution**: This is now handled automatically (v2.3+):
- **Lenient decompression**: Falls back to `zlib` when `gzip` fails on missing footers
- **Incremental parsing**: Uses `iterparse` to recover 99.9%+ of EPG data even from truncated XML
- **Auto-recovery**: Processes all complete `<programme>` and `<channel>` elements, skipping only incomplete fragments at the end

**Result**: EPG files that fail in strict parsers (browsers, other apps) work seamlessly in Term-TV. If you see this error, just rerun the program - it will automatically recover the data.

### No EPG Data
```
Warning: No EPG data loaded. Show search will not work properly.
```
**Solution**: Check that `epg_url` in `config.json` is accessible and valid.

### Timezone Issues
If times don't match your local time:
- Check system timezone settings
- EPG must include timezone offsets in XMLTV format

### No Channels Found
```
Could not load any channels. Exiting.
```
**Solution**: Verify `m3u_url` is accessible and properly formatted.

### VPN Connection Issues (Term-TV-VPN.py only)
```
❌ VPN connection failed!
```
**Solutions**:
1. Check `openvpn.log` (Windows) or terminal output for error details
2. Ensure OpenVPN is installed and in PATH
3. Verify `.ovpn` config file path is correct in `config.json`
4. Run with administrator/root privileges (required for VPN)
5. Test `.ovpn` file manually: `openvpn --config your-profile.ovpn`

### Debugging with Logs
The application creates detailed logs for troubleshooting:
- **term-tv.log**: DEBUG level logs with function names, line numbers, and timestamps
- **mpv-output.log**: Complete mpv console output including errors, warnings, and stream info
- **openvpn.log**: VPN connection logs (Windows only, Term-TV-VPN.py)

Check these logs when experiencing issues.

---

## Development

### Recent Changes
- **v2.3** (2025-12-07)
  - **Comprehensive Logging System**:
    - DEBUG level logs to `term-tv.log` for diagnostics
    - WARNING level logs to console for important messages
    - Complete MPV output logging to `mpv-output.log` with command details and exit codes
    - Filtered terminal output (hides verbose status lines while logging everything)
  - **Robust EPG Parsing**: Handles malformed/truncated EPG files from misconfigured servers
    - Lenient gzip decompression (tolerates missing gzip footers via `zlib` fallback)
    - Incremental XML parsing with `iterparse` (recovers 99.9%+ data from truncated XML)
    - Extended download timeout: 10s connect + 120s read (was 10s total)
    - Auto-recovery: processes all complete elements, skips only incomplete fragments
  - **M3U Playlist Caching**: Hash-based caching matching EPG format
    - Conditional HTTP requests (ETag/Last-Modified)
    - Only re-downloads when playlist changes
    - Falls back to cache on network errors
  - **Automatic Cache Cleanup**: Removes files older than 15 days on program exit
  - **Playlist Selection Enhancement**: 15-second countdown timer auto-selects playlist 1
  - **Search History Cache**: Shows last 5 successful searches for quick re-searching
  - **Recording Subtitle Fix**: Pre-loads subtitles to prevent recording from stopping
    - Subtitles loaded at start with `--sid=auto --no-sub-visibility`
    - Users can toggle visibility with 'v' key (safe)
    - Prevents recording stop when changing subtitle tracks
  - **Multi-Channel Variant Support**: Shows all channel variants with same tvg-id
    - Fixed: "FXX" and "FXX (alternate)" both appear in search results
    - Allows choosing working variant when one fails
  - **Intelligent Retry Logic**: 5-tier automatic failover for failed streams
    - Tier 1: Retry original URL once
    - Tier 2: Search for same episode on alternative channels/providers
    - Tier 3: Try each alternative
    - Tier 4: Search for future reruns (next 24h)
    - Tier 5: Auto-reschedule if rerun found
  - **Term-TV-VPN.py**: New VPN-integrated version with OpenVPN auto-connect
    - Cross-platform support (Windows, macOS, Linux)
    - Auto-detects OpenVPN executable
    - Connects on startup, disconnects on exit
    - Graceful degradation if VPN not configured

- **v2.2** (2025-11-23)
  - **Watch Time Leaderboard**: Changed from watch count to total viewing time tracking (e.g., "2h 15m watched")
  - **Provider Labels**: All channels now display their provider/source in brackets (e.g., [MoveOnJoy], [A1xmedia US Channels])
  - **EPG Caching**: Implemented smart caching with conditional HTTP requests (ETag/Last-Modified headers)
    - EPG files stored in `.epg_cache/` directory as gzipped files
    - Only re-downloads when EPG is updated on server
    - Falls back to cached version on network errors
  - **Scheduled Playback**: Future shows can auto-launch in mpv when they start (default option)
  - **Scheduled Recording**: Schedule recordings for shows that haven't started yet
  - **Scheduled Tasks Display**: Main menu shows pending scheduled playbacks/recordings with countdown timers
  - **Recording Enhancements**:
    - Record while watching or schedule for later
    - Auto-extract subtitles from recordings to SRT files
    - Recordings saved to `~/Videos/Recordings/` with metadata-based filenames
  - **Duplicate Channel Fix**: Uses unique URLs instead of duplicate tvg-ids for channel matching
  - **Watch History Migration**: Seamlessly upgrades old watch history format to include total_duration_seconds

- **v2.1** (2025-11-20)
  - Added VPN check with public IP display on launch
  - Changed from "Recently Watched" to "Frequently Watched" (sorted by watch count)
  - Added duration tracking (only counts sessions ≥ 2 minutes)
  - Auto-load playlist if only one is configured
  - Enhanced watch history with frequency statistics

- **v2.0** (2025-01-20)
  - Merged show search and channel search into unified interface
  - Added watch history tracking
  - Implemented new episode detection (`+++` markers)
  - Added proper timezone conversion (UTC → Local)
  - Enhanced EPG metadata extraction (subtitles, episode numbers, air dates)
  - Improved UX with default show search and quick-launch options
  - Archived legacy Term-TV-ShowSearch.py

- **v1.0** (Original)
  - Basic M3U playlist loading
  - EPG support
  - Channel search
  - MPV integration

### Contributing
Contributions welcome! This project focuses on:
- Simple, readable Python code
- No external dependencies beyond `requests`
- Command-line interface (no GUI)
- Feature-rich without complexity

---

## License

MIT License - See [LICENSE](LICENSE) file for details.

---

## Credits

**Primary Author**: Zach
**Original Concept**: pasiegel ([github.com/pasiegel](https://github.com/pasiegel))

Built with Python 3, `requests`, and `mpv`.

---

## Screenshots

### Frequently Watched Display
```
================================================================================
FREQUENTLY WATCHED:
================================================================================
1. NFL Channel [MoveOnJoy] (2h 15m watched)
   NOW PLAYING: NFL Daily (S2025E12) - "Week 12 Preview" +++

2. AMC HD [A1xmedia US Channels] (1h 30m watched)
   NOW PLAYING: Breaking Bad (S03E05) - "Mas"
```

### Scheduled Tasks Display
```
================================================================================
SCHEDULED TASKS:
================================================================================
▶ [PLAYBACK] In 25 min
   The Big Bang Theory (S04E12)
   Channel: TBS HD [MoveOnJoy]

⏺ [RECORD] In 1h 15m
   Breaking Bad (S05E14)
   Channel: AMC HD [A1xmedia US Channels]
```

### Show Search Results
```
Found 3 result(s):
--------------------------------------------------------------------------------
1. [ NOW PLAYING] 07:00 PM - NFL Daily (S2025E12) +++ ◄◄◄
   "Week 12 Preview" - NFL Channel [MoveOnJoy]

2. [   In 15 min] 07:30 PM - NFL Fantasy Football Show +++
   Channel: NFL Network [A1xmedia US Channels]
```

---

**Happy Watching! 📺**
