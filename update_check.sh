#!/bin/bash
# 組み込み機器と同じ「pull型OTA更新」。ラズパイ側が定期的に(systemdタイマー
# 経由で)GitHubへ「新しいコミットが無いか」を自分から確認しに行く方式にして
# いるため、ラズパイ側でポートを開けたりSSHを外部公開したりする必要が一切ない。
#
# 動作:
#   1. qzss-map / qzss-pi-package それぞれで `git fetch` し、
#      リモート(origin/main)に新しいコミットがあるか確認する
#   2. あれば現在のコミットを記録した上で `git reset --hard origin/main`
#      (取得した通りの内容にきっちり合わせる。手元での改変は前提にしない)
#   3. package.json / requirements.txt が変わっていれば依存関係を入れ直す
#   4. 関連サービスを再起動する
#   5. 再起動後、地図アプリが正常に応答するか確認する。応答が無ければ
#      直前のコミットに自動的に巻き戻し(ロールバック)、再度サービスを
#      再起動する(壊れた更新を適用したまま放置しない)
#
# 使い方: cronまたはsystemdタイマーで定期実行する(例: 10分おき)。
#   手動で今すぐ確認したい場合はそのまま実行するだけでよい:
#     ./update_check.sh
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$DIR")"
MAP_DIR="${MAP_DIR:-$PARENT_DIR/qzss-map}"
PI_DIR="${PI_DIR:-$DIR}"
HTTP_PORT="${HTTP_PORT:-8080}"
STATE_DIR="$DIR/update_state"
LOG_FILE="$STATE_DIR/update_check.log"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1091
source "$DIR/lib_log.sh"
rotate_log "$LOG_FILE"

