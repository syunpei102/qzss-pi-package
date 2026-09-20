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
#
# 合図を「確認済み」として記録するのは，更新が成功した後だけ。更新に失敗
# したときは記録せず，バックオフ(15分→30分→…最大6時間)を置いて再試行する。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$DIR/update_state"
SEEN_FILE="$STATE_DIR/urgent_seen"
RETRY_FILE="$STATE_DIR/urgent_retry"
LOG_FILE="$STATE_DIR/update_check.log"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1091
source "$DIR/lib_log.sh"

# 失敗した合図を再試行するまでの最小間隔(秒)。失敗のたびに2倍にし上限あり。
# 壊れた更新が「適用→ロールバック」を15分ごとに繰り返さないようにする
RETRY_BASE_SEC=900
RETRY_MAX_SEC=21600

cd "$DIR" || exit 1
git fetch origin main --quiet 2>/dev/null || exit 0

remote_content="$(git show origin/main:URGENT_UPDATE 2>/dev/null || true)"
[ -z "$remote_content" ] && exit 0

seen_content=""
[ -f "$SEEN_FILE" ] && seen_content="$(cat "$SEEN_FILE")"

[ "$remote_content" = "$seen_content" ] && exit 0

# 失敗後のバックオフ(合図の内容ごとに管理する。内容が変われば即座に試す)
now="$(date +%s)"
remote_sum="$(printf '%s' "$remote_content" | cksum | awk '{print $1}')"
retry_sum=""; retry_count=0; retry_next=0
if [ -f "$RETRY_FILE" ]; then
  read -r retry_sum retry_count retry_next < "$RETRY_FILE" || true
fi
if [ "$retry_sum" = "$remote_sum" ]; then
  [ "$now" -lt "${retry_next:-0}" ] && exit 0
else
  retry_count=0
fi

rotate_log "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 緊急更新の合図を検知しました。今すぐ更新します" \
  | tee -a "$LOG_FILE"
"$DIR/update_check.sh"
status=$?

case "$status" in
  0)
    # 更新が成功(または既に最新)したときだけ「確認済み」にする
    printf '%s\n' "$remote_content" > "$SEEN_FILE"
    rm -f "$RETRY_FILE"
    ;;
  75)
    # 別のOTAが実行中。失敗ではないので次回(15分後)にもう一度確認する
    ;;
  *)
    retry_count=$((retry_count + 1))
    delay=$((RETRY_BASE_SEC * (1 << (retry_count - 1))))
    [ "$delay" -gt "$RETRY_MAX_SEC" ] && delay="$RETRY_MAX_SEC"
    echo "$remote_sum $retry_count $((now + delay))" > "$RETRY_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 緊急更新に失敗しました(終了コード $status)。${delay}秒後以降に再試行します" \
      | tee -a "$LOG_FILE"
    ;;
esac
exit "$status"
