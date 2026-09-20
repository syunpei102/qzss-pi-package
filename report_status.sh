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
# shellcheck disable=SC1091
source "$DIR/lib_log.sh"
rotate_log "$LOG_FILE"

# 手動実行とタイマー実行が重なって，同じコマンドを二重に処理しないようにする
exec 7>"$STATE_DIR/report_status.lock"
flock -n 7 || exit 0

PY="$DIR/venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
INBOX="$DIR/command_inbox.py"

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

# --- 温度チェック(閾値超過を検知した最初の1回だけDiscordへ通知する。
#     高い状態が続いている間、毎時間繰り返し通知しないよう、直前の
#     状態をファイルに記録して「状態が変わった時だけ」通知する。
#     warn→criticalへ悪化した場合は改めて通知し、閾値を下回ったら
#     マーカーをリセットする(次に超えたらまた通知される)) ---
TEMP_STATE_FILE="$STATE_DIR/temp_notify_state"
last_temp_state="none"
[ -f "$TEMP_STATE_FILE" ] && last_temp_state="$(cat "$TEMP_STATE_FILE")"

if [ -n "$TEMPERATURE" ]; then
  temp_int="${TEMPERATURE%.*}"
  if [ "$temp_int" -ge "$TEMP_CRITICAL" ] 2>/dev/null; then
    current_temp_state="critical"
  elif [ "$temp_int" -ge "$TEMP_WARN" ] 2>/dev/null; then
    current_temp_state="warn"
  else
    current_temp_state="none"
  fi

  if [ "$current_temp_state" != "none" ] && [ "$current_temp_state" != "$last_temp_state" ]; then
    if [ "$current_temp_state" = "critical" ]; then
      log "🚨 本体温度が危険域です: ${TEMPERATURE}℃"
      notify_discord "🚨 本体温度が危険域です: ${TEMPERATURE}℃(閾値: ${TEMP_CRITICAL}℃)。サーマルスロットリングにより性能低下・不安定化のおそれがあります。設置場所の通気を確認してください。"
    else
      log "⚠️ 本体温度が高めです: ${TEMPERATURE}℃"
      notify_discord "⚠️ 本体温度が高めです: ${TEMPERATURE}℃(閾値: ${TEMP_WARN}℃)。しばらく様子を見てください(温度が下がるか、危険域に達したら改めて通知します)。"
    fi
  elif [ "$current_temp_state" = "none" ] && [ "$last_temp_state" != "none" ]; then
    log "✅ 本体温度が正常範囲に戻りました: ${TEMPERATURE}℃"
  fi

  echo "$current_temp_state" > "$TEMP_STATE_FILE"
fi

# --- 主要サービスの生存確認(念のための保険。本来はsystemdの
#     Restart=on-failure + StartLimitIntervalSec=0 により自動復帰する
#     はずだが、手動停止やmask等で止まったままになっていないかも確認する) ---
SERVICE_USER="$(whoami)"
check_and_heal_service() {
  local unit="$1"
  systemctl is-active --quiet "$unit" && return 0
  log "⚠️ $unit が停止しています。再起動を試みます"
  if sudo -n systemctl restart "${unit%.service}" 2>/dev/null; then
    log "✅ $unit を再起動しました"
    notify_discord "⚠️ $unit が停止していたため自動再起動しました。頻発する場合は本体の点検をお願いします。"
  else
    log "❌ $unit の自動再起動に失敗しました(sudo権限不足の可能性)"
    notify_discord "🚨 $unit が停止していますが自動再起動に失敗しました。手動での確認をお願いします。"
  fi
}
check_and_heal_service "qzss-map@${SERVICE_USER}.service"
check_and_heal_service "qzss-decoder@${SERVICE_USER}.service"

# --- qzss-mapが実際にHTTP応答するか確認する(上のcheck_and_heal_serviceは
#     「systemdがactiveと言っているか」しか見ないため、プロセスは起動して
#     いても中身が壊れていて応答しないケースを検知できない)。
#     応答が無ければ、まず同じコードでの再起動を試み、それでも直らない
#     場合は最後に動作確認できた安定版(last_known_good、update_check.shが
#     更新成功時・本チェックの成功時に記録する)へ自動的に切り替える。
#     OTA更新の直後だけでなく、それ以外の理由で壊れた場合にも効く保険 ---
HTTP_PORT="${HTTP_PORT:-8080}"