# qzss.env に DISCORD_WEBHOOK_URL 等を書いている場合はここで読み込む
# (systemdサービス経由でも手動実行でも同じように効くようにする)
if [ -f "$DIR/qzss.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$DIR/qzss.env"
  set +a
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# OTAは同時に1つだけ実行する(タイマー・緊急チェック・Discordの更新確認コマンドが
# 重なると，リポジトリの強制更新・npm/pip install・サービス再起動・ロールバックが
# 競合して壊れた状態になり得る)。ロックはプロセス終了時に自動で解放される。
# 他が実行中なら，失敗ではなく「使用中」を表す終了コード75で終わる
LOCK_FILE="$STATE_DIR/update.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "⏭️ 別の更新処理が実行中のため，今回はスキップします"
  exit 75
fi

# コミットハッシュ(例: 761f1b3)は技術者以外には何のことか分からないため、
# VERSIONファイルがあればそちらの番号(例: ver1.0 → ver1.1)で表示する。
# qzss-mapにはVERSIONファイルがあるが、qzss-pi-package(拠点の制御
# プログラム、ユーザーが直接バージョンを意識する機会が少ない)には無いため、
# そちらは従来通り短縮ハッシュ表示にフォールバックする
format_update_range() {
  local repo_dir="$1" old_rev="$2" new_rev="$3"
  local old_version new_version
  old_version="$(cd "$repo_dir" && git show "$old_rev:VERSION" 2>/dev/null)"
  new_version="$(cd "$repo_dir" && git show "$new_rev:VERSION" 2>/dev/null)"
  if [ -n "$old_version" ] && [ -n "$new_version" ]; then
    echo "ver${old_version} → ver${new_version}"
  else
    echo "${old_rev:0:7} → ${new_rev:0:7}"
  fi
}

# 更新の成功・失敗・ロールバック等をDiscordに通知する(Discordが
# デバイス操作・状態確認の主な窓口になったため、更新が無かった場合を
# 除き結果は毎回通知する)
notify_discord() {
  local message="$1"
  [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0
  local hostname_str full_text
  hostname_str="$(hostname)"
  full_text="🚨 QZSS 更新 (${hostname_str})
${message}"
  # jqがあれば安全にJSONエスケープする。無ければ最低限(バックスラッシュ・
  # ダブルクォート・改行)だけ手動エスケープするフォールバックにする
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

# systemdサービスを使っている場合はそちらを再起動し、使っていない場合
# (start_pi_local.sh等を手動起動している場合)は何もしない。
# 判定はコマンド置換で出力を変数に受けてからgrepする(set -o pipefail下で
# `systemctl ... | grep -q ...`のようにパイプへ直接流すと、grep -qが
# 一致した時点で早期終了してパイプを閉じ、書き込み中のsystemctlがSIGPIPEで
# 非0終了することがあり、それがpipefailによって「見つからなかった」
# 判定になってしまう=実際には存在するのに再起動がスキップされるバグが
# あった。実機での初回OTAテストで発覚)
restart_services() {
  local unit_files
  unit_files="$(systemctl list-unit-files 2>/dev/null)"
  if printf '%s' "$unit_files" | grep -q "qzss-map@"; then
    log "サービスを再起動します(qzss-map, qzss-decoder)"
    sudo systemctl restart "qzss-map@$(whoami)" "qzss-decoder@$(whoami)" 2>&1 | tee -a "$LOG_FILE" || return 1
    # qzss-map/qzss-decoderを再起動しても、キオスクのChromiumは既存タブに
    # 古いpublic/main.js等をメモリ上に持ったまま動き続ける(ブラウザは
    # サーバー側ファイルの更新を勝手に検知して再読み込みはしない)。
    # クライアント側の修正が実機に反映されないまま「更新は成功した」と
    # 誤認する事故が実際に起きたため、キオスク表示側も必ず再起動して
    # ページを読み込み直させる
    if printf '%s' "$unit_files" | grep -q "qzss-kiosk@"; then
      log "キオスク表示(Chromium)を再起動します(qzss-kiosk)"
      sudo systemctl restart "qzss-kiosk@$(whoami)" 2>&1 | tee -a "$LOG_FILE"
    fi
  else
    log "⚠️ systemdサービスが見つかりません．手動確認が必要です"
    return 1
  fi
}

# 地図アプリが実際に応答するかを確認する(プロセスが起動していても、
# 中身が壊れていて応答しないケースを検知するため)
health_check() {
  local tries=10
  for i in $(seq 1 "$tries"); do
    if curl --max-time 5 -fs "http://localhost:${HTTP_PORT}/" > /dev/null 2>&1 \
      && systemctl is-active --quiet "qzss-decoder@$(whoami).service"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

install_node_dependencies() {
  if [ -f package-lock.json ]; then
    npm ci --omit=dev 2>&1 | tee -a "$LOG_FILE"
  else
    npm install --omit=dev 2>&1 | tee -a "$LOG_FILE"
  fi
}

# 戻り値：0=更新成功，1=変更なし，2=変更前の失敗，3=変更後の失敗．
# 1つのリポジトリを更新する。更新した場合は0(更新あり)、
# 更新が無かった場合は1を返す。ロールバック用に更新前のコミットを
# $STATE_DIR/<リポジトリ名>.prev に記録する
update_repo() {
  local repo_dir="$1"
  local name
  name="$(basename "$repo_dir")"

  if [ ! -d "$repo_dir/.git" ]; then
    log "⚠️ $repo_dir はgitリポジトリではありません。スキップします"
    return 1
  fi

  cd "$repo_dir" || return 2
  git fetch origin main --quiet 2>&1 | tee -a "$LOG_FILE" || return 2

  local local_rev remote_rev
  local_rev="$(git rev-parse HEAD)" || return 2
  remote_rev="$(git rev-parse origin/main)" || return 2

  if [ "$local_rev" = "$remote_rev" ]; then
    return 1
  fi

  log "🆕 $name に更新があります: $(format_update_range "$repo_dir" "$local_rev" "$remote_rev")"
  echo "$local_rev" > "$STATE_DIR/$name.prev" || return 2

  local before_pkg before_req
  before_pkg="$(md5sum package.json package-lock.json 2>/dev/null || true)"
  before_req="$( [ -f requirements.txt ] && md5sum requirements.txt || true)"

  git reset --hard origin/main --quiet 2>&1 | tee -a "$LOG_FILE" || return 3

  if [ -f package.json ] && [ "$before_pkg" != "$(md5sum package.json package-lock.json 2>/dev/null || true)" ]; then
    log "📦 package.json が変わったため npm install します($name)"
    install_node_dependencies || return 3
  fi
  if [ -f requirements.txt ] && [ "$before_req" != "$(md5sum requirements.txt)" ]; then
    log "🐍 requirements.txt が変わったため pip install します($name)"
    ./venv/bin/pip install -q -r requirements.txt 2>&1 | tee -a "$LOG_FILE" || return 3
  fi

  return 0
}

# 現在のコミットを「最後に動作確認できた安定版」として記録する。
# report_status.sh側の自動ロールバック(OTA更新のタイミング以外で
# 壊れた場合の保険)が、切り替え先としてこれを参照する
record_last_known_good() {
  for repo_dir in "$MAP_DIR" "$PI_DIR"; do
    local name
    name="$(basename "$repo_dir")"
    [ -d "$repo_dir/.git" ] || continue
    (cd "$repo_dir" && git rev-parse HEAD) > "$STATE_DIR/$name.last_good" 2>/dev/null
  done
}

# 記録しておいた直前のコミットに戻す
rollback_repo() {
  local repo_dir="$1"
  local name
  name="$(basename "$repo_dir")"
  local prev_file="$STATE_DIR/$name.prev"

  if [ ! -f "$prev_file" ]; then
    log "⚠️ $name のロールバック先が記録されていません。手動確認が必要です"
    return 1
  fi

  local prev_rev current_rev
  prev_rev="$(cat "$prev_file")"
  current_rev="$(cd "$repo_dir" && git rev-parse HEAD)"
  log "⏪ $name をロールバックします: $(format_update_range "$repo_dir" "$current_rev" "$prev_rev")"
  cd "$repo_dir" || return 1
  git reset --hard "$prev_rev" --quiet 2>&1 | tee -a "$LOG_FILE" || return 1
  # コードだけでなく，更新途中で変わった依存関係も戻す．
  if [ -f package.json ]; then install_node_dependencies || return 1; fi
  if [ -f requirements.txt ]; then
    ./venv/bin/pip install -q -r requirements.txt 2>&1 | tee -a "$LOG_FILE" || return 1
  fi
  return 0
}

# リポジトリの systemd/ の変更(新しいタイマー・ユニット内容の修正・廃止)を実機へ反映する。
# ユニットの配置とenableは，install_services.shが root 所有で配置した専用ヘルパー
# (sudoersで許可済み)に任せる。ヘルパーは全ユニットを検証してから1つずつ原子的に
# 差し替える。反映後に daemon-reload し，変更されたユニットをenableする。
# ヘルパーが未導入の端末(install_services.shを再実行していない)では警告のみ。
UNIT_HELPER="${QZSS_UNIT_HELPER:-/usr/local/sbin/qzss-unit-helper}"
sync_systemd_units() {
  [ -d "$PI_DIR/systemd" ] || return 0
  if [ ! -x "$UNIT_HELPER" ]; then
    log "⚠️ $UNIT_HELPER が無いためsystemdユニットは反映しません(install_services.shの再実行が必要です)"
    return 0
  fi
  local result changed_units
  result="$(sudo -n "$UNIT_HELPER" install "$PI_DIR/systemd" 2>&1)" || {
    log "❌ systemdユニットの配置に失敗しました: $result"
    return 1
  }
  [ -n "$result" ] || return 0
  log "🧩 systemdユニットを更新しました: $(printf '%s' "$result" | tr '\n' ' ')"
  sudo -n systemctl daemon-reload 2>&1 | tee -a "$LOG_FILE"
  [ "${PIPESTATUS[0]}" -eq 0 ] || { log "❌ systemctl daemon-reload に失敗しました"; return 1; }
  changed_units="$(printf '%s\n' "$result" | awk '$1=="changed"{print $2}')"
  if [ -n "$changed_units" ]; then
    # shellcheck disable=SC2086
    sudo -n "$UNIT_HELPER" enable $changed_units 2>&1 | tee -a "$LOG_FILE"
    [ "${PIPESTATUS[0]}" -eq 0 ] || { log "❌ 変更したユニットのenableに失敗しました"; return 1; }
  fi
  return 0
}

log "=== 更新チェック開始 ==="

# それぞれのリポジトリが「今回の実行で実際に更新されたか」を個別に
# 覚えておく(updatedをまとめて1つのフラグにしていた頃は、片方だけ
# 更新された場合でも両方をロールバック対象にしてしまい、今回全く
# 更新していない側まで、前回実行分の古い.prevファイルを使って誤って
# 巻き戻してしまうバグがあった。OTAの自動ロールバックを意図的に
# 壊れた更新でテストした際に実機で発見した)
map_updated=0
pi_updated=0
update_failed=0
update_repo "$MAP_DIR"
map_result=$?
case "$map_result" in
  0) map_updated=1 ;;
  3) map_updated=1; update_failed=1 ;;
  2) update_failed=1 ;;
esac
if [ "$update_failed" -eq 0 ]; then
  update_repo "$PI_DIR"
  pi_result=$?
  case "$pi_result" in
    0) pi_updated=1 ;;
    3) pi_updated=1; update_failed=1 ;;
    2) update_failed=1 ;;
  esac
