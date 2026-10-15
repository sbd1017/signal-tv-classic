#!/bin/bash
# Generates the local, legal demo media used by signal-tv-classic-demo (no downloads).
# Usage: make-media.sh OUT_DIR     (safe to re-run; skips files that exist)
set -euo pipefail
OUT=${1:?out dir}; mkdir -p "$OUT"; cd "$OUT"
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
X264=(-c:v libx264 -preset veryfast -profile:v high -pix_fmt yuv420p -g 50 -keyint_min 50 -sc_threshold 0)
label() {  # $1 title
  echo "drawtext=fontfile=$FONT:text='$1':x=40:y=36:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=14,drawtext=fontfile=$FONT:text='%{pts\:hms}':x=w-tw-40:y=h-th-36:fontsize=38:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12"
}
gen() { # out seconds videosrc title audiofreq [encoder args...]
  local out=$1 dur=$2 src=$3 title=$4 freq=$5; shift 5
  [ -s "$out" ] && { echo "have $out"; return; }
  echo "making $out"
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "$src" -f lavfi -i "sine=frequency=$freq:beep_factor=4:sample_rate=48000" \
    -t "$dur" -vf "$(label "$title")" "$@" "part.$out" && mv "part.$out" "$out"
}
# live loops (MPEG-TS, like a broadcast feed)
gen live1.ts 90 "testsrc2=s=960x540:r=25" "Demo One" 440 "${X264[@]}" -c:a aac -b:a 128k -f mpegts
gen live2.ts 90 "smptehdbars=s=960x540:r=25" "Demo Two" 523 "${X264[@]}" -c:a mp2 -b:a 192k -f mpegts
gen live3.ts 90 "life=s=960x540:r=25:mold=10:ratio=0.1:death_color=#14213d:life_color=#7fd1ff" "Demo Three (MPEG-2)" 659 \
  -c:v mpeg2video -b:v 4M -g 12 -pix_fmt yuv420p -c:a mp2 -b:a 192k -f mpegts
gen live4.ts 90 "testsrc=s=960x540:r=25" "Demo Four (AC-3)" 784 "${X264[@]}" -c:a ac3 -b:a 192k -f mpegts
# movies
gen movie1.mp4 180 "testsrc2=s=1280x720:r=25" "Test Pattern Feature" 330 "${X264[@]}" -c:a aac -b:a 128k -movflags +faststart
gen movie2.mkv 150 "smptehdbars=s=1280x720:r=25" "Colour Bars (MKV, AC-3 5.1)" 350 "${X264[@]}" -c:a ac3 -ac 6 -b:a 384k
gen movie3.mp4 120 "testsrc=s=1280x720:r=25" "Late Moov (no faststart)" 370 "${X264[@]}" -c:a aac -b:a 128k
# series
for e in 1 2 3; do gen "series_s01e0$e.mkv" 60 "testsrc2=s=960x540:r=25" "Demo Series S01E0$e" $((300 + e * 40)) "${X264[@]}" -c:a aac -b:a 128k; done
for e in 1 2; do gen "series_s02e0$e.mp4" 60 "smptehdbars=s=960x540:r=25" "Demo Series S02E0$e" $((500 + e * 40)) "${X264[@]}" -c:a aac -b:a 128k -movflags +faststart; done
# channel logos (square) and posters (2:3)
logo() { # n size text colour
  [ -s "logo$1.png" ] && return
  ffmpeg -hide_banner -loglevel error -y -f lavfi -i "color=c=$4:s=$2" -frames:v 1 \
    -vf "drawtext=fontfile=$FONT:text='$3':x=(w-tw)/2:y=(h-th)/2:fontsize=$5:fontcolor=white" "logo$1.png"
}
logo 1 256x256 "ONE" "#1f6feb" 64;  logo 2 256x256 "TWO" "#8957e5" 64
logo 3 256x256 "THREE" "#2ea043" 56; logo 4 256x256 "FOUR" "#d29922" 60
logo 101 256x256 "MUX" "#30363d" 64; logo 102 256x256 "BIPBOP" "#6e7681" 44
logo 201 300x450 "FEATURE" "#0b3d91" 48; logo 202 300x450 "BARS" "#7a1f5c" 60
logo 203 300x450 "MOOV" "#1b5e20" 60;    logo 204 300x450 "STEEL" "#37474f" 60
logo 301 300x450 "SERIES" "#b35900" 56
ls -la
