#!/bin/bash
# キオスク表示(Chromium)を1日1回リロードする。
#
# クラッシュはしていなくても、Chromiumを再起動せず何日も連続稼働させて
# いると、レイテンシ計測結果の送信(/client-timing)だけが静かに機能
# しなくなる現象を実機で確認した(地図の描画やWebSocket受信は正常な
# ままだったため、既存のkiosk_watchdog.sh(タイトル・クラッシュダンプ
# 監視)では検知できなかった)。--js-flags=--max-old-space-size=128 と
# メモリを絞って動かしている影響と見られる。再起動直後は問題無く動作
# したため、恒久対策としてクラッシュの有無に関わらず定期的にプロセスを
# 作り直すことにした。
#
# 使い方: systemdタイマー(qzss-kiosk-daily-reload.timer)で1日1回実行する。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$DIR/update_state"
LOG_FILE="$STATE_DIR/kiosk_daily_reload.log"
CRASH_COUNT_FILE="$STATE_DIR/kiosk_crash_dump_count"
CRASH_REPORTS_DIR="$HOME/.config/chromium/Crash Reports"
mkdir -p "$STATE_DIR"

# クラッシュダンプ(pending配下、レンダラークラッシュのたびkiosk_watchdog.sh
# が検知に使う診断用ファイル)は自動で消えず溜まり続ける一方で、実機では
# 816件・133MB(数ヶ月分)まで蓄積していたのを確認した。直近の傾向を見る
# 分には数日分あれば十分なので、1日1回のこのタイミングで7日より古い分
# だけ削除する。kiosk_watchdog.sh側のカウント(kiosk_crash_dump_count)も
# 削除後の実件数に合わせておく(ずれていても比較は「増えたか」だけなので
# 実害は無いが、念のため)
if [ -d "$CRASH_REPORTS_DIR/pending" ]; then
  deleted_count="$(find "$CRASH_REPORTS_DIR/pending" -maxdepth 1 -type f -mtime +7 -print 2>/dev/null | wc -l | tr -d ' ')"
  find "$CRASH_REPORTS_DIR/pending" -maxdepth 1 -type f -mtime +7 -delete 2>/dev/null
  remaining_count="$(find "$CRASH_REPORTS_DIR/pending" -maxdepth 1 -name '*.dmp' 2>/dev/null | wc -l | tr -d ' ')"
  echo "$remaining_count" > "$CRASH_COUNT_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🧹 7日より古いクラッシュダンプを削除しました(${deleted_count}件、残り${remaining_count}件)" | tee -a "$LOG_FILE"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔄 定期リロード: Chromiumを再起動します" | tee -a "$LOG_FILE"
sudo systemctl restart "qzss-kiosk@$(whoami).service"