fi

if [ "$map_updated" -eq 0 ] && [ "$pi_updated" -eq 0 ]; then
  if [ "$update_failed" -ne 0 ]; then
    log "❌ 更新の取得に失敗しました"
    exit 1
  fi
  log "更新はありませんでした"
  exit 0
fi

if [ "$update_failed" -eq 0 ] && [ "$pi_updated" -eq 1 ]; then
  sync_systemd_units || update_failed=1
fi
if [ "$update_failed" -eq 0 ]; then
  restart_services || update_failed=1
fi

log "⏳ 起動確認中…"
if [ "$update_failed" -eq 0 ] && health_check; then
  log "✅ 更新を適用し、正常に起動していることを確認しました"
  success_summary=""
  for repo_dir in "$MAP_DIR" "$PI_DIR"; do
    repo_name="$(basename "$repo_dir")"
    prev_file="$STATE_DIR/$repo_name.prev"
    if [ -f "$prev_file" ]; then
      prev_rev="$(cat "$prev_file")"
      new_rev="$(cd "$repo_dir" && git rev-parse --short HEAD)"
      success_summary="${success_summary}"$'\n'"${repo_name}: $(format_update_range "$repo_dir" "$prev_rev" "$new_rev")"
      # ロールバックが不要になったので.prevを消しておく。残したままだと
      # 次回以降の実行で「今回は更新していないこのリポジトリ」まで、
      # この古い.prevを使って誤ってロールバックされてしまう
      rm -f "$prev_file"
    fi
  done
  notify_discord "✅ 更新を適用しました。${success_summary}"
  record_last_known_good
  exit 0
