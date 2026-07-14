#!/bin/bash
# 緊急更新の合図を確認する軽量チェック。頻繁に(既定15分おき)実行される
# ことを前提にしているため、通常時のコストを極力小さくしてある
# (git fetchだけで、変化が無ければ即終了する)。
#
# 使い方(開発側/リモート側):
#   重大なバグを直したので今すぐラズパイに反映したい場合、
#   qzss_pi_package/URGENT_UPDATE の中身を書き換えてcommit・pushする。
#   例:
#     echo "最終更新: $(date '+%Y-%m-%d %H:%M') 深刻な地図描画バグの緊急修正" \
#       >> qzss_pi_package/URGENT_UPDATE
#     git add qzss_pi_package/URGENT_UPDATE
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

cd "$DIR" || exit 1
git fetch origin main --quiet 2>/dev/null || exit 0

remote_content="$(git show origin/main:qzss_pi_package/URGENT_UPDATE 2>/dev/null || true)"
[ -z "$remote_content" ] && exit 0

seen_content=""
[ -f "$SEEN_FILE" ] && seen_content="$(cat "$SEEN_FILE")"

if [ "$remote_content" != "$seen_content" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 緊急更新の合図を検知しました。今すぐ更新します" \
    | tee -a "$STATE_DIR/update_check.log"
  echo "$remote_content" > "$SEEN_FILE"
  "$DIR/update_check.sh"
fi
