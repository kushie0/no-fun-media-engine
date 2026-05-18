"""nofun/smoke_tests.py — Pre-launch manual smoke test queue.

Each entry in TESTS is a step-by-step instruction for verifying a recent
feature or bug fix.  On startup (TUI watchdog mode only), pending tests are
shown one at a time as native OS dialogs before the Textual TUI opens:

  OK     → marks the test passed; dialog never reappears
  Cancel → skips for now; reappears on next launch

Passed IDs are stored in smoke_tests_passed.json beside media_engine.py.
Add new entries to TESTS when features ship.  Never reuse a retired ID.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

TESTS: list[dict] = [
    {
        'id':    'deprecated_commands_001',
        'title': 'RESCAN and REBUILD are gone — SCAN and BIGSCAN replace them',
        'instructions': (
            'RESCAN and REBUILD were renamed SCAN and BIGSCAN.\n\n'
            'To test: type RESCAN in the command bar, then type REBUILD.\n\n'
            'Pass if: both show a "NOTICE Unknown command" line in the log. '
            'Then type SCAN and confirm the scan starts normally.'
        ),
    },
    {
        'id':    'bigscan_time_gate_001',
        'title': 'BIGSCAN is blocked after 4pm; SCAN always runs',
        'instructions': (
            'This test only applies after 4pm. Skip with OK if it is before 4pm.\n\n'
            'To test: type INVENTORY, then type BIGSCAN.\n\n'
            'Pass if: the log shows a time-gate notice and BIGSCAN does not run. '
            'Then type SCAN — it should start immediately regardless of the hour.'
        ),
    },
    {
        'id':    'help_overlay_001',
        'title': 'HELP overlay: brief first, detail on second press, HOME closes it',
        'instructions': (
            'HELP was changed to a two-press toggle: brief summary first, '
            'full technical detail on the second press.\n\n'
            'To test: type HELP, then HELP again, then HELP once more, then HOME.\n\n'
            'Pass if: first press shows one brief line per command; second shows full detail; '
            'third toggles back to brief; HOME closes the overlay and restores the command bar.'
        ),
    },
    {
        'id':    'streams_full_001',
        'title': 'STREAMS opens idle, START launches workers, HELP overlay works',
        'instructions': (
            'STREAMS was changed to open idle — no streams start automatically.\n\n'
            'To test: type STREAMS. Type HELP, then HELP again, then HOME to return to the stream view. '
            'Type START, then STOP.\n\n'
            'Pass if: menu opens showing "Streams are not running". '
            'HELP shows brief then full detail. '
            'START shows port and URL rows with live workers. '
            'STOP returns the menu to the idle state.'
        ),
    },
    {
        'id':    'streams_clean_close_001',
        'title': 'Closing the app while streaming gives TouchDesigner a clean disconnect',
        'instructions': (
            'This test requires TouchDesigner to be connected to a stream. '
            'Skip with OK if it is not available.\n\n'
            'To test: type STREAMS then START. Confirm TouchDesigner is receiving video. '
            'Close the Media Engine window using the X button or Ctrl+C.\n\n'
            'Pass if: TouchDesigner shows a clean disconnect with no crash or stall.'
        ),
    },
    {
        'id':    'bigscan_async_001',
        'title': 'BIGSCAN runs in the background and the list auto-refreshes when done',
        'instructions': (
            'This test only applies before 4pm. Skip with OK if it is after 4pm.\n\n'
            'To test: type INVENTORY, then type BIGSCAN. '
            'While it is still running, type HOME and navigate freely. '
            'When the scan finishes, type INVENTORY again.\n\n'
            'Pass if: the command bar shows "BIGSCAN running…" during the scan, '
            'navigation works normally while it runs, '
            'and the inventory list is refreshed with the command bar restored after it finishes.'
        ),
    },
    {
        'id':    'scan_skips_sharepoint_ffprobe_001',
        'title': 'SCAN and BIGSCAN skip ffprobe on SharePoint files',
        'instructions': (
            'ffprobe is no longer called on files under the SharePoint/OneDrive '
            'folder during SCAN or BIGSCAN — those files may not be locally '
            'cached and would trigger slow background downloads.\n\n'
            'To test: type INVENTORY, then BIGSCAN. Watch the log while it runs.\n\n'
            'Pass if: BIGSCAN completes without any long stalls on SharePoint '
            'MP4 files, and SharePoint files still appear in the INVENTORY list '
            'without codec/resolution data. Type HOME when done.'
        ),
    },
    {
        'id':    'sharepoint_sync_no_time_gate_001',
        'title': 'SharePoint sync runs after 4pm and while the INVENTORY menu is open',
        'instructions': (
            'This test requires a sync-eligible performance. '
            'Skip with OK if none are available.\n\n'
            'To test: after 4pm, type INVENTORY and leave the menu open for about ten seconds.\n\n'
            'Pass if: SYNC lines appear in the log even though it is after 4pm '
            'and the inventory menu is still open.'
        ),
    },
    {
        'id':    'sharepoint_folder_rename_001',
        'title': 'SharePoint folder name grows as each band uploads',
        'instructions': (
            'This test requires a multi-band show night. '
            'Skip with OK if none is available.\n\n'
            'To test: open File Explorer and navigate to OneDrive / Multitracks. '
            'Watch the date folder as each band syncs.\n\n'
            'Pass if: the folder name grows incrementally as bands upload — '
            'for example "26-04-10" → "26-04-10 HORSE_GRAVE" → "26-04-10 HORSE_GRAVE + CLAY". '
            'Names over fifteen characters should collapse to acronyms. '
            'NOFUN and TBD should be excluded.'
        ),
    },
    {
        'id':    'nofun_info_history_001',
        'title': 'Re-uploading a band appends a "re-uploaded" line to _nofun_info.txt',
        'instructions': (
            'Re-upload now appends history rather than overwriting _nofun_info.txt.\n\n'
            'To test: let a band sync to SharePoint. Open _nofun_info.txt and note the '
            '"uploaded" timestamp. Then type INVENTORY, expand that show, and type REUPLOAD. '
            'Open the file again.\n\n'
            'Pass if: the original "uploaded" line is preserved and a new '
            '"re-uploaded" line appears beneath it.'
        ),
    },
    {
        'id':    'header_disk_stats_001',
        'title': 'Header shows disk usage on launch and updates after SCAN',
        'instructions': (
            'Disk stats now populate on launch from the cached database '
            'instead of waiting for the first scan.\n\n'
            'To test: launch the TUI and immediately check the header. '
            'Then type INVENTORY followed by SCAN.\n\n'
            'Pass if: C, D, and SharePoint disk usage appear before any command is typed. '
            'After the scan finishes, the stats and performance count in the header update.'
        ),
    },
    {
        'id':    'inventory_browse_001',
        'title': 'INVENTORY groups shows by date, and expanding one reveals per-band rows',
        'instructions': (
            'INVENTORY now shows one row per show night rather than one row per band.\n\n'
            'To test: type INVENTORY. Type a number to expand a multi-band show.\n\n'
            'Pass if: the list shows one combined row per date. '
            'Expanding it reveals per-band rows with status icons and file sections '
            '(Quadrants, Clips, Audio zip, Cloud). Missing components appear as badges. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'inventory_overdue_badge_001',
        'title': 'INVENTORY shows an overdue badge on shows past their lifecycle deadline',
        'instructions': (
            'This test requires a show 40+ days old with cloud files still present, '
            'or 60+ days old with raw files still present. '
            'Skip with OK if none exist.\n\n'
            'To test: type INVENTORY and look for any old shows.\n\n'
            'Pass if: the affected row displays a clock badge such as '
            '"⏰ cloud removal overdue (45d)" or "⏰ raw video overdue (65d)".'
        ),
    },
    {
        'id':    'rename_band_001',
        'title': 'RENAME corrects a band name across all files and SharePoint',
        'instructions': (
            'RENAME lets you override the auto-extracted band name for any performance. '
            'It renames quadrants, clips, audio zip, cloud files, and the SharePoint '
            'folder, and updates the encoding database.\n\n'
            'To test: type INVENTORY, expand a show, type RENAME, select a band with b1 '
            '(or b2 etc.), type a new name, then type CONFIRM.\n\n'
            'Pass if: the log shows RENAME lines for each file group, the INVENTORY '
            'list refreshes with the new band name, and the SharePoint folder is '
            'renamed to include the new name. Type HOME when done.'
        ),
    },
    {
        'id':    'sharepoint_folder_all_bands_001',
        'title': 'SharePoint folder name and INVENTORY show name include all bands',
        'instructions': (
            'The SharePoint folder name now accumulates all bands for a show night, '
            'and fully-synced folders are renamed when a new band is detected.\n\n'
            'To test: open OneDrive / Multitracks in File Explorer. '
            'Type INVENTORY in the TUI and check the show name for a multi-band night.\n\n'
            'Pass if: the INVENTORY show name includes all bands '
            '(e.g. "26-04-05_HORSE_GRAVE_CLAY", not just "26-04-05_HORSE_GRAVE"). '
            'The SharePoint folder for that date should have the same name. '
            'If the folder was previously named with only the date (e.g. "26-04-05"), '
            'it should be renamed on the next sync cycle. Type HOME when done.'
        ),
    },
    {
        'id':    'sharepoint_placeholder_001',
        'title': 'SharePoint date folder is created as soon as a .mov is detected',
        'instructions': (
            'The pipeline now creates the SharePoint date folder immediately when a '
            '.mov file appears in VenueLighting — before encoding starts, even while '
            'the file is still being recorded.\n\n'
            'To test: drop a .mov file named with a date and band (e.g. '
            '26-04-15_TESTBAND.mov) into VenueLighting and watch OneDrive / Multitracks '
            'in File Explorer. Wait one watchdog loop (a few seconds).\n\n'
            'Pass if: a folder named "26-04-15_TESTBAND" (or just "26-04-15" initially) '
            'appears in OneDrive / Multitracks, and the log shows a '
            '"SHARE   folder →" line. The folder should appear before any encoding '
            'or stability check completes.'
        ),
    },
    {
        'id':    'reupload_all_bands_async_001',
        'title': 'REUPLOAD uploads every band in a show and runs in the background',
        'instructions': (
            'This test requires a multi-band show. Skip with OK if none is available.\n\n'
            'To test: type INVENTORY, expand a multi-band show, and type REUPLOAD. '
            'While the upload is running, type HOME and navigate freely.\n\n'
            'Pass if: the log shows a REUPLOAD line for each band; '
            'the command bar shows "REUPLOAD running…"; '
            'navigation works during the upload; '
            'and the list refreshes with the command bar restored when it finishes.'
        ),
    },
    {
        'id':    'raw_expire_age_001',
        'title': 'Raw files are kept until 60 days old, not 40',
        'instructions': (
            'Raw file expiry was extended from 40 days to 60 days.\n\n'
            'To test: type INVENTORY and expand a show aged 40–60 days with raw files present. '
            'Then type HOME and type CLEANUP.\n\n'
            'Pass if: the band row shows no "raw video overdue" badge in INVENTORY, '
            'and those raw files do not appear in the cleanup queue.'
        ),
    },
    {
        'id':    'scan_progress_001',
        'title': 'SCAN shows progress in the status bar, even while INVENTORY is open',
        'instructions': (
            'SCAN now reports per-file progress in the status bar while running.\n\n'
            'To test: type INVENTORY, then type SCAN without closing the menu.\n\n'
            'Pass if: the status bar shows "SCAN · N/total (X%)" with the count incrementing '
            'while the inventory list stays visible above. '
            'When the scan finishes, the list refreshes and the status bar clears.'
        ),
    },
    {
        'id':    'test_tutorial_nonblocking_001',
        'title': 'TEST and TUTORIAL run independently of the TUI',
        'instructions': (
            'TEST and TUTORIAL now open dialogs in a background thread '
            'so the TUI stays interactive while they are open.\n\n'
            'To test: type TEST. While the dialog is visible, type a command in the TUI. '
            'Dismiss the dialog, then type TUTORIAL.\n\n'
            'Pass if: the TUI processes commands behind the TEST dialog; '
            'TUTORIAL opens with "NOFUN (1/7)" as the title and the step name in the body; '
            'and the TUI remains responsive throughout. Type HOME when done.'
        ),
    },
    {
        'id':    'home_commands_on_startup_001',
        'title': 'TEST and TUTORIAL appear in the command bar immediately on launch',
        'instructions': (
            'TEST and TUTORIAL were added to the home command bar so they '
            'no longer require opening INVENTORY first.\n\n'
            'To test: launch the app and check the command bar immediately '
            'without typing any command.\n\n'
            'Pass if: TEST and TUTORIAL appear alongside INVENTORY, STREAMS, and HELP '
            'from the moment the TUI opens.'
        ),
    },
    {
        'id':    'disk_free_pct_001',
        'title': 'Header shows free space as percent and GB, SharePoint out of 1TB',
        'instructions': (
            'Disk stats were changed from "used / total" to free percentage and free size. '
            'SharePoint uses a hardcoded 1TB total.\n\n'
            'To test: launch the TUI and check the header immediately.\n\n'
            'Pass if: C and D show "XX% free (XXXGB)" and SharePoint shows '
            '"SP: XX% free (XXXGB)" where the GB reflects remaining space.'
        ),
    },
    {
        'id':    'disk_stats_in_inventory_001',
        'title': 'Disk stats appear in the INVENTORY menu header, not the home header',
        'instructions': (
            'Disk stats were moved from the always-visible home header into the '
            'INVENTORY menu header, so the home screen stays uncluttered.\n\n'
            'To test: check the home header on launch. Then type INVENTORY.\n\n'
            'Pass if: the home header shows only performance counts, not disk usage. '
            'The INVENTORY menu header shows a second line with C, D, and SP free space.'
        ),
    },
    {
        'id':    'home_counts_on_launch_001',
        'title': 'Home header shows performance counts immediately on launch',
        'instructions': (
            'Counts are now read from a cached summary in encoding_db.json '
            'so they appear before any scan or INVENTORY command is run.\n\n'
            'To test: launch the app and check the home header immediately.\n\n'
            'Pass if: performance count and file type breakdown appear right away, '
            'with an "updated HH:MM" timestamp matching the last scan.'
        ),
    },
    {
        'id':    'inventory_scroll_to_selected_001',
        'title': 'Typing a number in INVENTORY scrolls the selected show into view',
        'instructions': (
            'This test requires at least 20 shows so the list is longer than the screen. '
            'Skip with OK if fewer shows are present.\n\n'
            'To test: type INVENTORY. Scroll down to the bottom of the list. '
            'Then type a number for a show near the top (e.g. 1).\n\n'
            'Pass if: the selected show scrolls into view automatically. Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_command_001',
        'title': 'REMASTER regenerates FULLSET WAV from the selected show\'s ZIP',
        'instructions': (
            'This test requires a show with a ZIP file already in the audio destination. '
            'Skip with OK if none is available.\n\n'
            'To test: type INVENTORY, expand a show with a ZIP (type its number), '
            'then type REMASTER.\n\n'
            'Pass if: the status bar shows "remastering…", a REMASTER log line appears, '
            'and a _FULLSET.wav file is written to the audio destination folder. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'master_before_zip_001',
        'title': 'A selected-channel master WAV is generated before the audio ZIP',
        'instructions': (
            'This test requires a performance with chan-numbered WAV files (e.g. chan29, '
            'chan31, chan32). Skip with OK if none is available.\n\n'
            'To test: let the pipeline process a new audio group from start to finish. '
            'Watch the log for MASTER and ZIPPING lines.\n\n'
            'Pass if: a MASTER line appears before the ZIPPING line for the same '
            'performance, and a _master_selected_ott.wav (or _master_selected_approx.wav) '
            'file appears in the audio destination folder alongside the ZIP. Type HOME when done.'
        ),
    },
    {
        'id':    'channel_alignment_001',
        'title': 'Cross-correlation alignment corrects per-channel timing offsets',
        'instructions': (
            'Channels are now cross-correlated against the first available channel before '
            'mixing, compensating for hardware ADC timing offsets. '
            'This test requires a FULLSET render with DEBUG output visible.\n\n'
            'To test: run uv run python -m nofun.mastering <folder> --clip 60 120 '
            'and check the DEBUG output.\n\n'
            'Pass if: a line appears showing "alignment — lags (ms): ch29:+0.0, ch31:±X.X, '
            'ch32:±X.X" and the rendered audio sounds phase-coherent with no flamming. '
            'Use --no-align to compare against the unaligned version.'
        ),
    },
    {
        'id':    'alignment_sign_fix_001',
        'title': 'Auto-alignment sign convention: positive lag = channel is trimmed',
        'instructions': (
            'The cross-correlation sign was inverted: a channel detected as leading the '
            'reference was incorrectly trimmed by zero while the reference was trimmed '
            'instead. The return value of _find_lag_samples is now negated so positive '
            '= channel lags reference = channel is trimmed.\n\n'
            'To test: run uv run python -m nofun.mastering <folder> --clip 60 120 with '
            'channels 29 and 31 present. Note the reported lag for ch29. Then run again '
            'with --offset 29:<lag_ms> to confirm the manual offset produces the same '
            'or tighter result than auto-align. Compare with --no-align to hear the difference.\n\n'
            'Pass if: auto-aligned render sounds phase-coherent (no flamming), and ch29 '
            'lag reported by auto-align is positive when ch29 arrives after ch31. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_001',
        'title': 'REEL generates a 1080×1920 vertical video with scrolling quads and FULLSET audio',
        'instructions': (
            'This test requires a show with all four quadrant MP4s and a _FULLSET.wav in '
            'the audio destination. Skip with OK if none is available.\n\n'
            'To test: type INVENTORY, expand a show that has quads + FULLSET WAV (type its '
            'number), then type REEL.\n\n'
            'Pass if: the status bar shows "REEL running", a REEL log line appears, and a '
            '_reel.mp4 file is written to the reels/ destination folder. Open the file and '
            'confirm it is 1080×1920, the four quads scroll slowly from top to bottom, and '
            'the FULLSET audio plays throughout. Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_status_visible_001',
        'title': 'REMASTER progress stays visible in status bar after returning HOME',
        'instructions': (
            'This test requires a show with a ZIP in audio_dest. '
            'Skip with OK if none is available.\n\n'
            'To test: type STATUS, expand a show, type TESTREMASTER, then immediately '
            'type HOME to exit the STATUS menu while remaster is still running.\n\n'
            'Pass if: the status bar continues to show "remaster: REMASTER …" '
            'alongside the normal idle status (e.g. "no files pending · remaster: …") '
            'until remaster completes. Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_reel_and_sharepoint_001',
        'title': 'REMASTER copies FULLSET WAV to SharePoint and logs skipped reels',
        'instructions': (
            'This test requires a show that has been processed (quads in vids_dest '
            'and a ZIP in audio_dest). Skip with OK if none is available.\n\n'
            'To test: type STATUS, expand a processed show, then type REMASTER. '
            'Observe the log for SHARE lines and any REEL-skipped warnings.\n\n'
            'Pass if: the log shows "SHARE   …_FULLSET.wav → <date-folder>" after '
            'mastering completes, and a reel video appears in vids_dest (or a '
            '"REEL skipped — missing quad(s):" warning appears if quads are absent). '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'concurrent_band_001',
        'title': 'Audio and video pipelines run concurrently for the same band',
        'instructions': (
            'This test requires a performance with both a raw .mov and unzipped '
            'channel WAVs present in the source directory. Skip with OK if none is '
            'available.\n\n'
            'To test: place (or wait for) a show with both .mov and _ch??.wav files '
            'in the source directory, then observe the status bar during processing.\n\n'
            'Pass if: the status bar shows both an "encode: …" slot and an '
            '"audio: …" slot at the same time while the band is being processed, '
            'indicating audio zipping and video encoding are running concurrently. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_progress_cb_001',
        'title': 'REEL encode shows live progress in TUI and can be interrupted by PAUSE',
        'instructions': (
            'This test requires a show with all four quad MP4s and a FULLSET WAV '
            'present. Skip with OK if none is available.\n\n'
            'To test: run REMASTER on a date that has a complete set of quads and '
            'a FULLSET WAV. Once the REEL encode starts (status bar shows '
            '"REEL  name  [1/N]"), watch the progress row at the bottom of the TUI.\n\n'
            'Pass if: the progress row ticks with frame/fps/timecode/speed data '
            'throughout the REEL encode (not blank/frozen), and typing PAUSE during '
            'the encode stops it cleanly. Type HOME when done.'
        ),
    },
    {
        'id':    'reel_heartbeat_001',
        'title': 'REEL encode emits a heartbeat log line every 5 minutes',
        'instructions': (
            'This test requires a REEL encode that takes longer than 5 minutes. '
            'Skip with OK if no such job is available.\n\n'
            'To test: run REMASTER on a full-length show. Open the rolling log file '
            '(convert_recent.log) in a text editor while the encode runs.\n\n'
            'Pass if: every 5 minutes a line appears in the log file starting with '
            '"REEL  " and containing a heart symbol (♥) along with frame, fps, '
            'timecode, and speed. Type HOME when done.'
        ),
    },
    {
        'id':    'reel_batch_summary_001',
        'title': 'REEL batch prints a summary line after all reels are finished',
        'instructions': (
            'This test requires two or more shows queued for REMASTER. '
            'Skip with OK if only one show is available.\n\n'
            'To test: run REMASTER so that multiple REEL encodes are queued. '
            'Wait for all encodes to complete.\n\n'
            'Pass if: after the last reel finishes, a line appears in the TUI log '
            'reading "REEL    batch done — N/N rendered  (Xs total)". Type HOME when done.'
        ),
    },
    {
        'id':    'reel_variable_scroll_001',
        'title': 'REEL scroll uses a 10× slower base speed with cosine-eased burst acceleration',
        'instructions': (
            'The reel scroll speed was changed: base is now 10× slower than before, '
            'with random cosine-eased bursts up to 4× base every 10–40 seconds (2–5 s hold, '
            '3.5 s ramp each side).\n\n'
            'To test: run REMASTER on any show to produce a reel. Play back the resulting '
            '_reel.mp4 at normal speed and watch the scroll motion for at least 90 seconds.\n\n'
            'Pass if: the base scroll is visibly very slow; the video occasionally drifts '
            'noticeably faster for a few seconds then eases back smoothly — with no jarring '
            'jumps at the transitions. Type HOME when done.'
        ),
    },
    {
        'id':    'reel_variable_scroll_fix_001',
        'title': 'REEL variable-scroll filter parses and renders correctly',
        'instructions': (
            'Three ffmpeg filter bugs were fixed: commas in comparison functions were '
            'misread as option separators; eval=frame (not a valid crop option) was removed; '
            'and the strip is now doubled to allow seamless y-wrap.\n\n'
            'To test: run REMASTER on any show. Watch the log for "REEL  ffmpeg spawned" '
            'followed by frame progress (not immediate failure).\n\n'
            'Pass if: the reel encode runs to completion and produces a _reel.mp4 file '
            'with visible slow scrolling. Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_fullset_mp3_001',
        'title': 'REMASTER generates FULLSET as 128 kbps MP3 instead of WAV',
        'instructions': (
            'REMASTER now writes FULLSET audio as _FULLSET.mp3 (128 kbps CBR) '
            'instead of _FULLSET.wav.\n\n'
            'To test: run REMASTER on a show that requires ZIP extraction '
            '(delete any existing FULLSET file first). Wait for REMASTER to complete.\n\n'
            'Pass if: a _FULLSET.mp3 file appears in the audio destination folder '
            '(D:\\audio\\) and no _FULLSET.wav is created. The REEL encode should '
            'still succeed using the MP3 as audio input. Type HOME when done.'
        ),
    },
    {
        'id':    'mastering_keywords_001',
        'title': 'Mastering log calls use normalised keywords (MASTER/ALIGN/WRITE/LOAD)',
        'instructions': (
            'This test requires a band with channel WAVs available for mastering. '
            'Skip with OK if none is available.\n\n'
            'To test: run REMASTER on a date that triggers the mastering path. '
            'Watch the TUI log and the rolling log file.\n\n'
            'Pass if: mastering progress appears as lines starting with MASTER, ALIGN, '
            'WRITE, or LOAD — with no lines starting with the old "Mastering:" prefix. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'log_quiet_advisory_001',
        'title': 'Status bar shows log-quiet advisory after 2 minutes of TUI silence during encode',
        'instructions': (
            'This test requires a long REEL or mastering encode (at least 3 minutes). '
            'Skip with OK if no such job is available.\n\n'
            'To test: start a long REEL encode. After the encode starts, wait at least '
            '2 minutes without any new lines appearing in the TUI log. '
            '(REEL and MASTER lines are file-only and will not appear in the TUI.)\n\n'
            'Pass if: within 60 seconds of crossing the 2-minute silence threshold, '
            'the status bar shows a yellow "log quiet N min — ffmpeg running" advisory. '
            'The advisory should clear once the encode finishes and normal log lines resume. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_libx264_fallback_001',
        'title': 'REEL encode uses libx264 instead of GPU encoder (h264_amf) to avoid silent portrait failure',
        'instructions': (
            'h264_amf silently rejected portrait (320×568) output with AVERROR(EINVAL). '
            'REEL now always encodes with libx264 -preset fast -crf 23.\n\n'
            'To test: trigger a REEL encode (run the pipeline on a show that has all four '
            'quad files and a FULLSET MP3, or use --trial-run for a short test).\n\n'
            'Pass if: the REEL encode completes successfully and a _reel.mp4 file appears '
            'in the video destination folder without any rc=-22 failure in the log. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_filter_script_001',
        'title': 'REEL uses -filter_complex_script to avoid Windows command-line length limit',
        'instructions': (
            'The variable-scroll burst schedule embedded in the filter_complex y expression '
            'can exceed 40 000 characters for long shows, pushing the full ffmpeg command '
            'past Windows CreateProcess\'s 32 767-character limit and causing AVERROR(EINVAL). '
            'REEL now writes the filter to a temp file and passes it via -filter_complex_script.\n\n'
            'To test: trigger a REEL encode on any show (trial-run is fine). Check the debug '
            'log for a line like "REEL  filter_complex_script: C:\\…\\tmpXXX.txt  (N chars)".\n\n'
            'Pass if: the encode completes successfully and the debug log confirms the script '
            'file was used. The temp file should be deleted automatically after encoding. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_sine_scroll_001',
        'title': 'REEL scroll uses a short sine-wave expression instead of burst schedule',
        'instructions': (
            'The previous burst-schedule y expression (~140 additive terms, 12 000–20 000 '
            'characters) hit ffmpeg\'s expression evaluator AST node limit, causing '
            '"[Eval] Missing \')\'" and AVERROR(EINVAL) on every encode. '
            'REEL now uses a single-term sinusoidal velocity integral: '
            'y(t) = (base·t + B/ω·(1−cos(ω·t))) % strip_h — about 50 characters.\n\n'
            'To test: trigger a REEL encode on any show (--trial-run is fine).\n\n'
            'Pass if: the encode completes without error and a _reel.mp4 appears in the '
            'video destination. No "[Eval] Missing \')\'" or rc=-22 in the log. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_skip_mp3_001',
        'title': 'REMASTER skips ZIP extract when MP3 exists; second press forces from scratch',
        'instructions': (
            'REMASTER now skips MP3 generation if the FULLSET MP3 already exists and '
            'goes straight to the reel. A second REMASTER press (while it is running) '
            'kills the current encode and restarts everything from scratch.\n\n'
            'To test: expand a show that already has FULLSET MP3s, type REMASTER. '
            'Confirm the log shows "using existing FULLSET (skipping ZIP extract)" and '
            'the reel starts immediately. Then expand a show with no MP3s, type REMASTER '
            'and confirm ZIP extraction runs. Finally type REMASTER a second time right '
            'after it starts to confirm it restarts from scratch with "forcing from scratch".\n\n'
            'Pass if: existing-MP3 path skips to reel, no-MP3 path extracts ZIP, and '
            'double-press restarts from scratch. Type HOME when done.'
        ),
    },
    {
        'id':    'script_encode_quads_001',
        'title': 'Quadrant encode uses script runner instead of inline ffmpeg',
        'instructions': (
            'The quadrant encode now runs through scripts/encode_quads.py '
            'instead of building the ffmpeg command inline in Python.\n\n'
            'To test: drop a .mov file into VenueLighting and wait for encoding to begin.\n\n'
            'Pass if: quadrant MP4s appear in D:/videos/ as before. '
            'The debug log should show "ScriptRunner: <base> → quadrants" instead of '
            'the old inline ffmpeg label. Type HOME when done.'
        ),
    },
    {
        'id':    'script_batch_silence_001',
        'title': 'Silence detection runs as one batch script instead of 32 separate ffmpeg calls',
        'instructions': (
            'Peak detection now runs scripts/detect_silence.py once for all '
            'channels instead of calling ffmpeg 32 times individually.\n\n'
            'To test: drop a multichannel .wav into VenueLighting and wait for audio splitting.\n\n'
            'Pass if: channels are split, silent ones are cleaned, and '
            'the audio ZIP appears in D:/audio/. Check the debug log for a single '
            '"ScriptRunner: silence detection (N files)" line instead of many separate '
            'ffmpeg volumedetect calls. Type HOME when done.'
        ),
    },
    {
        'id':    'jobs_menu_001',
        'title': 'JOBS command opens queue status menu',
        'instructions': (
            'A JOBS menu was added. Typing JOBS opens an overlay showing pending, '
            'running, and recently completed jobs. CANCEL <n> cancels a pending job '
            'by its list number; CLEAR removes completed entries; HOME closes the menu.\n\n'
            'To test: type JOBS in the command bar.\n\n'
            'Pass if: the JOBS overlay opens and shows either "No jobs in queue" or '
            'a list of active/completed jobs. Type HOME when done.'
        ),
    },
    {
        'id':    'inventory_no_fullset_reel_001',
        'title': 'FULLSET WAVs and reel MP4s do not appear as phantom bands in INVENTORY',
        'instructions': (
            'This test requires a show that has been REMASTER-ed (FULLSET WAVs and/or '
            'a reel MP4 exist in D:/audio/ or D:/videos/). Skip with OK if none.\n\n'
            'To test: type BIGSCAN, wait for it to finish, then type INVENTORY.\n\n'
            'Pass if: the show appears once with its real band names only — no extra '
            'entries like "MX_LONELY_FULLSET" or "PFC_PRIZE_reel", and the show name '
            'does not contain repeated band tokens. Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_via_queue_001',
        'title': 'REMASTER command enqueues a MANUAL job visible in JOBS menu',
        'instructions': (
            'This test requires a show with at least one audio ZIP on D:. '
            'Skip with OK if none is available.\n\n'
            'To test: open INVENTORY, expand a show with a ZIP, type REMASTER. '
            'Then type HOME and type JOBS.\n\n'
            'Pass if: the JOBS menu shows a REMASTER job as pending or running, '
            'and the INVENTORY row for that show shows a cyan ⚙ badge. '
            'Type REMASTER a second time while it is pending — confirm it restarts '
            'with "force" in the label. Type HOME when done.'
        ),
    },
    {
        'id':    'inventory_job_badge_001',
        'title': 'INVENTORY collapsed rows show a job-queue badge while encoding',
        'instructions': (
            'This test requires a performance that is currently encoding. '
            'Skip with OK if no encoding is in progress.\n\n'
            'To test: while encoding is running, type INVENTORY and look at the '
            'collapsed show row for the performance being processed.\n\n'
            'Pass if: a cyan "⚙ encoding (…)" or "⚙ queued N" badge appears next '
            'to the show row. Type HOME when done.'
        ),
    },
    {
        'id':    'bigscan_self_heal_001',
        'title': 'BIGSCAN automatically prunes phantom band entries from the encoding DB',
        'instructions': (
            'BIGSCAN now removes stale (date, band) entries from encoding_db.json '
            'automatically — any band key that no longer appears in the filesystem '
            'scan for that date is deleted. Only dates with files present are touched; '
            'unmounted drives are never affected.\n\n'
            'To test: if a show previously had phantom bands (e.g. MX_LONELY_FULLSET), '
            'run BIGSCAN (double-press REBUILD) and check the log. '
            'Skip with OK if no phantom bands are known.\n\n'
            'Pass if: the log shows "REBUILD  pruned N stale band entries from encoding DB" '
            'and INVENTORY no longer shows the phantom band after the scan completes.'
        ),
    },
    {
        'id':    'schedule_command_001',
        'title': 'SCHEDULE command in JOBS menu shows encode window and allows toggle',
        'instructions': (
            'A SCHEDULE command was added to the JOBS menu. It shows the current '
            'schedule rules (encode_window 00:00–16:00, sync_always, manual_always) '
            'and whether each is currently active.\n\n'
            'To test: type JOBS, then type SCHEDULE. After viewing, type '
            'SCHEDULE OFF and confirm the window shows "(disabled)". '
            'Then type SCHEDULE ON to re-enable it. Type HOME when done.\n\n'
            'Pass if: the menu shows all three rules. SCHEDULE OFF disables the '
            'encode window and the status changes to reflect it. SCHEDULE ON '
            'restores it.'
        ),
    },
    {
        'id':    'dryrun_command_001',
        'title': 'DRYRUN in JOBS menu previews the next pending job without executing',
        'instructions': (
            'A DRYRUN command was added to the JOBS menu. It displays the next '
            'pending job\'s script name and key args without running anything.\n\n'
            'To test: while a job is pending (e.g. immediately after a file is '
            'detected), type JOBS then DRYRUN. Skip with OK if no jobs are pending.\n\n'
            'Pass if: the menu shows the script name and args for the next pending '
            'job without starting execution. Type HOME when done.'
        ),
    },
    {
        'id':    'reel_via_script_001',
        'title': 'REMASTER reel encode runs through scripts/generate_reel.py',
        'instructions': (
            'The reel generation in REMASTER now runs through '
            'scripts/generate_reel.py via ScriptRunner when available.\n\n'
            'To test: trigger REMASTER on a show that has quad MP4s and a '
            'FULLSET WAV. Wait for reel generation to complete.\n\n'
            'Pass if: the _reel.mp4 file appears in the output directory. '
            'The debug log should show "REEL  ScriptRunner: generate_reel" '
            'instead of the old "REEL  ffmpeg spawned" label. Type HOME when done.'
        ),
    },
    {
        'id':    'mp3_via_script_001',
        'title': 'REMASTER MP3 transcode runs through scripts/transcode_mp3.py',
        'instructions': (
            'The WAV→MP3 transcode in REMASTER now runs through '
            'scripts/transcode_mp3.py via ScriptRunner when available.\n\n'
            'To test: trigger REMASTER and wait for MP3 output files.\n\n'
            'Pass if: MP3 files appear in the audio destination. '
            'The debug log should show "ScriptRunner: transcode_mp3" lines. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'jobs_per_band_001',
        'title': 'JOBS menu shows one row per band for REMASTER',
        'instructions': (
            'REMASTER now enqueues one job per band instead of a single '
            'all-bands job. Each band can be cancelled independently from JOBS. '
            'PAUSE now stops between bands, and the manual worker thread has '
            'been removed — all jobs dispatch in the same pipeline thread.\n\n'
            'To test: type REMASTER on a date with two or more bands, then '
            'open JOBS. Observe the job list.\n\n'
            'Pass if: each band appears as a separate row (e.g. "PRIZE REMASTER", '
            '"CLAY REMASTER"). Type CANCEL <n> on a pending band to verify '
            'cancellation works. Type HOME when done.'
        ),
    },
    {
        'id':    'jobs_label_band_001',
        'title': 'Automatic encode jobs show band name in JOBS menu',
        'instructions': (
            'Automatic encode and audio jobs now use the band name as the '
            'label ("PRIZE REENCODE", "PRIZE AUDIO") instead of the full '
            'file stem ("26-04-07_PRIZE.0 → video").\n\n'
            'To test: introduce a new source MOV/WAV file and open JOBS '
            'while encoding is queued.\n\n'
            'Pass if: the job rows read "BANDNAME REENCODE" and '
            '"BANDNAME AUDIO". Type HOME when done.'
        ),
    },
    {
        'id':    'full_dag_001',
        'title': 'Full 8-stage lifecycle runs encode→clips→sync→remaster→reel→sync',
        'instructions': (
            'Precondition: a performance with at least one .mov and one .wav in '
            'search_dir. The lifecycle now has 8 stages.\n\n'
            'To test: type NOPROBLEM to allow processing, then type JOBS to watch '
            'the queue. Observe each stage appear and complete in order.\n\n'
            'Pass if: all 8 stages — ENCODE, CLIPS, SPLIT, SYNC QUADS, SYNC AUDIO, '
            'REMASTER, REEL, SYNC REEL — appear in JOBS and execute without errors. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_separate_job_001',
        'title': 'Reel generation is a separate GPU job, not bundled with remaster',
        'instructions': (
            'Reel generation was decoupled from _do_remaster_for_band and is now '
            'its own GPU_BOUND job in the lifecycle manifest.\n\n'
            'To test: watch JOBS during a full pipeline run after REMASTER completes.\n\n'
            'Pass if: a separate REEL row appears in the JOBS list after REMASTER '
            'completes, tagged [GPU]. Type HOME when done.'
        ),
    },
    {
        'id':    'jobs_progress_001',
        'title': 'Running encode job shows live frame/fps progress in JOBS menu',
        'instructions': (
            'The JOBS menu now shows per-frame progress for actively running encode jobs.\n\n'
            'To test: start processing a performance, then type JOBS. Look at the '
            'running job row.\n\n'
            'Pass if: a second line appears below the running job showing '
            '"◎ frame NNNNN  fps NN.N  HH:MM:SS  Nx". Values should update each '
            'second. Type HOME when done.'
        ),
    },
    {
        'id':    'reprocess_staging_cleanup_001',
        'title': 'REPROCESS staging directory is removed after app restarts',
        'instructions': (
            'After a REPROCESS job completes or the app exits, the '
            '_reprocess_staging/ directory should be cleaned up automatically.\n\n'
            'To test: type REPROCESS and select a performance. After processing '
            'completes, restart the app. Check the process-videos/ directory.\n\n'
            'Pass if: no _reprocess_staging/ directory exists after restart. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'parallel_clip_export_001',
        'title': 'Clip export runs all 4 quads in parallel instead of sequentially',
        'instructions': (
            'This test requires a performance that has not yet had clips exported. '
            'export_clips.py now uses a ThreadPoolExecutor to run UL/UR/LL/LR simultaneously.\n\n'
            'To test: process a new show through to the CLIPS stage and watch the log. '
            'You should see ffmpeg_pid lines for multiple quads appearing interleaved '
            'rather than UL finishing completely before UR starts.\n\n'
            'Pass if: clips complete in roughly 1/4 the previous time, and the clips '
            'directory is fully populated. Type HOME when done.'
        ),
    },
    {
        'id':    'inventory_time_ago_001',
        'title': 'Inventory panel shows relative time ("X minutes ago") instead of clock time',
        'instructions': (
            'The inventory panel now shows how long ago the scan ran rather than the '
            'wall-clock time it ran at.\n\n'
            'To test: type INVENTORY to force a scan, then wait a minute and watch the '
            'bottom-right of the inventory panel.\n\n'
            'Pass if: the label reads "updated X seconds ago" immediately after the scan '
            'and increments to "1 minute ago", "2 minutes ago", etc. over time. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'clip_progress_counter_001',
        'title': 'Progress row shows X/T clips during clip export',
        'instructions': (
            'This test requires a performance that has not yet had clips exported. '
            'The progress row now shows "clips X/T" counting up as each clip finishes.\n\n'
            'To test: let the pipeline reach the clip export stage for a new show and '
            'watch the progress row (above the command input).\n\n'
            'Pass if: the row shows "clips 1/N", "clips 2/N", etc. counting up to N, '
            'then clears when done. Type HOME when done.'
        ),
    },
    {
        'id':    'audio_cleanup_loop_001',
        'title': 'Audio job does not re-run when all channels are already queued for deletion',
        'instructions': (
            'This test requires a show whose _chan*.wav files are already in audio_archive '
            '(duplicates). Previously, _archive_or_dedup added them to delete_queue instead '
            'of moving them, so they stayed in VenueLighting/ and the AUDIO job looped.\n\n'
            'To test: let the pipeline process a show where the _chan*.wav files are already '
            'in D:\\audio_archive\\. Watch whether the AUDIO job re-queues after finishing.\n\n'
            'Pass if: the AUDIO job runs once, logs "dropped N silent channels", and does '
            'not appear again in the JobQueue log for that band. Type HOME when done.'
        ),
    },
    {
        'id':    'reel_after_fullset_001',
        'title': 'REEL runs immediately after REMASTER completes for the same band',
        'instructions': (
            'REEL is now MANUAL category (same worker as REMASTER) so each band '
            'finishes fully — REMASTER then REEL — before the next band starts.\n\n'
            'To test: trigger REMASTER for a show that has a ZIP in D:\\audio\\. '
            'Watch the JOBS menu. After the REMASTER job finishes for a band, REEL '
            'should start for that same band before any other band\'s REMASTER runs.\n\n'
            'Pass if: REMASTER and REEL for Band A complete back-to-back in JOBS '
            'before Band B\'s REMASTER begins. Type HOME when done.'
        ),
    },
    {
        'id':    'jobs_menu_expanded_finish_001',
        'title': 'JOBS menu does not crash when an expanded job finishes',
        'instructions': (
            'Previously the 1-second _tick_live_menu timer could race with the job '
            'completion callback while a job was expanded, causing rapid concurrent '
            'set_rows() calls. Now the timer skips rebuilding while a job is expanded.\n\n'
            'To test: open the JOBS menu and expand a running job by typing its number. '
            'Let the job finish naturally while the expanded view is visible.\n\n'
            'Pass if: the expanded detail disappears cleanly after the job finishes, '
            'the app does not crash, and the menu collapses to the normal list. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'audio_all_silent_db_skip_001',
        'title': 'AUDIO job is not re-enqueued for a show where every channel was silent',
        'instructions': (
            'This test requires a show whose _chan*.wav files keep reappearing in '
            'VenueLighting/ (e.g. restored by an external sync process) after the pipeline '
            'archives all channels as silent. Previously the AUDIO job would loop every '
            '~20 minutes; now the encoding DB records the "audio_all_silent" flag and '
            'the manifest builder skips re-creating the job.\n\n'
            'To test: wait for a show to log "dropped N silent channels" with no ZIP '
            'created, then check encoding_db.json for an "audio_all_silent" entry for '
            'that band. Leave the pipeline running for two full loop cycles (~30 s each).\n\n'
            'Pass if: the AUDIO job does not reappear in the job queue for that band on '
            'subsequent loops. Type HOME when done.'
        ),
    },
    {
        'id':    'batch_queue_001',
        'title': 'Batch mode (-d) uses queue workers, not ThreadPoolExecutor',
        'instructions': (
            'Batch mode (-d) now enqueues work via _job_queue and processes it '
            'with the same GPU/CPU worker threads as TUI mode.\n\n'
            'To test: run python media_engine.py -d test_files/ -t 5 and watch '
            'the log. Look for "JobQueue: enqueued" and "JobQueue: start" lines '
            'in convert_recent.log.\n\n'
            'Pass if: the log shows jobs enqueued and dispatched by named workers '
            '(gpu-worker / cpu-worker) with no ThreadPoolExecutor tracebacks. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'rename_queue_001',
        'title': 'RENAME appears in JOBS menu while running',
        'instructions': (
            'RENAME is now dispatched as a MANUAL queue job instead of a raw '
            'background thread.\n\n'
            'To test: open INVENTORY, expand a show with at least one band, type '
            'RENAME, select a band (b1), enter a new name, type CONFIRM. '
            'Immediately open JOBS before the rename finishes.\n\n'
            'Pass if: a RENAME job (e.g. "26-04-15 BAND → NEWNAME RENAME") appears '
            'in the JOBS menu with the band names in its label. Type HOME when done.'
        ),
    },
    {
        'id':    'resolution_preflight_001',
        'title': 'Undersized source files are rejected with a clear ALERT before encoding',
        'instructions': (
            'A pre-flight dimension check was added to the encode step. '
            'It catches sources whose quadrant dimensions fall below the '
            'active encoder minimum and logs ALERT instead of failing silently.\n\n'
            'To test: type INVENTORY and confirm that recent show encodes completed '
            'normally. Then open the log panel and confirm no ALERT lines appear '
            'for healthy source files.\n\n'
            'Pass if: no ALERT lines are present for files that encoded successfully. '
            'If any ALERT does appear, the log will name the file, its quadrant size, '
            'and the encoder minimum — confirming the message is actionable. Type HOME when done.'
        ),
    },
    {
        'id':    'quality_preflight_001',
        'title': 'Raw file expiry skips deletion when quadrant or ZIP probe fails',
        'instructions': (
            'The EXPIRE RAW FILES scheduled task now probes quadrant MP4s and '
            'ZIP archives before deleting raw .mov and .wav files. '
            'A corrupt or empty output causes the raw file to be kept and a '
            'warning logged instead of being silently deleted.\n\n'
            'To test: wait for the next EXPIRE RAW FILES run (logged hourly) or '
            'let the pipeline run overnight. Then check the log for any '
            '"EXPIRE skipped" lines.\n\n'
            'Pass if: DELETE lines in the log are accompanied by "quads verified" '
            'or "ZIP verified" (not just "quads exist" / "ZIP exists"). '
            'No raw files should be deleted without a successful probe. Type HOME when done.'
        ),
    },
]

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _state_path(script_dir: pathlib.Path) -> pathlib.Path:
    return script_dir / 'smoke_tests_passed.json'


def _load_passed(script_dir: pathlib.Path) -> set[str]:
    try:
        return set(json.loads(_state_path(script_dir).read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return set()


def _save_passed(script_dir: pathlib.Path, passed: set[str]) -> None:
    _state_path(script_dir).write_text(
        json.dumps(sorted(passed), indent=2) + '\n'
    )

# ---------------------------------------------------------------------------
# OS-native dialog
# ---------------------------------------------------------------------------

def _show_dialog(title: str, message: str) -> bool:
    """Show a blocking OS dialog. Returns True if the user clicked OK."""
    platform = str(sys.platform)  # str() prevents Pyright from narrowing cross-platform branches

    if platform == 'darwin':
        # Escape for AppleScript string literal
        safe_msg   = message.replace('\\', '\\\\').replace('"', '\\"')
        safe_title = title.replace('\\', '\\\\').replace('"', '\\"')
        script = (
            f'display dialog "{safe_msg}" '
            f'with title "{safe_title}" '
            f'buttons {{"Cancel", "OK"}} '
            f'default button "OK"'
        )
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
        )
        return result.returncode == 0
    elif platform == 'win32':  # pyright: ignore[reportUnreachable]
        import ctypes
        MB_OKCANCEL = 1
        IDOK        = 1
        rc = ctypes.windll.user32.MessageBoxW(0, message, title, MB_OKCANCEL)  # pyright: ignore[reportAttributeAccessIssue]
        return rc == IDOK
    else:  # pyright: ignore[reportUnreachable]
        # Fallback for Linux / headless: try tkinter, silently skip if unavailable
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            ok = messagebox.askokcancel(title, message)
            root.destroy()
            return bool(ok)
        except Exception:
            return True  # no display — treat as passed so we don't block forever

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_smoke_tests(script_dir: pathlib.Path) -> int:
    """Show any pending smoke test dialogs, one at a time.

    Called from a background thread at TUI startup and from the TEST command.
    Returns the number of pending tests that were shown.
    """
    passed  = _load_passed(script_dir)
    pending = [t for t in reversed(TESTS) if t['id'] not in passed]
    if not pending:
        return 0

    total = len(pending)
    for i, test in enumerate(pending, start=1):
        ok = _show_dialog(
            title=f"NOFUN ({i}/{total})",
            message=f"{test['title']}\n\n{test['instructions']}",
        )
        if ok:
            passed.add(test['id'])
            _save_passed(script_dir, passed)  # save after each OK so crashes don't lose progress

    return total


# ---------------------------------------------------------------------------
# Tutorial
# ---------------------------------------------------------------------------

TUTORIAL_STEPS: list[dict] = [
    {
        'title': 'Welcome',
        'text': (
            'This pipeline watches VenueLighting for new video and audio files and '
            'automatically encodes quadrants, exports proxy clips, splits audio channels, '
            'and syncs finished files to SharePoint. It mostly runs itself.\n\n'
            'This tour covers the seven commands you will use on show night. '
            'Cancel any step to exit early.\n\n'
            'From HOME, type HELP to see every available command.'
        ),
    },
    {
        'title': 'The Header',
        'text': (
            'The top-right panel shows disk usage for the C drive, D drive, and SharePoint, '
            'plus a count of known performances. '
            'These populate from the cached database on launch '
            'and refresh automatically after each scan.\n\n'
            'From HOME, type INVENTORY. '
            'Confirm that disk stats appear in the header. '
            'Type HOME to return.'
        ),
    },
    {
        'title': 'INVENTORY',
        'text': (
            'INVENTORY lists all known performances grouped by show night. '
            'Each row is one date. Multi-band shows appear as a single combined row. '
            'Expanding a row shows per-band file sections — Quadrants, Clips, Audio zip, '
            'and Cloud — with badges for anything missing or overdue.\n\n'
            'From HOME, type INVENTORY. '
            'Type a row number to expand a show and review its file sections. '
            'Type HOME to return.'
        ),
    },
    {
        'title': 'SCAN',
        'text': (
            'SCAN probes the source directory for new or changed files and updates the '
            'inventory database. Progress appears in the status bar as "SCAN · N/total (X%)". '
            'You can run SCAN while INVENTORY is open — the list refreshes when it finishes.\n\n'
            'From INVENTORY, type SCAN without closing the menu. '
            'Watch the status bar count up. '
            'Confirm the list refreshes when the scan completes. '
            'Type HOME to return.'
        ),
    },
    {
        'title': 'REUPLOAD',
        'text': (
            'REUPLOAD copies a show\'s processed files to SharePoint manually. '
            'On a multi-band show it uploads every band at once. '
            'It runs in the background, so you can navigate freely while it works. '
            'The _nofun_info.txt file in the SharePoint folder records the full upload history.\n\n'
            'From HOME, type INVENTORY, expand a show, and type REUPLOAD. '
            'While the upload runs, type HOME. '
            'Confirm the list refreshes automatically when the upload finishes.'
        ),
    },
    {
        'title': 'STREAMS',
        'text': (
            'STREAMS manages the HTTP video stream server for TouchDesigner. '
            'The menu opens idle — streams only start when you type START. '
            'Exiting the menu with HOME leaves streams running. '
            'Type STOP inside the menu to shut them down.\n\n'
            'From HOME, type STREAMS. '
            'Type START to launch the stream workers and confirm port and URL rows appear. '
            'Type HOME to exit while streams stay live. '
            'Type STREAMS then STOP to shut down.'
        ),
    },
    {
        'title': 'PAUSE and RESUME',
        'text': (
            'The first PAUSE lets the current encode finish before stopping the pipeline. '
            'A second PAUSE hard-kills ffmpeg immediately and moves any partial files '
            'to a hard_paused folder for later recovery. '
            'RESUME continues from either pause state.\n\n'
            'From HOME, between encodes, type PAUSE. '
            'Confirm the status bar shows PAUSED. '
            'Type RESUME and confirm the pipeline restarts.'
        ),
    },
    {
        'title': 'HELP',
        'text': (
            'Every screen has a HELP command. '
            'The first press shows a brief one-liner per command. '
            'A second press switches to full technical detail. '
            'HOME dismisses the overlay from any screen.\n\n'
            'That is the tour. The pipeline handles encoding and syncing on its own — '
            'check INVENTORY on show night and open STREAMS when TouchDesigner needs a feed.\n\n'
            'From HOME, type HELP, then HELP again to see full detail, then HOME to finish.'
        ),
    },
    {
        'id':    'scriptrunner_stall_watchdog_001',
        'title': 'ScriptRunner kills a stalled subprocess after 5 minutes of no output',
        'instructions': (
            'ScriptRunner now uses a background reader thread so it can detect '
            'when a subprocess (e.g. the h264_amf encoder) stops producing any '
            'stderr output. If no bytes arrive for 10 seconds the process is killed '
            'and retried once; if it stalls again the job is marked failed and the '
            'pipeline moves on.\n\n'
            'To test: this is hard to trigger manually. Instead, confirm the guard is '
            'present by checking the log after any clips encode — it should complete '
            'normally. If a GPU encoder hangs in future, look for '
            '"no output for 10s — killing stalled process (will retry)" followed by '
            '"retrying (attempt 2/2)" in the log.\n\n'
            'Pass if: clips encode completes and the pipeline continues to the next '
            'file without freezing. Type HOME when done.'
        ),
    },
    {
        'id':    'silent_channel_summary_log_001',
        'title': 'TUI shows one summary line for silent/kept channels instead of one line each',
        'instructions': (
            'Silent and active channels are now summarised into a single INFO line '
            '("dropped N silent … kept N with signal …") instead of one PENDING/CREATE '
            'line per channel. Per-channel detail is still written to the log file at DEBUG.\n\n'
            'To test: process a multichannel WAV that has at least one silent channel. '
            'Watch the TUI log panel during the silence-check phase.\n\n'
            'Pass if: exactly one summary line appears (e.g. "Audio: … — dropped 3 silent '
            '(−82.0–−81.5 dB), kept 5 with signal (−18.3–−12.1 dB)") with no individual '
            'PENDING lines visible in the TUI. Open the rolling log file and confirm the '
            'per-channel PENDING lines are still present there. Type HOME when done.'
        ),
    },
    {
        'id':    'auto_remaster_after_encode_001',
        'title': 'REMASTER and REEL trigger automatically after encode + audio complete',
        'instructions': (
            'After all AUTOMATIC jobs (REENCODE + AUDIO) for a band finish, the '
            'pipeline now auto-enqueues a REMASTER job (which also renders the REEL). '
            'Each band is only auto-enqueued once per session; if a FULLSET MP3 already '
            'exists the band is skipped. Manual REMASTER still works as before.\n\n'
            'To test: drop a .mov and channel WAVs for a new band into VenueLighting '
            'and wait for encoding and audio ZIP to complete. Watch the JOBS menu.\n\n'
            'Pass if: a REMASTER job for that band appears automatically in the JOBS '
            'menu after the AUDIO job completes, and the FULLSET MP3 and REEL file '
            'appear in the audio folder when it finishes. Type HOME when done.'
        ),
    },
    {
        'id':    'zip_parallel_compress_001',
        'title': 'ZIP archiving uses all CPU cores for maximum throughput',
        'instructions': (
            'Audio ZIP creation now compresses all channel WAVs in parallel '
            'using one thread per CPU core (zlib releases the GIL), then writes '
            'the pre-deflated bytes directly into the zip to avoid a second pass.\n\n'
            'To test: wait for a show night with multiple channel WAVs to be archived. '
            'Open Task Manager during the ZIPPING phase.\n\n'
            'Pass if: CPU utilization is high across multiple cores during zipping '
            'and the resulting ZIP opens correctly in Windows Explorer or 7-Zip. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'jobs_home_during_ffmpeg_001',
        'title': 'HOME closes the JOBS menu immediately even while ffmpeg is running',
        'instructions': (
            'The JOBS menu now closes as soon as HOME is typed, without waiting '
            'for the current ffmpeg encode to finish draining the command queue.\n\n'
            'To test: open the JOBS menu while an encode is running (no job row '
            'selected, no HELP overlay open). Type HOME.\n\n'
            'Pass if: the JOBS overlay disappears and the home command bar is '
            'restored immediately, while the encode continues in the background. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'mp3_stall_fix_001',
        'title': 'MP3 transcode no longer triggers the 10s stall detector',
        'instructions': (
            'transcode_mp3.py now passes -progress pipe:2 -nostats to ffmpeg '
            'and emits a heartbeat line to stderr after encoding, so the '
            'ScriptRunner stall timer does not fire during a long WAV→MP3 encode.\n\n'
            'To test: trigger a REMASTER for a show with a large ZIP (a full set, '
            'not a trial run) and watch the log.\n\n'
            'Pass if: the MP3 transcode completes without any "no output for 10s" '
            'warning in the log. Type HOME when done.'
        ),
    },
    {
        'id':    'reel_channel_date_filter_001',
        'title': 'Auto-REMASTER only runs for shows ≤7 days old with room + board channels',
        'instructions': (
            'Auto-REMASTER now skips shows older than 7 days and skips shows '
            'whose ZIP lacks at least one room channel (29 or 30) AND one board '
            'channel (31 or 32).\n\n'
            'To test: let the pipeline auto-detect a completed show. Confirm in '
            'the log that only shows from the last 7 days trigger "AUTO REMASTER". '
            'Optionally inspect a ZIP with only room or only board channels — it '
            'should show "skip REMASTER … missing room or board" in debug log.\n\n'
            'Pass if: no AUTO REMASTER jobs appear for shows older than 7 days '
            'and no REEL is rendered for shows with incomplete channel sets. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'orphan_pid_001',
        'title': 'Stall-kill terminates the ffmpeg grandchild',
        'instructions': (
            'Orphan PID tracking now kills the ffmpeg grandchild when '
            'ScriptRunner stalls or when PAUSE PAUSE (hard stop) is issued.\n\n'
            'To test: start a REEL or REENCODE and trigger PAUSE PAUSE (hard '
            'stop) while ffmpeg is actively encoding. Wait 3 seconds.\n\n'
            'Pass if: running `ps aux | grep ffmpeg` (or Task Manager on '
            'Windows) shows no orphaned ffmpeg process for that encode. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'dual_lane_001',
        'title': 'GPU and CPU jobs dispatch simultaneously via worker threads',
        'instructions': (
            'GPU and CPU worker threads now dispatch independently so video '
            'encodes and audio processing overlap in time.\n\n'
            'To test: trigger NOPROBLEM on a show with both .mov and .wav '
            'files. Watch the status bar carefully.\n\n'
            'Pass if: both "encode:" and "audio:" operation slots appear '
            'active at the same time in the status bar. Type JOBS to confirm '
            'both jobs show as running. Type HOME when done.'
        ),
    },
    {
        'id':    'gpu_cpu_categories_001',
        'title': 'Video jobs enqueue as GPU_BOUND, audio jobs as CPU_BOUND',
        'instructions': (
            'Video and audio jobs are now tagged with separate queue categories '
            '(GPU_BOUND and CPU_BOUND) so they can be dispatched to independent '
            'worker lanes in a future step.\n\n'
            'To test: trigger NOPROBLEM on a show with both .mov and .wav files, '
            'then type JOBS.\n\n'
            'Pass if: video jobs (REENCODE) and audio jobs (AUDIO) both appear in '
            'the queue and process correctly to completion. Type HOME when done.'
        ),
    },
    {
        'id':    'full_lifecycle_manifest_001',
        'title': 'REMASTER triggers automatically after encode + audio complete',
        'instructions': (
            'Video and audio encode jobs now live in a single lifecycle manifest. '
            'REMASTER is queued as a dependent job that only runs after both '
            'encode and audio complete for the same performance.\n\n'
            'To test: with a show that has both .mov and .wav files, let the '
            'pipeline run. Open JOBS and watch the queue.\n\n'
            'Pass if: after REENCODE and AUDIO jobs finish, a REMASTER job '
            'appears and starts automatically. Type HOME when done.'
        ),
    },
    {
        'id':    'reprocess_001',
        'title': 'REPROCESS lists archived performances',
        'instructions': (
            'The REPROCESS command scans video_archive/ and audio_archive/ for '
            'raw files and presents a selection menu.\n\n'
            'To test: type REPROCESS from the HOME screen.\n\n'
            'Pass if: a numbered list of archived performances appears. '
            'Type HOME to cancel.'
        ),
    },
    {
        'id':    'reprocess_002',
        'title': 'REPROCESS runs full pipeline on selected performance',
        'instructions': (
            'Selecting a number in REPROCESS stages the archived files and '
            'enqueues a full lifecycle manifest.\n\n'
            'To test: type REPROCESS, select a performance, then type JOBS '
            'to watch the pipeline progress.\n\n'
            'Pass if: lifecycle stage jobs (REENCODE, AUDIO, REMASTER) '
            'appear in the JOBS queue and execute in order. Type HOME.'
        ),
    },
    {
        'id':    'scheduled_jobs_001',
        'title': 'SYNC and EXPIRE appear as SCHEDULED entries in JOBS',
        'instructions': (
            'Sync and expiry operations are now dispatched as SCHEDULED jobs '
            'so they show up in the JOBS queue rather than running silently.\n\n'
            'To test: type JOBS and wait up to 30 seconds after the TUI starts.\n\n'
            'Pass if: SYNC PERFORMANCES, EXPIRE CLOUD SHARES, and EXPIRE RAW FILES '
            'entries appear in the JOBS list (running or done). Type HOME when done.'
        ),
    },
    {
        'id':    'bigscan_in_jobs_001',
        'title': 'BIGSCAN and SCAN appear in the JOBS queue',
        'instructions': (
            'BIGSCAN and SCAN are now dispatched through the SCHEDULED job lane '
            'instead of a bare background thread, so they appear in JOBS like any '
            'other queued operation.\n\n'
            'To test: type BIGSCAN (or SCAN), then immediately type JOBS.\n\n'
            'Pass if: a "BIGSCAN: probe files" (or "SCAN: probe files") entry '
            'appears in the JOBS list with a running or done status. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reencode_park_001',
        'title': 'REENCODE parks after 3 immediate failures instead of looping forever',
        'instructions': (
            'This test requires a corrupt .mov that cannot be encoded (e.g. missing moov atom). '
            'Skip with OK if none is available.\n\n'
            'To test: drop a corrupt .mov into VenueLighting, then wait for encoding to start. '
            'Watch the log.\n\n'
            'Pass if: after exactly 3 failed REENCODE attempts the log shows an ALERT line '
            'saying "3 immediate REENCODE failures; not retrying" and the pipeline stops '
            'retrying that file. Type HOME when done.'
        ),
    },
    {
        'id':    'clip_seek_001',
        'title': 'Clip export uses per-clip seek calls instead of segment muxer',
        'instructions': (
            'Clip export was rewritten to use individual -ss/-t ffmpeg calls per clip '
            'instead of the -f segment muxer, which stalled consistently with h264_amf.\n\n'
            'To test: process any show that has quadrant MP4s and wait for clip export to run.\n\n'
            'Pass if: clips appear in the clips/ subfolder and the log shows no '
            '"no output for 10s" stall messages. Type HOME when done.'
        ),
    },
    {
        'id':    'silent_wav_archive_001',
        'title': 'Silent channel WAVs go to audio_archive instead of being deleted',
        'instructions': (
            'Silent channel WAVs are now archived to audio_archive/ instead of being '
            'queued for deletion, preserving recordings in case of misconfigured channels.\n\n'
            'To test: process a show that has silent channels (check the log for '
            '"SILENT" lines during audio processing).\n\n'
            'Pass if: the log shows "SILENT … archiving" lines and the corresponding '
            '_chan*.wav files appear in audio_archive/ rather than being deleted. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'reel_multi_session_001',
        'title': 'REMASTER generates one reel per session for multi-session bands',
        'instructions': (
            'This test requires a show with a band that has two separate sessions '
            '(two distinct quad sets, e.g. ANIMENIGHT10.0 and ANIMENIGHT11.0). '
            'Skip with OK if none is available.\n\n'
            'To test: type REMASTER on a date with such a band and wait for both '
            'REMASTER and REEL jobs to finish in the JOBS list.\n\n'
            'Pass if: two reel MP4 files appear in D:\\videos\\ — one per session '
            '(e.g. 26-04-17_ANIMENIGHT10.0_reel.mp4 and 26-04-17_ANIMENIGHT11.0_reel.mp4). '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'inventory_reel_mp3_001',
        'title': 'Inventory table shows MP3 and Reel columns',
        'instructions': (
            'The INVENTORY dashboard now tracks fullset MP3 and reel MP4 counts per performance.\n\n'
            'To test: type INVENTORY and rebuild with REBUILD on a date that has completed '
            'remaster and reel jobs.\n\n'
            'Pass if: the table header shows MP3 and Reel columns, and the corresponding '
            'row shows non-dash values for performances that have those files. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'remaster_perf_key_date_format_001',
        'title': 'REMASTER correctly finds PerformanceState for short-date perf keys',
        'instructions': (
            'REMASTER was silently skipping all bands with "no performance state found" '
            'because perf keys use short YY-MM-DD dates but _status_entries keys use '
            'YYYY-MM-DD — the comparison never matched.\n\n'
            'To test: run the pipeline on a show with WAV files present. Wait for the '
            'AUDIO job to finish, then watch for the REMASTER job in JOBS.\n\n'
            'Pass if: REMASTER runs to completion (JobQueue: finish … REMASTER) and a '
            '_FULLSET.mp3 appears in the audio destination folder. Type HOME when done.'
        ),
    },
    {
        'id':    'fs_first_db_reads_001',
        'title': 'Stale DB records show as (stale) in INVENTORY, and restored all-silent files are re-probed',
        'instructions': (
            'Two FS-first improvements were made to encoding DB reads.\n\n'
            'To test part 1 (stale display): type INVENTORY, expand any show, and look at the '
            'quadrant file list. Files whose DB record is out of date (mtime changed since last '
            'SCAN) should now show "(stale)" instead of codec/resolution metadata. '
            'Run BIGSCAN to refresh, then re-open INVENTORY — the stale tag should be replaced '
            'by up-to-date metadata.\n\n'
            'Pass if: stale files show (stale) before BIGSCAN and correct metadata after. '
            'Type HOME when done.'
        ),
    },
    {
        'id':    'platform_helpers_001',
        'title': 'Platform detection uses is_windows/is_darwin helpers throughout',
        'instructions': (
            'All sys.platform == "win32" or "MSYSTEM" in os.environ checks have been '
            'replaced with is_windows() / is_windows_native() / is_darwin() helpers '
            'from nofun.paths. The OTT plugin path lookup in mastering.py was also '
            'fixed to use detect_platform() so the "windows" key matches correctly.\n\n'
            'To test: run the pipeline normally and trigger a REMASTER on a band with a ZIP.\n\n'
            'Pass if: REMASTER completes without error and the pipeline starts correctly '
            'on both macOS (dev) and Windows (production). Type HOME when done.'
        ),
    },
    {
        'id':    'noproblem_unblocks_workers_001',
        'title': 'NOPROBLEM allows workers to dispatch jobs outside the 00:00–16:00 window',
        'instructions': (
            'Previously, NOPROBLEM bypassed the watchdog time gate so jobs were enqueued, '
            'but the worker threads still checked is_within_schedule independently and '
            'refused to dispatch after 16:00 — jobs sat in the queue forever.\n\n'
            'To test: run the pipeline after 16:00 with unprocessed files present. '
            'Type NOPROBLEM and then open JOBS.\n\n'
            'Pass if: jobs begin running (JobQueue: start lines appear in the log) within '
            'a few seconds of typing NOPROBLEM. Type HOME when done.'
        ),
    },
    {
        'id':    'ott_plugin_no_concurrent_load_001',
        'title': 'OTT VST3 plugin is not loaded concurrently when first attempt times out',
        'instructions': (
            'This test requires a multi-band show with at least two bands that will be '
            'REMASTERed sequentially. Previously, _do_remaster_for_band reset the OTT '
            'plugin cache before every band, so if band-1 timed out and left an abandoned '
            'load thread running, band-2 would spawn a second concurrent load — two threads '
            'calling pedalboard.load_plugin into the same VST3 DLL → native crash. '
            'Now the cache is only reset if the previous load actually failed (cache is None), '
            'so a successful load is reused and no concurrent calls are possible.\n\n'
            'To test: run the pipeline with two or more bands that have ZIP files eligible '
            'for REMASTER. Watch the JOBS menu as their REMASTER jobs complete sequentially.\n\n'
            'Pass if: both REMASTER jobs complete without a hard crash or raw ANSI escape '
            'sequences appearing in the terminal. Type HOME when done.'
        ),
    },
    {
        'id':    'menu_cleanup_001',
        'title': 'Home and JOBS menus show only essential commands',
        'instructions': (
            'The home command bar now shows only NOPROBLEM / INVENTORY / JOBS / PAUSE / HELP. '
            'STREAMS, REPROCESS, TEST, and TUTORIAL are removed from the bar (TEST and TUTORIAL '
            'still appear in the HELP overlay). '
            'The JOBS menu now shows history from log files instead of in-memory, '
            'and no longer has DRYRUN, SCHEDULE, or CLEAR commands.\n\n'
            'To test: launch the TUI and check the command bar at the bottom. '
            'Then type JOBS and confirm the bar shows only "Type a number to select / HELP / HOME". '
            'Type HELP from the home screen and confirm TEST and TUTORIAL appear in the list.\n\n'
            'Pass if: command bars match the above. Type HOME when done.'
        ),
    },
    {
        'id':    'windows_sigkill_symlink_001',
        'title': 'SIGKILL and symlink tests pass on Windows',
        'instructions': (
            'ScriptRunner now uses getattr(signal, "SIGKILL", signal.SIGTERM) so '
            'kill() and stall-kill work on Windows where SIGKILL is absent. '
            'Symlink tests skip gracefully when Developer Mode is off. '
            'The stall-test script is written with encoding="utf-8" to avoid '
            'UnicodeEncodeError on cp1252 consoles.\n\n'
            'To test: on the prod Windows machine, run uv run pytest '
            'tests/test_script_runner.py::TestOrphanPidTracking '
            'tests/test_menu_commands.py::TestSafeLinkAndStagingCleanup -v.\n\n'
            'Pass if: all tests pass or skip (none fail). Type OK when done.'
        ),
    },
    {
        'id':    'queue_test_suite_001',
        'title': 'Queue-layer test suite passes for dependency, pause, and batch drain',
        'instructions': (
            'A new queue-layer test suite (tests/test_queue.py) was added covering '
            'dependency ordering, failed-job blocking, PermissionError retry, pause/resume, '
            'schedule gate, manifest idempotency, multi-performance isolation, and full '
            'synthetic batch run. No production behaviour changed.\n\n'
            'To test: run uv run pytest tests/test_queue.py -v in a terminal. '
            'All 23 tests should pass in under 30 seconds with no real ffmpeg calls.\n\n'
            'Pass if: 23 passed, 0 failed. Type OK when done.'
        ),
    },
    {
        'id':    'auto_cleanup_001',
        'title': 'AUTO CLEANUP scheduled task runs every 6 hours without confirmation',
        'instructions': (
            'Orphaned temps, redundant sources, archive duplicates, and orphaned clip dirs '
            'are now cleaned automatically by a scheduled task that runs every 6 hours '
            'in the watchdog loop — no YESPLEASE or manual command needed.\n\n'
            'To test: leave the TUI running for a few minutes and open the JOBS menu. '
            'After the first 6-hour interval the CLEANUP steps will appear. '
            'To verify sooner, temporarily create a stray *_temp*.mp4 file in the videos folder, '
            'then check the log for "AUTO CLEANUP" and "DELETE" entries.\n\n'
            'Pass if: log shows "AUTO CLEANUP  starting" and "AUTO CLEANUP  done" without any '
            'prompt or YESPLEASE gate. Type HOME when done.'
        ),
    },
    {
        'id':    'sp_empty_folder_archive_001',
        'title': 'Empty SharePoint date folders (only _nofun_info.txt) are moved to archived/',
        'instructions': (
            'Previously, SharePoint date folders whose media files had already been deleted '
            '(by AUTO CLEANUP or a prior expiry run) were never moved to archived/ — they sat '
            'forever with only _nofun_info.txt.\n\n'
            'To test: find a SharePoint date folder that contains only _nofun_info.txt (no MP4s '
            'or ZIPs). Wait for EXPIRE CLOUD SHARES to run (hourly), or restart the pipeline to '
            'trigger the first scheduled run. Check the log for "CLOUDCLEAN: moved … → archived/ '
            '(already cleaned)".\n\n'
            'Pass if: the folder appears under archived/ and is no longer visible at the top '
            'level of the SharePoint Multitracks folder. Type HOME when done.'
        ),
    },
    {
        'id':    'temp_file_ghost_perf_001',
        'title': 'Temp encode files do not appear as phantom inventory entries',
        'instructions': (
            'JUNK_SUFFIX now strips "temp" tokens so files like '
            '26-01-01_Band_UR_temp.mp4 parse back to band "Band" instead of '
            '"Band_UR_temp". _check_orphaned_temps also now catches *_temp.mp4 '
            '(quadrant encode temps) in both search_dir and vids_dest.\n\n'
            'To test: copy or rename a file in VenueLighting/ to '
            '26-01-01_TestBand_UR_temp.mp4, then run SCAN from the INVENTORY menu. '
            'Open INVENTORY and expand the 2026-01-01 date.\n\n'
            'Pass if: only the real TestBand row appears — no "TestBand UR temp" or '
            '"Band_UR_temp" phantom row. Delete the test file and type HOME when done.'
        ),
    },
    {
        'id':    'amf_vbaq_suppress_001',
        'title': 'h264_amf VBAQ warning suppressed from log',
        'instructions': (
            'The AMF encoder emits "VBAQ is not supported by cqp Rate Control Method, '
            'automatically disabled" once per ffmpeg process; clip export spawns many '
            'processes so this line previously flooded the log.\n\n'
            'To test: trigger a clip export for any show (encoding must use h264_amf on '
            'a Windows GPU machine), then open the rolling log file.\n\n'
            'Pass if: the VBAQ warning line is absent from the log. Type HOME when done.'
        ),
    },
]


def run_tutorial() -> None:
    """Show all tutorial steps one at a time. Cancel exits early."""
    total = len(TUTORIAL_STEPS)
    for i, step in enumerate(TUTORIAL_STEPS, start=1):
        ok = _show_dialog(
            title=f"NOFUN ({i}/{total})",
            message=f"{step['title']}\n\n{step['text']}",
        )
        if not ok:
            break
