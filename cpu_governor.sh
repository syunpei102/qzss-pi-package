#!/bin/bash
# CPUガバナーを QZSS_CPU_GOVERNOR(既定: ondemand)に設定する。
# 指定したガバナーがその機種のカーネルで使えない場合は何もしない
# (常時performanceへ勝手にフォールバックしない)。
set -uo pipefail

GOVERNOR="${QZSS_CPU_GOVERNOR:-ondemand}"
case "$GOVERNOR" in
  performance)
    echo "⚠️ performance(常時最大クロック)は発熱・消費電力が増えるため既定では使いません。明示的に指定された場合のみ設定します" >&2 ;;
esac

status=0
for dir in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
  [ -d "$dir" ] || continue
  available="$(cat "$dir/scaling_available_governors" 2>/dev/null || true)"
  case " $available " in
    *" $GOVERNOR "*) ;;
    *) echo "ℹ️ $dir: ガバナー $GOVERNOR は利用できません(利用可能: ${available:-不明})" >&2; continue ;;
  esac
  if ! echo "$GOVERNOR" > "$dir/scaling_governor"; then
    echo "❌ $dir: ガバナーの設定に失敗しました" >&2
    status=1
  fi
done
exit "$status"
