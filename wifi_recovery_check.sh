#!/bin/bash
# 起動直後にwlan0が認識されていない場合、ドライバの再読み込みで復旧を
# 試みる。
#
# 背景: 実機で「通常の reboot 後、wlan0自体がip aに出てこない(rfkillの
# ブロックとは別の状態)」現象を複数回確認した。前回はコンセントを
# 完全に抜き差しする物理的な電源サイクルでのみ復旧しており、SDIOバス
# 側の何らかのタイミング問題が疑われるが、再起動をまたいだログが
# 残らない設定になっていたため確定的な原因はまだ特定できていない。
# 確定原因が分からなくても実用上困らないよう、まずソフトウェア的な
# 復旧(ドライバの再読み込み)を自動で試すことにする。これで直らない
# 場合は物理的な電源の抜き差しが引き続き必要。
#
# 使い方: systemdサービス(qzss-wifi-recovery.service)で起動時に1回実行する。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$DIR/update_state"
LOG_FILE="$STATE_DIR/wifi_recovery.log"
mkdir -p "$STATE_DIR"

if [ -f "$DIR/qzss.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$DIR/qzss.env"
  set +a
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

notify_discord() {
  local message="$1"
  [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0
  local hostname_str full_text
  hostname_str="$(hostname)"
  full_text="📶 QZSS WiFi復旧監視 (${hostname_str})
${message}"
  local payload
  if command -v jq > /dev/null 2>&1; then
    payload="$(jq -n --arg content "$full_text" '{content: $content}')"
  else
    local escaped
    escaped="$(printf '%s' "$full_text" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')"
    payload="{\"content\": \"${escaped%\\n}\"}"
  fi
  curl -fsS -X POST -H "Content-Type: application/json" -d "$payload" "$DISCORD_WEBHOOK_URL" \
    > /dev/null 2>&1 || log "⚠️ Discord通知の送信に失敗しました"
}

# 起動直後、SDIOバスの列挙が完了するまで少し待つ余地を持たせる
sleep 10

if ip link show wlan0 > /dev/null 2>&1; then
  log "✅ wlan0は正常に認識されています(復旧不要)"
  exit 0
fi

log "⚠️ wlan0が認識されていません。ドライバの再読み込みを試みます"

sudo /sbin/modprobe -r brcmfmac brcmfmac_cyw brcmutil 2>&1 | tee -a "$LOG_FILE"
sleep 2
sudo /sbin/modprobe brcmfmac 2>&1 | tee -a "$LOG_FILE"
sleep 5

if ip link show wlan0 > /dev/null 2>&1; then
  log "✅ ドライバの再読み込みでwlan0が復旧しました"
  notify_discord "起動時にwlan0が認識されていませんでしたが、ドライバの再読み込みで復旧しました。"
else
  log "❌ ドライバの再読み込みでも復旧しませんでした。物理的な電源の抜き差しが必要な可能性があります"
  # 注意: この通知はwlan0が無い=ネットワーク経路が無い状態で送ろうと
  # しているため、実際には届かない可能性が高い(送れたら復旧している
  # ということでもある)。ログファイルが最後の頼りになる
  notify_discord "🚨 起動時にwlan0が認識されず、ドライバの再読み込みでも復旧しませんでした。物理的な電源の抜き差し(コンセントを抜いて入れ直す)が必要な可能性があります。ローカルのkiosk表示・受信機自体は動作しているはずですが、リモートからは繋がりません。"
fi
