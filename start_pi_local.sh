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
BAUDRATE="${2:-9600}"
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
# Pi 3B+のGPU(VideoCore IV)を--use-angle=gl-eglで有効化すると、
# 実機で確認したところレンダラープロセスが数十分おきにクラッシュする
# (GPU-GLコンテキスト関連の--type=rendererクラッシュダンプを確認)。
# ハードウェアGPUパスを避け、ソフトウェアWebGL(SwiftShader)に固定して
# 安定性を優先する(描画は多少遅くなるが、クラッシュが直る)。
# 注意: 単純な--disable-gpuだとWebGL自体が初期化に失敗し(MapLibre GL
# JSはWebGL必須)、地図が全く表示されなくなるため使わないこと(実機で
# 確認済み)。--use-angle=swiftshaderだけでは software WebGL fallback
# が拒否されるため、--enable-unsafe-swiftshaderとの併用が必須
# (信頼済みの自前コンテンツのみのkioskなので許容する)
# --disable-smooth-scrolling: 慣性スクロール等の余計な演出を削って軽くする
# --password-store=basic: 指定しないと初回起動時に「キーリングの
# パスワードを設定してください」というダイアログが表示され、無人の
# キオスク画面が固まって見えてしまう(実機で確認済み)
# --disable-background-networking / --disable-sync / --disable-features=...:
# GoogleアカウントSync・翻訳・最適化ヒント等、kioskには不要な裏側の
# ネットワーク通信・機能を止める
"$CHROMIUM_BIN" \
  --kiosk \
  --incognito \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --use-angle=swiftshader \
  --enable-unsafe-swiftshader \
  --disable-smooth-scrolling \
  --disable-background-networking \
  --disable-sync \
  --disable-notifications \
  --disable-client-side-phishing-detection \
  --disable-default-apps \
  --disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication,GCM \
  "http://localhost:${HTTP_PORT}" &
CHROMIUM_PID=$!
trap 'kill "$MAP_PID" "$CHROMIUM_PID" 2>/dev/null' EXIT

echo "🛰️  受信機からの取り込みを開始し、ローカルの地図アプリへ送信します ($PORT_ARG @ $BAUDRATE)"
QZSS_CLOUD_URL="http://localhost:${HTTP_PORT}/ingest" \
  ./venv/bin/python3 read_legacy_dual.py "$PORT_ARG" "$BAUDRATE"
