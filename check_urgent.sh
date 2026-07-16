#!/bin/bash
# 緊急更新の合図を確認する軽量チェック。頻繁に(既定15分おき)実行される
# ことを前提にしているため、通常時のコストを極力小さくしてある
# (git fetchだけで、変化が無ければ即終了する)。
#
# 使い方(開発側/リモート側):
#   重大なバグを直したので今すぐラズパイに反映したい場合、このリポジトリ
#   (qzss-pi-package)直下の URGENT_UPDATE の中身を書き換えてcommit・push
#   する。例(qzss-pi-packageのリポジトリ直下で実行):
#     echo "最終更新: $(date '+%Y-%m-%d %H:%M') 深刻な地図描画バグの緊急修正" \
#       >> URGENT_UPDATE
#     git add URGENT_UPDATE
#     git commit -m "緊急更新の合図"
#     git push
#
# これだけで、次回のcheck_urgent.sh実行時(最短15分以内)に
# ラズパイ側が気づいて即座に更新される(夜間の定期更新を待たない)。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$DIR/update_state"
SEEN_FILE="$STATE_DIR/urgent_seen"
mkdir -p "$STATE_DIR"

if [ -f "$DIR/qzss.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$DIR/qzss.env"
  set +a
fi

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

# --- Cloud Run(eq.shum10.com等)自体が外から見て応答するかを確認する。
#     report_status.shはラズパイ上のqzss-mapアプリ(localhost)のみ確認して
#     いるため、Cloud Run側が落ちている(billing停止・デプロイ失敗等)場合を
#     検知できていなかった。オンライン/オフラインが切り替わった時だけ
#     通知する(温度監視と同じedge-trigger方式で、毎回の通知連打を防ぐ) ---
if [ -n "${QZSS_CLOUD_URL:-}" ]; then
  CLOUD_BASE_URL="${QZSS_CLOUD_URL%/ingest}"
  CLOUD_STATE_FILE="$STATE_DIR/cloud_notify_state"
  last_cloud_state="unknown"
  [ -f "$CLOUD_STATE_FILE" ] && last_cloud_state="$(cat "$CLOUD_STATE_FILE")"

  cloud_ok=1
  for i in 1 2 3; do
    curl -fs --max-time 10 "$CLOUD_BASE_URL/" > /dev/null 2>&1 && { cloud_ok=0; break; }
    sleep 3
  done

  if [ "$cloud_ok" -eq 0 ]; then
    current_cloud_state="online"
  else
    current_cloud_state="offline"
  fi

  if [ "$current_cloud_state" != "$last_cloud_state" ]; then
    if [ "$current_cloud_state" = "offline" ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 クラウド(${CLOUD_BASE_URL})が応答しません" \
        | tee -a "$STATE_DIR/update_check.log"
      notify_discord "🚨 ${CLOUD_BASE_URL} が応答しません(オフライン)。Cloud Runのデプロイ状況・課金停止等を確認してください。"
    elif [ "$last_cloud_state" != "unknown" ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ クラウド(${CLOUD_BASE_URL})が復旧しました" \
        | tee -a "$STATE_DIR/update_check.log"
      notify_discord "✅ ${CLOUD_BASE_URL} が復旧しました(オンライン)。"
    fi
  fi
  echo "$current_cloud_state" > "$CLOUD_STATE_FILE"
fi

cd "$DIR" || exit 1
git fetch origin main --quiet 2>/dev/null || exit 0

remote_content="$(git show origin/main:URGENT_UPDATE 2>/dev/null || true)"
[ -z "$remote_content" ] && exit 0

seen_content=""
[ -f "$SEEN_FILE" ] && seen_content="$(cat "$SEEN_FILE")"

if [ "$remote_content" != "$seen_content" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 緊急更新の合図を検知しました。今すぐ更新します" \
    | tee -a "$STATE_DIR/update_check.log"
  echo "$remote_content" > "$SEEN_FILE"
  "$DIR/update_check.sh"
fi
