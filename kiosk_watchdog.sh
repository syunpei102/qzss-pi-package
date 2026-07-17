#!/bin/bash
# キオスク表示(Chromium)のレンダラークラッシュ対策。
#
# Chromiumのタブ内レンダラープロセスがクラッシュしても、親プロセス
# (systemdが監視しているMain PID)自体は生き続けるため、
# qzss-kiosk@.serviceのRestart=on-failureだけでは検知・復旧できない
# (実機で確認済み: エラーコード11のクラッシュ画面のままプロセスは
# 生存し続けた)。
#
# ウィンドウのタイトルで簡易判定する: 正常時は"QZSS 災危通報マップ"、
# クラッシュ画面/白紙は別のタイトル("エラー"・空・"about:blank"等)に
# なるため、期待タイトルと一致しなければ壊れているとみなし、
# Chromium本体を再起動する(systemdが自動的に立ち上げ直す)。
#
# 使い方: systemdタイマー(qzss-kiosk-watchdog.timer)で定期実行する。
set -uo pipefail

EXPECTED_TITLE_PREFIX="QZSS"
DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$DIR/update_state"
LOG_FILE="$STATE_DIR/kiosk_watchdog.log"
CRASH_COUNT_FILE="$STATE_DIR/kiosk_crash_dump_count"
CRASH_REPORTS_DIR="$HOME/.config/chromium/Crash Reports/pending"
mkdir -p "$STATE_DIR"

export DISPLAY=:0

title="$(xdotool getactivewindow getwindowname 2>/dev/null)"

if [ -z "$title" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ ウィンドウタイトルを取得できません(Chromium自体が落ちている可能性。systemdの再起動を待ちます)" \
    | tee -a "$LOG_FILE"
  exit 0
fi

if [[ "$title" != "$EXPECTED_TITLE_PREFIX"* ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 想定外のタイトルを検知しました(\"$title\")。レンダラークラッシュとみなしChromiumを再起動します" \
    | tee -a "$LOG_FILE"
  sudo systemctl restart "qzss-kiosk@$(whoami).service"
  exit 0
fi

# タイトルチェックだけでは不十分だと実機で判明した: レンダラーがクラッシュ
# して「エラー コード: 11」のクラッシュ画面になっても、ウィンドウタイトル
# は直前のページ("QZSS 災危通報マップ")のまま変わらないままだった
# (スクリーンショットで実際に真っ暗+クラッシュ画面を確認)。そのため
# Chromiumが自分で書き出すクラッシュダンプの件数を見て、前回チェック時
# より増えていれば(タイトルが変わらない種類のクラッシュも含めて)検知する
if [ -d "$CRASH_REPORTS_DIR" ]; then
  current_count="$(find "$CRASH_REPORTS_DIR" -maxdepth 1 -name '*.dmp' 2>/dev/null | wc -l | tr -d ' ')"
  last_count="$(cat "$CRASH_COUNT_FILE" 2>/dev/null || echo 0)"
  if [ "$current_count" -gt "$last_count" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 新しいクラッシュダンプを検知しました(${last_count}→${current_count}件)。タイトルは正常に見えても実際にはクラッシュしていた可能性が高いため、Chromiumを再起動します" \
      | tee -a "$LOG_FILE"
    sudo systemctl restart "qzss-kiosk@$(whoami).service"
  fi
  echo "$current_count" > "$CRASH_COUNT_FILE"
fi
