#!/usr/bin/env bash
# Standalone RTSP/HLS streaming server for clip directories.
# Spawns mediamtx + N ffmpeg push streams, prints RTSP URLs.
# Press Enter to stop.

set -euo pipefail

CLIP_ROOT="${1:-test_files/clips}"
STREAM_COUNT="${2:-5}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MEDIAMTX="$REPO_ROOT/bin/mediamtx"
[ -x "$MEDIAMTX" ] || { echo "missing $MEDIAMTX — see bin/README.md"; exit 1; }

TMP="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$TMP"' EXIT

# Generate config
cat > "$TMP/mediamtx.yml" <<EOF
logLevel: warn
rtspAddress: :8554
hlsAddress:  :8888
webrtcAddress: :8889
paths:
$(for i in $(seq 1 "$STREAM_COUNT"); do echo "  stream$i: {}"; done)
EOF

# Start mediamtx
"$MEDIAMTX" "$TMP/mediamtx.yml" >"$TMP/mediamtx.log" 2>&1 &
echo "mediamtx pid $! (log: $TMP/mediamtx.log)"
for i in {1..10}; do
    nc -z localhost 8554 2>/dev/null && break
    [ $i -eq 10 ] && { echo "mediamtx failed to start — check $TMP/mediamtx.log"; exit 1; }
    sleep 1
done

# Resolve LAN IP for printing URLs (best-effort)
LOCAL_IP="$(ipconfig getifaddr en0 2>/dev/null || hostname)"

# Build per-stream playlists and launch ffmpeg pushers
for i in $(seq 1 "$STREAM_COUNT"); do
    PLS="$TMP/pls_stream$i.txt"
    # Shuffle clips into an ffconcat playlist
    {
        echo 'ffconcat version 1.0'
        find "$CLIP_ROOT" -type f \( -name '*.mp4' -o -name '*.mov' \) \
            | shuf | sed "s|^|file '|; s|$|'|"
    } > "$PLS"

    ffmpeg -hide_banner -loglevel warning \
           -fflags +genpts+igndts -re \
           -f concat -safe 0 -stream_loop -1 -i "$PLS" \
           -c:v copy -an \
           -bsf:v h264_mp4toannexb \
           -f rtsp -rtsp_transport tcp \
           "rtsp://localhost:8554/stream$i" \
           >"$TMP/ffmpeg_stream$i.log" 2>&1 &
    echo "stream$i  rtsp://$LOCAL_IP:8554/stream$i   (HLS: http://$LOCAL_IP:8888/stream$i/index.m3u8)"
done

read -p "Press Enter to stop..."