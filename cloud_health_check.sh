#!/bin/bash
# Cloud Run(eq.shum10.com等)自体が外から見て応答するかを確認する軽量
# チェック。30秒おきに実行される前提なので、通常時のコストを極力
# 小さくしてある(1回のcurlだけで、状態が変わらなければ即終了する)。
#
# report_status.shはラズパイ上のqzss-mapアプリ(localhost)のみ確認して
# いるため、Cloud Run側が落ちている(billing停止・デプロイ失敗等)場合を
# 検知できていなかった。オンライン/オフラインが切り替わった時だけ
# 通知する(温度監視と同じedge-trigger方式で、毎回の通知連打を防ぐ)。
#
# 使い方: systemdタイマー(qzss-cloud-health-check.timer)で定期実行する。
#   手動で今すぐ確認したい場合はそのまま実行するだけでよい:
#     ./cloud_health_check.sh
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$DIR/update_state"
LOG_FILE="$STATE_DIR/cloud_health_check.log"
mkdir -p "$STATE_DIR"

if [ -f "$DIR/qzss.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$DIR/qzss.env"
  set +a
fi

[ -z "${QZSS_CLOUD_URL:-}" ] && exit 0

notify_discord() {
  local message="$1"
  [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0
  local hostname_str full_text
  hostname_str="$(hostname)"
  full_text="☁️ QZSS クラウド死活監視 (${hostname_str})
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
    > /dev/null 2>&1
}

CLOUD_BASE_URL="${QZSS_CLOUD_URL%/ingest}"
CLOUD_STATE_FILE="$STATE_DIR/cloud_notify_state"
last_cloud_state="unknown"
[ -f "$CLOUD_STATE_FILE" ] && last_cloud_state="$(cat "$CLOUD_STATE_FILE")"

# 30秒おきに動くので、1回ごとのチェック自体は1回のcurlだけにして軽くする
# (report_status.shの10回リトライのような重い作りにはしない)
if curl -fs --max-time 10 "$CLOUD_BASE_URL/" > /dev/null 2>&1; then
  current_cloud_state="online"
else
  current_cloud_state="offline"
fi

# 状態が変わらなくても、実際に毎回チェックが動いていることを目視で
# 確認できるよう、実行のたびに1行だけ記録する(Discord通知は状態が
# 変わった時だけ、というedge-trigger方針とは別に、ログだけは常時記録する)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] チェック実行: ${current_cloud_state}" >> "$LOG_FILE"

if [ "$current_cloud_state" != "$last_cloud_state" ]; then
  if [ "$current_cloud_state" = "offline" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 クラウド(${CLOUD_BASE_URL})が応答しません" \
      | tee -a "$LOG_FILE"
    notify_discord "🚨 ${CLOUD_BASE_URL} が応答しません(オフライン)。Cloud Runのデプロイ状況・課金停止等を確認してください。"
  elif [ "$last_cloud_state" != "unknown" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ クラウド(${CLOUD_BASE_URL})が復旧しました" \
      | tee -a "$LOG_FILE"
    notify_discord "✅ ${CLOUD_BASE_URL} が復旧しました(オンライン)。"
  fi
fi
echo "$current_cloud_state" > "$CLOUD_STATE_FILE"
