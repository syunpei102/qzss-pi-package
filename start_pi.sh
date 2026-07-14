#!/bin/bash
# ラズベリーパイ側のワンショット起動スクリプト。
# 受信機からの取り込み → デコード → 重要情報はメイン地図へ、注意情報は
# 注意情報マップへ、それぞれ振り分けて送信する(read_legacy_dual.py)。
# (地図の描画やWebSocket配信はクラウド側のCloud Runサービスが行う)
#
# 使い方:
#   QZSS_CLOUD_URL_CRITICAL=https://xxxx.a.run.app/ingest \
#   QZSS_INGEST_TOKEN_CRITICAL=xxxxxxxx \
#   QZSS_CLOUD_URL_CAUTION=https://yyyy.a.run.app/ingest \
#   QZSS_INGEST_TOKEN_CAUTION=yyyyyyyy \
#   ./start_pi.sh /dev/ttyUSB0 115200
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT="$1"
BAUDRATE="${2:-115200}"

if [ -z "$PORT" ] || { [ -z "$QZSS_CLOUD_URL_CRITICAL" ] && [ -z "$QZSS_CLOUD_URL_CAUTION" ]; }; then
  echo "使い方:"
  echo "  QZSS_CLOUD_URL_CRITICAL=https://xxxx.a.run.app/ingest QZSS_INGEST_TOKEN_CRITICAL=xxxx \\"
  echo "  QZSS_CLOUD_URL_CAUTION=https://yyyy.a.run.app/ingest QZSS_INGEST_TOKEN_CAUTION=yyyy \\"
  echo "  $0 <シリアルポート> [ボーレート]"
  echo ""
  echo "接続中のシリアルポート候補:"
  ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  (見つかりませんでした。受信機の接続を確認してください)"
  exit 1
fi

if [ ! -e "$PORT" ]; then
  echo "❌ ポート $PORT が見つかりません。受信機の接続を確認してください。"
  exit 1
fi

if [ ! -d venv ]; then
  echo "🐍 Python venv が無いので作成します (azarashi, pyserial を導入)"
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

echo "🛰️  受信機からの取り込みを開始し、重要情報はメイン地図へ、注意情報は注意情報マップへ送信します"
echo "   ($PORT @ $BAUDRATE)"
./venv/bin/python3 read_legacy_dual.py "$PORT" "$BAUDRATE"
