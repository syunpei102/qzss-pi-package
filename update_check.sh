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

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# systemdサービスを使っている場合はそちらを再起動し、使っていない場合
# (start_pi_local.sh等を手動起動している場合)は何もしない
restart_services() {
  if systemctl list-unit-files 2>/dev/null | grep -q "qzss-map@"; then
    log "サービスを再起動します(qzss-map, qzss-decoder)"
    sudo systemctl restart "qzss-map@$(whoami)" "qzss-decoder@$(whoami)" 2>&1 | tee -a "$LOG_FILE"
  else
    log "⚠️ systemdサービスが見つからないため、再起動はスキップします(手動再起動が必要です)"
  fi
}

# 地図アプリが実際に応答するかを確認する(プロセスが起動していても、
# 中身が壊れていて応答しないケースを検知するため)
health_check() {
  local tries=10
  for i in $(seq 1 "$tries"); do
    if curl -fs "http://localhost:${HTTP_PORT}/" > /dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

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

  cd "$repo_dir" || return 1
  git fetch origin main --quiet 2>&1 | tee -a "$LOG_FILE"

  local local_rev remote_rev
  local_rev="$(git rev-parse HEAD)"
  remote_rev="$(git rev-parse origin/main)"

  if [ "$local_rev" = "$remote_rev" ]; then
    return 1
  fi

  log "🆕 $name に更新があります: ${local_rev:0:7} → ${remote_rev:0:7}"
  echo "$local_rev" > "$STATE_DIR/$name.prev"

  local before_pkg before_req
  before_pkg="$( [ -f package.json ] && md5sum package.json || true)"
  before_req="$( [ -f requirements.txt ] && md5sum requirements.txt || true)"

  git reset --hard origin/main --quiet 2>&1 | tee -a "$LOG_FILE"

  if [ -f package.json ] && [ "$before_pkg" != "$(md5sum package.json)" ]; then
    log "📦 package.json が変わったため npm install します($name)"
    npm install --omit=dev 2>&1 | tee -a "$LOG_FILE"
  fi
  if [ -f requirements.txt ] && [ "$before_req" != "$(md5sum requirements.txt)" ]; then
    log "🐍 requirements.txt が変わったため pip install します($name)"
    ./venv/bin/pip install -q -r requirements.txt 2>&1 | tee -a "$LOG_FILE"
  fi

  return 0
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

  local prev_rev
  prev_rev="$(cat "$prev_file")"
  log "⏪ $name を ${prev_rev:0:7} にロールバックします"
  cd "$repo_dir" || return 1
  git reset --hard "$prev_rev" --quiet 2>&1 | tee -a "$LOG_FILE"
}

log "=== 更新チェック開始 ==="

updated=0
update_repo "$MAP_DIR" && updated=1
update_repo "$PI_DIR" && updated=1

if [ "$updated" -eq 0 ]; then
  log "更新はありませんでした"
  exit 0
fi

restart_services

log "⏳ 起動確認中…"
if health_check; then
  log "✅ 更新を適用し、正常に起動していることを確認しました"
  exit 0
fi

log "❌ 更新後に地図アプリが応答しません。ロールバックします"
rollback_repo "$MAP_DIR"
rollback_repo "$PI_DIR"
restart_services

if health_check; then
  log "✅ ロールバック後、正常に起動していることを確認しました"
else
  log "🚨 ロールバック後も応答がありません。手動での確認が必要です"
fi