fi

log "❌ 更新後に地図アプリが応答しません。ロールバックします"
notify_discord "更新後に地図アプリが応答しなくなったため、直前のコミットへロールバックを試みます。"
# 今回実際に更新したリポジトリだけをロールバック対象にする
rollback_failed=0
if [ "$map_updated" -eq 1 ]; then rollback_repo "$MAP_DIR" || rollback_failed=1; fi
if [ "$pi_updated" -eq 1 ]; then
  rollback_repo "$PI_DIR" || rollback_failed=1
  # ユニットも元のコミットの内容へ戻す
  sync_systemd_units || rollback_failed=1
fi
restart_services || rollback_failed=1

if [ "$rollback_failed" -eq 0 ] && health_check; then
  log "✅ ロールバック後、正常に起動していることを確認しました"
  notify_discord "ロールバックにより復旧しました。原因(新しいコミットの内容)を確認してください。\nログ: update_state/update_check.log"
  # ロールバック完了後も.prevを掃除する(残っていると次回以降に誤爆する)
  [ "$map_updated" -eq 1 ] && rm -f "$STATE_DIR/$(basename "$MAP_DIR").prev"
  [ "$pi_updated" -eq 1 ] && rm -f "$STATE_DIR/$(basename "$PI_DIR").prev"
  record_last_known_good
else
  log "🚨 ロールバック後も応答がありません。手動での確認が必要です"
  notify_discord "⚠️ ロールバックしても地図アプリが復旧しません。至急、実機の確認をお願いします。"
fi

# 更新自体は失敗しているため，復旧しても呼び出し元へ非0で通知する．
exit 1