http_health_check() {
  local tries=10
  for i in $(seq 1 "$tries"); do
    curl -fs "http://localhost:${HTTP_PORT}/" > /dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

record_last_known_good() {
  for repo_dir in "$MAP_DIR" "$PI_DIR"; do
    local name
    name="$(basename "$repo_dir")"
    [ -d "$repo_dir/.git" ] || continue
    (cd "$repo_dir" && git rev-parse HEAD) > "$STATE_DIR/$name.last_good" 2>/dev/null
  done
}

# すべてのリポジトリを安定版へ戻せたときだけ成功(0)を返す。記録が無い・
# 巻き戻しコマンドが失敗した場合は失敗(1)にする(失敗を成功扱いしない)
rollback_to_last_known_good() {
  local failed=0
  for repo_dir in "$MAP_DIR" "$PI_DIR"; do
    local name good_file good_rev
    name="$(basename "$repo_dir")"
    good_file="$STATE_DIR/$name.last_good"
    if [ ! -f "$good_file" ]; then
      log "⚠️ $name の安定版記録が無いため切り替えられません"
      failed=1
      continue
    fi
    good_rev="$(cat "$good_file")"
    log "⏪ $name を最後に動作確認できた安定版 ${good_rev:0:7} へ切り替えます"
    if ! (cd "$repo_dir" && git reset --hard "$good_rev" --quiet) >> "$LOG_FILE" 2>&1; then
      log "❌ $name の安定版への切り替えに失敗しました"
      failed=1
    fi
  done
  return "$failed"
}

# OTA更新中はサービス再起動で一時的にHTTP応答が途切れる。その間にここで
# 「応答なし→ロールバック」を走らせると更新と競合するため，OTAのロックを
# 取れたときだけ死活確認・復旧を行う
health_check_and_recover() {
if http_health_check; then
  record_last_known_good
else
  log "⚠️ qzss-map がHTTP応答しません。再起動を試みます"
  sudo systemctl restart "qzss-map@${SERVICE_USER}" "qzss-decoder@${SERVICE_USER}" 2>&1 | tee -a "$LOG_FILE"
  if http_health_check; then
    log "✅ 再起動で復旧しました"
    record_last_known_good
    notify_discord "⚠️ qzss-mapが応答しなかったため再起動しました。復旧しました。"
  elif rollback_to_last_known_good; then
    sudo systemctl restart "qzss-map@${SERVICE_USER}" "qzss-decoder@${SERVICE_USER}" 2>&1 | tee -a "$LOG_FILE"
    if http_health_check; then
      log "✅ 安定版への切り替えで復旧しました"
      notify_discord "🚨 qzss-mapが応答しなかったため、最後に動作確認できた安定版へ自動的に切り替えて復旧しました。原因(直近の変更内容)を確認してください。"
    else
      log "🚨 安定版に切り替えても応答しません"
      notify_discord "🚨 自動復旧に失敗しました(安定版への切り替え後も応答なし)。至急、実機の確認をお願いします。"
    fi
  else
    notify_discord "🚨 qzss-mapが応答しませんが、安定版の記録が無く自動切り替えできません。至急、実機の確認をお願いします。"
  fi
fi
}

exec 8>"$STATE_DIR/update.lock"
if flock -n 8; then
  health_check_and_recover
  flock -u 8
else
  log "⏭️ OTA更新中のため，死活確認と自動復旧はスキップします"
fi
exec 8>&-

# --- 送信先を決める(QZSS_CLOUD_URLの/ingestを/device/statusに置き換える) ---
if [ -z "${QZSS_CLOUD_URL:-}" ]; then
  log "⚠️ QZSS_CLOUD_URL が未設定のため状態報告をスキップします"
  exit 0
fi
STATUS_URL="${QZSS_CLOUD_URL%/ingest}/device/status"

# 送信本文は全体をPythonのJSONエンコーダーで生成する(ホスト名・コミット等に
# 引用符・バックスラッシュ・改行が含まれても壊れない。数値は検証して不正ならnull)。
# ackすべきコマンドID(supports_ack / acked_command_ids)もここで永続化ファイルから付ける。
# サーバーはこれを受けてコマンドをackし，再配信を止める
if [ -z "$PY" ]; then
  log "⚠️ python3 が無いため状態報告を送れません"
  exit 0
fi
payload="$(DEVICE_ID="$DEVICE_ID" HOSTNAME_STR="$(hostname)" TEMPERATURE="$TEMPERATURE" \
  UPTIME_SEC="$UPTIME_SEC" DISK_FREE_PCT="$DISK_FREE_PCT" \
  GIT_COMMIT_MAP="$GIT_COMMIT_MAP" GIT_COMMIT_PI="$GIT_COMMIT_PI" \
  "$PY" "$DIR/status_payload.py")" || {
  log "⚠️ 状態報告の本文を生成できませんでした"
  exit 0
}

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
# 応答のcommands({id, command})はまず durable inbox へ永続化する(実行前ack)。
# 実行状態は processed ledger に記録し，同じコマンドの二重実行(特にreboot)を防ぐ。
# 詳細とクラッシュ時のトレードオフは command_inbox.py と README.md を参照
if [ -z "$PY" ]; then
  log "⚠️ python3 が無いためリモートコマンドを処理できません"
  exit 0
fi

# 前回の実行中に電源断等で中断されたコマンドを整理する(冪等なものは再実行待ちへ戻り，
# rebootは二重実行を避けて中断扱いにして通知だけ行う)
interrupted="$("$PY" "$INBOX" recover 2>&1)" || log "⚠️ コマンド状態の整理に失敗しました: $interrupted"
if [ -n "$interrupted" ]; then
  log "⚠️ 前回実行中に中断された非冪等コマンドがあります(再実行しません): $interrupted"
  notify_discord "⚠️ 前回の再起動コマンドが実行途中で中断されました(実行されたか不明なため自動では再実行しません)。必要なら再度予約してください。"
fi

if ! printf '%s' "$response" | "$PY" "$INBOX" receive; then
  log "⚠️ コマンド応答の保存に失敗しました。今回はコマンドを実行しません"
  exit 0
fi

while IFS=$'\t' read -r cmd_id cmd; do
  [ -n "$cmd_id" ] || continue
  case "$cmd" in
    reboot)
      log "🔄 再起動コマンドを受信しました(id=$cmd_id)。再起動します"
      # 非冪等: 実行中の印を永続化してから実行する(以後は再実行しない)
      if ! "$PY" "$INBOX" start "$cmd_id"; then
        log "❌ 実行状態を保存できないため再起動しません(id=$cmd_id)"
        continue
      fi
      notify_discord "再起動を要求します(実際に再起動したら，起動後に完了を通知します)。"
      touch "$STATE_DIR/reboot_requested"
      # systemctl rebootは要求を送るとすぐ制御を返す(実際のシャットダウンは
      # systemd自身が引き継ぐ)ため，バックグラウンド化・遅延は不要
      if sudo -n /usr/bin/systemctl reboot; then
        "$PY" "$INBOX" finish "$cmd_id" done || true
      else
        # 失敗したのに「再起動を予約した」状態を残さない
        rm -f "$STATE_DIR/reboot_requested"
        "$PY" "$INBOX" finish "$cmd_id" failed || true
        log "❌ 再起動コマンドの実行に失敗しました(sudo権限不足の可能性)"
        notify_discord "🚨 再起動を要求しましたが実行に失敗しました(sudo権限を確認してください)。"
      fi
      ;;
    force_update_check)
      log "🔍 管理サイトからの更新確認コマンドを受信しました(id=$cmd_id)"
      "$PY" "$INBOX" start "$cmd_id" || true
      # update_check.sh自体は「更新が見つかった場合」しかDiscordへ通知しない。
      # 手動要求の経路でだけ，更新が無かった場合の完了通知を追加する。
      # 終了コードで成功・排他中・失敗を区別し，失敗を「更新なし」と誤通知しない
      update_output="$("$DIR/update_check.sh")"
      update_status=$?
      echo "$update_output"
      case "$update_status" in
        0)
          "$PY" "$INBOX" finish "$cmd_id" done || true
          if echo "$update_output" | grep -q "更新はありませんでした"; then
            notify_discord "✅ 更新確認を実行しました。更新はありませんでした(既に最新版です)。"
          fi
          ;;
        75)
          # 別のOTAが実行中。そちらが結果を通知するので，こちらは完了扱いにする
          "$PY" "$INBOX" finish "$cmd_id" done || true
          notify_discord "⏭️ 別の更新処理が実行中のため，更新確認はそちらに任せます。"
          ;;
        *)
          "$PY" "$INBOX" finish "$cmd_id" failed || true
          log "❌ 更新確認に失敗しました(終了コード $update_status)"
          notify_discord "🚨 更新確認に失敗しました(終了コード $update_status)。ログ: update_state/update_check.log"
          ;;
      esac
      ;;
    *)
      log "⚠️ 未対応のコマンドを受信しました: $cmd"
      "$PY" "$INBOX" finish "$cmd_id" failed || true
      ;;
  esac
done < <("$PY" "$INBOX" pending)
