#!/bin/bash
# start-streams.sh ΓÇö launch VLC HTTP streams from the clip library.
# Each stream gets a random shuffled playlist of all .mp4s under CLIP_ROOT.
#
# Usage: ./start-streams.sh [clip_root] [base_port] [stream_count]
# Defaults: MOUNT_D/clips   8554   5

# Detect MOUNT_D (matches process-videos.sh convention)
if [ -z "$MOUNT_D" ]; then
    if [ -d "/mnt/c" ]; then MOUNT_C="/mnt/c"; MOUNT_D="/mnt/d"
    elif [ -d "/c" ]; then MOUNT_C="/c"; MOUNT_D="/d"
    fi
fi
[ -z "$MOUNT_D" ] && MOUNT_D="."

CLIP_ROOT="${1:-$MOUNT_D/clips}"
BASE_PORT="${2:-8554}"
STREAM_COUNT="${3:-5}"

# Kill any existing VLC instances
taskkill.exe /F /IM vlc.exe /T 2>/dev/null || pkill -f vlc 2>/dev/null || true

# Detect local IP ΓÇö Windows first, then Linux/macOS fallbacks
get_local_ip() {
    if command -v ipconfig.exe &>/dev/null; then
        ipconfig.exe | grep "IPv4 Address" | head -n 1 | awk '{print $NF}' | tr -d '\r'
    elif command -v ip &>/dev/null; then
        ip route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}'
    elif command -v ipconfig &>/dev/null; then
        ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null
    fi
}
LOCAL_IP=$(get_local_ip)
LOCAL_IP="${LOCAL_IP:-127.0.0.1}"

# Find VLC binary ΓÇö Windows paths first, then macOS, then PATH
if   [ -x "/c/Program Files/VideoLAN/VLC/vlc.exe" ]; then VLC="/c/Program Files/VideoLAN/VLC/vlc.exe"
elif [ -x "/d/Program Files/VideoLAN/VLC/vlc.exe" ]; then VLC="/d/Program Files/VideoLAN/VLC/vlc.exe"
elif command -v vlc.exe &>/dev/null; then VLC=vlc.exe
elif [ -x "/Applications/VLC.app/Contents/MacOS/VLC" ]; then VLC="/Applications/VLC.app/Contents/MacOS/VLC"
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

    # Random shuffled playlist
    find "$CLIP_ROOT" -name "*.mp4" | shuf > "$PLIST"

    # Convert POSIX path to Windows path for VLC playlist arg
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
        WINDOWS_PLIST=$(cygpath -w "$PLIST")
    else
        WINDOWS_PLIST="$PLIST"
    fi

    echo "Launching stream $((i+1)) on port $PORT..."

    # Alternative sout formats (from original PS1):
    # --sout "#transcode{vcodec=MJPG,vb=200,fps=15}:http{mux=mpjpeg,dst=:${PORT}/video}"
    # --sout "#transcode{vcodec=h264,vb=200,fps=30,profile=baseline}:http{mux=ts,dst=:${PORT}/video}"
    # --sout "#transcode{vcodec=h264,vb=512,fps=15,profile=baseline}:http{mux=mp4,dst=:${PORT}/video}"
    "$VLC" \
        --intf dummy \
        --random \
        --loop \
        --playlist-autostart \
        --sout-keep \
        --file-caching 3000 \
        --network-caching 2000 \
        "$WINDOWS_PLIST" \
        --sout "#transcode{vcodec=h264,acodec=none}:http{mux=ts,dst=:${PORT}/video}" \
        >"$LOGFILE" 2>&1 &

    echo "  Stream $((i+1)) ΓåÆ http://${LOCAL_IP}:${PORT}/video  (log: $LOGFILE)"

    # Stagger starts so each instance can bind its port before the next launches
    [ $((i + 1)) -lt "$STREAM_COUNT" ] && sleep 3
done

echo ""
echo "All streams started.  Press <Enter> to terminate."
read -r _

taskkill.exe /F /IM vlc.exe /T 2>/dev/null || pkill -f vlc 2>/dev/null || true
