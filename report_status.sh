#!/bin/bash
# ラズパイ本体の状態(温度・稼働時間・ディスク空き・現在のgitコミット)を
# 数分おきに管理サイト(Cloud Run)へ送る。同じ応答で「予約されている
# コマンド」(reboot等)も受け取り、その場で実行する。
#
# ラズパイ側は外部からの着信を一切受け付けない設計(OTA更新と同じ
# pull型)にしているため、リモート再起動もこの「状態報告のついでに
# コマンドを受け取る」形にしている(このスクリプト自体を定期実行する
# systemdタイマーがpull役を担う)。
#
# 使い方: systemdタイマー(qzss-report-status.timer)で定期実行する。
#   手動で今すぐ確認したい場合はそのまま実行するだけでよい:
#     ./report_status.sh
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$DIR")"
MAP_DIR="${MAP_DIR:-$PARENT_DIR/qzss-map}"
PI_DIR="${PI_DIR:-$DIR}"
STATE_DIR="$DIR/update_state"
LOG_FILE="$STATE_DIR/report_status.log"
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

# 温度の閾値(℃)。TEMP_CRITICALはラズパイがサーマルスロットリングを
# 始める目安(80℃前後)なので、それより少し手前で気づけるようにする
TEMP_WARN="${TEMP_WARN:-70}"
TEMP_CRITICAL="${TEMP_CRITICAL:-80}"

