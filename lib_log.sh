#!/bin/bash
# ログファイルが際限なく増えてSDカードを消費しないようにする共通関数。
# 各スクリプトから `source "$DIR/lib_log.sh"` で読み込む。
#
# rotate_log <ファイル> [上限バイト数=524288]
#   上限を超えていたら「<ファイル>.1」へ退避する(世代は1つだけ。古い .1 は上書き)。
#   実行のたびに呼んでも，超えていなければstat 1回だけで済む軽い処理。
rotate_log() {
  local file="$1" max="${2:-524288}" size
  [ -f "$file" ] || return 0
  size="$(wc -c < "$file" 2>/dev/null | tr -d ' ')"
  if [ "${size:-0}" -gt "$max" ]; then
    mv -f "$file" "$file.1"
  fi
  return 0
}
