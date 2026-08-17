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
mkdir -p "$STATE_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔄 定期リロード: Chromiumを再起動します" | tee -a "$LOG_FILE"
sudo systemctl restart "qzss-kiosk@$(whoami).service"
