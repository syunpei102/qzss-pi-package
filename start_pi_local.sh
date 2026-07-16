#!/bin/bash
# ラズベリーパイ+ディスプレイ直結の完全オフラインkiosk表示用スクリプト。
# インターネット接続やCloud Runを一切使わず、ラズパイ自身が
# 地図アプリ(server.js)を起動し、同じラズパイ上のChromiumを
# kioskモード(全画面・操作不可)でそこに向ける。
#
# 前提: このリポジトリ(qzss-pi-package)と、統合地図アプリのリポジトリ
# (qzss-map)を同じ親ディレクトリに並べてcloneしておくこと(GitHubの
# リポジトリ名そのままでよい):
#   ~/qzss/qzss-pi-package/  (このリポジトリ)
#   ~/qzss/qzss-map/         (地図アプリ。server.js・public/ を含む)
#
# 使い方:
#   ./start_pi_local.sh /dev/ttyUSB0 115200
#
# Pi 3等のGPUが非力な機種向けの事前準備(一度だけ手動で、要再起動):
#   sudo raspi-config → Performance Options → GPU Memory → 128以上に設定
#   (raspi-configにこの項目が無いイメージの場合は、
#    /boot/firmware/config.txt (無ければ /boot/config.txt) に
#    `gpu_mem=128` を追記してから再起動する)
#   (GPUメモリが少ないとWebGL描画がソフトウェアフォールバックし著しく重くなる)
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT_ARG="$1"
BAUDRATE="${2:-115200}"
HTTP_PORT="${HTTP_PORT:-8080}"
MAP_DIR="${MAP_DIR:-$DIR/../qzss-map}"

if [ -z "$PORT_ARG" ]; then
  echo "使い方: $0 <シリアルポート> [ボーレート]"
  echo ""
  echo "接続中のシリアルポート候補:"
  ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  (見つかりませんでした。受信機の接続を確認してください)"
  exit 1
fi

if [ ! -e "$PORT_ARG" ]; then
  echo "❌ ポート $PORT_ARG が見つかりません。受信機の接続を確認してください。"
  exit 1
fi

if [ ! -d "$MAP_DIR" ]; then
  echo "❌ 地図アプリ($MAP_DIR)が見つかりません。MAP_DIR環境変数で場所を指定してください。"
  echo "   例: MAP_DIR=/home/pi/qzss/map $0 $PORT_ARG $BAUDRATE"
  exit 1
fi

if [ ! -d venv ]; then
  echo "🐍 Python venv が無いので作成します (azarashi, pyserial を導入)"
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

echo "🗺️  地図アプリを起動します ($MAP_DIR, port $HTTP_PORT)"
(cd "$MAP_DIR" && [ -d node_modules ] || npm install --omit=dev)
(cd "$MAP_DIR" && PORT="$HTTP_PORT" LOCAL_STATE_ONLY=true node server.js > /tmp/qzss_map_local.log 2>&1) &
MAP_PID=$!
trap 'kill "$MAP_PID" 2>/dev/null' EXIT

echo "⏳ 地図アプリの起動を待っています…"
for i in $(seq 1 30); do
  if curl -fs "http://localhost:${HTTP_PORT}/" > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "🖥️  Chromiumをkioskモードで起動します"
CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || echo chromium-browser)"
# Pi 3等のGPUが非力な機種向けに、WebGL(MapLibre GL JS)がなるべく
# ハードウェアアクセラレーションで動くように明示的にフラグを付ける。
# --use-angle=gl-egl: VideoCoreのEGL経由で描画(ソフトウェアフォールバックを
#   避ける)。実機(Pi 3B+, Chromium 150)で検証したところ旧来の
#   --use-gl=egl はこのバージョンのChromiumでは受け付けられず即座に
#   GPUプロセスが終了してしまうことを確認した。--use-angle=gl-eglなら
#   GPUプロセスが安定して動き、/dev/dri/renderD128 を実際に使用する
#   (Pi 3のVideoCore IVはOpenGL ES 3.0非対応のためES3コンテキスト生成の
#   失敗ログが出るが、ES2へ自動フォールバックするため描画自体は動く)
# --enable-zero-copy / --enable-gpu-rasterization: GPUラスタライズを有効化
# --disable-smooth-scrolling: 慣性スクロール等の余計な演出を削って軽くする
"$CHROMIUM_BIN" \
  --kiosk \
  --incognito \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --check-for-update-interval=31536000 \
  --use-angle=gl-egl \
  --enable-gpu-rasterization \
  --enable-zero-copy \
  --disable-smooth-scrolling \
  "http://localhost:${HTTP_PORT}" &
CHROMIUM_PID=$!
trap 'kill "$MAP_PID" "$CHROMIUM_PID" 2>/dev/null' EXIT

echo "🛰️  受信機からの取り込みを開始し、ローカルの地図アプリへ送信します ($PORT_ARG @ $BAUDRATE)"
QZSS_CLOUD_URL="http://localhost:${HTTP_PORT}/ingest" \
  ./venv/bin/python3 read_legacy_dual.py "$PORT_ARG" "$BAUDRATE"