notify_discord() {
  local message="$1"
  [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0
  local hostname_str full_text
  hostname_str="$(hostname)"
  full_text="🌡️ QZSS 状態監視 (${hostname_str})
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
    > /dev/null 2>&1 || log "⚠️ Discordへの通知に失敗しました"
}

# --- 各種状態を集める ---

DEVICE_ID="${QZSS_DEVICE_ID:-$(hostname)}"

# 温度: vcgencmd(Raspberry Pi OS標準)を優先し、無ければ
# /sys/class/thermal を使う(その他Linux環境でのローカル動作確認用)
read_temperature() {
  if command -v vcgencmd > /dev/null 2>&1; then
    vcgencmd measure_temp 2>/dev/null | sed -n "s/temp=\([0-9.]*\).*/\1/p"
  elif [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp
  else
    echo ""
  fi
}

TEMPERATURE="$(read_temperature)"
UPTIME_SEC="$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo "")"
DISK_FREE_PCT="$(df -P "$DIR" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print 100-$5}')"

# --- 直前に自分(report_status.sh)が予約した再起動が、実際に成功したか
#     確認する(下のコマンド処理でreboot実行前にマーカーを作成する)。
#     稼働時間が短ければ再起動が完了したとみなし、長ければ何らかの理由
#     (sudo権限不足等)で再起動されていない可能性を通知する。
#     停電等、こちらが予約していない再起動では通知しない(マーカーが
#     無いため誤検知しない) ---
REBOOT_MARKER="$STATE_DIR/reboot_requested"
if [ -f "$REBOOT_MARKER" ]; then
  if [ -n "$UPTIME_SEC" ] && [ "$UPTIME_SEC" -lt 600 ]; then
    log "✅ 予約された再起動が完了しました(稼働時間: ${UPTIME_SEC}秒)"
    notify_discord "✅ 再起動が完了しました(稼働時間: ${UPTIME_SEC}秒)。"
  else
    log "⚠️ 再起動を予約しましたが、まだ再起動されていない可能性があります(稼働時間: ${UPTIME_SEC:-不明}秒)"
    notify_discord "⚠️ 再起動を予約しましたが、まだ再起動されていない可能性があります(稼働時間: ${UPTIME_SEC:-不明}秒)。手動での確認をお願いします。"
  fi
  rm -f "$REBOOT_MARKER"
fi

git_commit() {
  local repo_dir="$1"
  [ -d "$repo_dir/.git" ] || { echo ""; return; }
  (cd "$repo_dir" && git rev-parse HEAD 2>/dev/null) || echo ""
}
GIT_COMMIT_MAP="$(git_commit "$MAP_DIR")"
GIT_COMMIT_PI="$(git_commit "$PI_DIR")"

# --- 温度チェック(閾値超過ならDiscordへ即通知) ---
if [ -n "$TEMPERATURE" ]; then
  temp_int="${TEMPERATURE%.*}"
  if [ "$temp_int" -ge "$TEMP_CRITICAL" ] 2>/dev/null; then
    log "🚨 本体温度が危険域です: ${TEMPERATURE}℃"
    notify_discord "🚨 本体温度が危険域です: ${TEMPERATURE}℃(閾値: ${TEMP_CRITICAL}℃)。サーマルスロットリングにより性能低下・不安定化のおそれがあります。設置場所の通気を確認してください。"
  elif [ "$temp_int" -ge "$TEMP_WARN" ] 2>/dev/null; then
    log "⚠️ 本体温度が高めです: ${TEMPERATURE}℃"
  fi
fi

# --- 主要サービスの生存確認(念のための保険。本来はsystemdの
#     Restart=on-failure + StartLimitIntervalSec=0 により自動復帰する
#     はずだが、手動停止やmask等で止まったままになっていないかも確認する) ---
SERVICE_USER="$(whoami)"
check_and_heal_service() {
  local unit="$1"
  systemctl is-active --quiet "$unit" && return 0
  log "⚠️ $unit が停止しています。再起動を試みます"
  if sudo -n systemctl restart "$unit" 2>/dev/null; then
    log "✅ $unit を再起動しました"
    notify_discord "⚠️ $unit が停止していたため自動再起動しました。頻発する場合は本体の点検をお願いします。"
  else
    log "❌ $unit の自動再起動に失敗しました(sudo権限不足の可能性)"
    notify_discord "🚨 $unit が停止していますが自動再起動に失敗しました。手動での確認をお願いします。"
  fi
}
check_and_heal_service "qzss-map@${SERVICE_USER}.service"
check_and_heal_service "qzss-decoder@${SERVICE_USER}.service"

# --- 送信先を決める(QZSS_CLOUD_URLの/ingestを/device/statusに置き換える) ---
if [ -z "${QZSS_CLOUD_URL:-}" ]; then
  log "⚠️ QZSS_CLOUD_URL が未設定のため状態報告をスキップします"
  exit 0
fi
STATUS_URL="${QZSS_CLOUD_URL%/ingest}/device/status"

payload=$(cat <<JSON
{
  "device_id": "${DEVICE_ID}",
  "hostname": "$(hostname)",
  "temperature_c": ${TEMPERATURE:-null},
  "uptime_sec": ${UPTIME_SEC:-null},
  "disk_free_pct": ${DISK_FREE_PCT:-null},
  "git_commit_map": "${GIT_COMMIT_MAP}",
  "git_commit_pi": "${GIT_COMMIT_PI}"
}
JSON
)

response="$(curl -fsS -X POST "$STATUS_URL" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${QZSS_INGEST_TOKEN:-}" \
  -d "$payload" 2>&1)"

if [ $? -ne 0 ]; then
  log "⚠️ 状態報告の送信に失敗しました: $response"
  exit 0
fi

log "📡 状態報告: 温度=${TEMPERATURE:-不明}℃ 稼働=${UPTIME_SEC:-不明}秒 disk空き=${DISK_FREE_PCT:-不明}%"

# --- 予約されているコマンドを実行する ---
if command -v jq > /dev/null 2>&1; then
  commands="$(echo "$response" | jq -r '.commands[]?.command' 2>/dev/null)"
else
  # jq が無い環境向けの簡易フォールバック(コマンド名を粗く抜き出す)
  commands="$(echo "$response" | grep -o '"command":"[a-z_]*"' | sed 's/"command":"\(.*\)"/\1/')"
fi

for cmd in $commands; do
  case "$cmd" in
    reboot)
      log "🔄 再起動コマンドを受信しました。10秒後に再起動します"
      notify_discord "再起動します(完了したら改めて通知します)。"
      touch "$STATE_DIR/reboot_requested"
      ( sleep 10 && sudo /usr/bin/systemctl reboot ) &
      ;;
    force_update_check)
      log "🔍 管理サイトからの更新確認コマンドを受信しました"
      "$DIR/update_check.sh"
      ;;
    *)
      log "⚠️ 未対応のコマンドを受信しました: $cmd"
      ;;
  esac
done
