#!/bin/bash
# start-streams-mac.sh — launch VLC HTTP streams from the clip library (macOS).
# Each stream gets a random shuffled playlist of all .mp4s under CLIP_ROOT.
#
# Usage: ./start-streams-mac.sh [clip_root] [base_port] [stream_count]
# Defaults: ./clips   8554   5

CLIP_ROOT="${1:-${MOUNT_D:-$PWD}/clips}"
BASE_PORT="${2:-8554}"
STREAM_COUNT="${3:-5}"

# Kill any existing VLC instances
pkill -f "VLC" 2>/dev/null || true

# Detect local IP
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")

# Find VLC binary
if   [ -x "/Applications/VLC.app/Contents/MacOS/VLC" ]; then VLC="/Applications/VLC.app/Contents/MacOS/VLC"
elif command -v vlc &>/dev/null; then VLC=vlc
else echo "ERROR: vlc not found"; exit 1
fi

# Validate clip root has content
mp4_count=$(find "$CLIP_ROOT" -name "*.mp4" 2>/dev/null | wc -l | tr -d ' ')
if [ "$mp4_count" -eq 0 ]; then
    echo "ERROR: No .mp4 files found in $CLIP_ROOT"
    exit 1
fi
echo "Found $mp4_count .mp4 files in $CLIP_ROOT"
echo "Starting $STREAM_COUNT streams on $LOCAL_IP..."

for ((i=0; i<STREAM_COUNT; i++)); do
    PORT=$((BASE_PORT + i))
    PLIST="/tmp/pls_${PORT}.m3u"
    LOGFILE="/tmp/vlc_${PORT}.log"

    find "$CLIP_ROOT" -name "*.mp4" | sort -R > "$PLIST"

    echo "Launching stream $((i+1)) on port $PORT..."

    "$VLC" \
        --intf dummy \
        --random \
        --loop \
        --playlist-autostart \
        --sout-keep \
        --file-caching 3000 \
        --network-caching 2000 \
        "$PLIST" \
        --sout "#transcode{vcodec=h264,acodec=none}:http{mux=ts,dst=:${PORT}/video}" \
        >"$LOGFILE" 2>&1 &

    echo "  Stream $((i+1)) → http://${LOCAL_IP}:${PORT}/video  (log: $LOGFILE)"

    [ $((i + 1)) -lt "$STREAM_COUNT" ] && sleep 3
done

echo ""
echo "All streams started.  Press <Enter> to terminate."
read -r _

pkill -f "VLC" 2>/dev/null || true
