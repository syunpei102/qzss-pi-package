#!/bin/bash
# ラズパイ上で一度だけ実行する。systemdサービス/タイマーを登録し、
# 電源投入で自動起動・クラッシュ時は自動再起動・OTA更新を有効にする。
#
# 前提:
#   ~/qzss/qzss-map/         (地図アプリ)
#   ~/qzss/qzss-pi-package/  (このリポジトリ)
# が並んでcloneされていること(SETUP.md参照)。
#
# 使い方: ./install_services.sh
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"

if [ ! -f "$DIR/qzss.env" ]; then
  echo "❌ qzss.env が見つかりません。先に用意してください:"
  echo "   cp $DIR/qzss.env.example $DIR/qzss.env"
  echo "   nano $DIR/qzss.env  # シリアルポート等を記入"
  exit 1
fi

echo "📄 systemdユニットファイルを配置します(sudoが必要です)"
# 配置・有効化はroot所有の専用ヘルパーに任せる(OTAも同じヘルパーで反映する)。
# ヘルパーは全ユニットを生成・検証してから1ファイルずつ原子的に差し替え，
# qzss-map / qzss-decoder / qzss-kiosk は name@.service のテンプレートに，
# それ以外は %i をこのユーザー名に置き換えた通常ユニットにする
HELPER=/usr/local/sbin/qzss-unit-helper
sudo install -o root -g root -m 755 "$DIR/qzss_unit_helper.sh" "$HELPER"

echo "🔑 更新スクリプトがsudo無しでサービス再起動・本体再起動できるようにします"
SUDOERS_LINE="$USER_NAME ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart qzss-map@$USER_NAME, /usr/bin/systemctl restart qzss-decoder@$USER_NAME, /usr/bin/systemctl restart qzss-map@$USER_NAME qzss-decoder@$USER_NAME, /usr/bin/systemctl restart qzss-kiosk@$USER_NAME, /usr/bin/systemctl reboot, /usr/bin/systemctl daemon-reload, /usr/local/sbin/qzss-unit-helper, /usr/bin/systemctl restart lightdm, /sbin/modprobe -r brcmfmac brcmfmac_cyw brcmutil, /sbin/modprobe brcmfmac"
echo "$SUDOERS_LINE" | sudo tee "/etc/sudoers.d/qzss-update" > /dev/null
sudo chmod 440 /etc/sudoers.d/qzss-update
sudo visudo -c -f /etc/sudoers.d/qzss-update

echo "🧩 ユニットを配置します(qzss-reception-watch.service 等を含む全ユニット)"
sudo "$HELPER" install "$DIR/systemd"

echo "🔄 systemdに反映します"
sudo systemctl daemon-reload

echo "▶️  アプリ本体のサービスを有効化・起動します"
sudo systemctl enable --now "qzss-map@$USER_NAME.service"
sudo systemctl enable --now "qzss-decoder@$USER_NAME.service"

echo "🖥️  キオスク表示を有効化します(クラッシュ時も自動再起動)"
sudo systemctl enable --now "qzss-kiosk@$USER_NAME.service"

echo "⏰ 更新チェックのタイマーを有効化します(毎晩3時 + 緊急時15分おき)"
sudo systemctl enable --now "qzss-update-check.timer"
sudo systemctl enable --now "qzss-urgent-check.timer"

echo "📡 状態報告タイマーを有効化します(5分おき。温度・リモートコマンド受信)"
sudo systemctl enable --now "qzss-report-status.timer"

echo "☁️  クラウド死活監視タイマーを有効化します(2分おき)"
sudo systemctl enable --now "qzss-cloud-health-check.timer"

echo "⚡ CPUガバナーを負荷追従(ondemand)にします(旧: 常時performanceは廃止)"
sudo systemctl enable --now "qzss-cpu-governor.service"

echo "🖥️  キオスクのレンダラークラッシュ監視タイマーを有効化します(30秒おき)"
sudo systemctl enable --now "qzss-kiosk-watchdog.timer"

echo "🔄 キオスクの定期リロードタイマーを有効化します(毎晩4時。クラッシュしないまま機能が徐々に壊れる問題の予防)"
sudo systemctl enable --now "qzss-kiosk-daily-reload.timer"

echo "📶 起動時WiFi復旧チェックを有効化します(wlan0が無ければドライバ再読み込みを試みる)"
sudo systemctl enable --now "qzss-wifi-recovery.service"

echo "📡 受信監視・自動復旧を有効化します(qzss-reception-watch.service を30秒おきに実行)"
sudo systemctl enable --now "qzss-reception-watch.timer"

echo "🐕 ハードウェアウォッチドッグを設定します(OSごとフリーズした場合の自動電源再投入)"
WATCHDOG_REBOOT_NEEDED=0
# Raspberry Pi OS bookworm以降は/boot/firmware/config.txt、それ以前は/boot/config.txt
BOOT_CONFIG=""
if [ -f /boot/firmware/config.txt ]; then
  BOOT_CONFIG="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
  BOOT_CONFIG="/boot/config.txt"
fi
if [ -n "$BOOT_CONFIG" ]; then
  if ! grep -q "^dtparam=watchdog=on" "$BOOT_CONFIG" 2>/dev/null; then
    echo "dtparam=watchdog=on" | sudo tee -a "$BOOT_CONFIG" > /dev/null
    WATCHDOG_REBOOT_NEEDED=1
  fi
else
  echo "⚠️  boot設定ファイルが見つかりません(Raspberry Pi以外の環境の可能性)。ハードウェアウォッチドッグの設定をスキップします"
fi
# BroadcomのSoCウォッチドッグ(bcm2835_wdt)はハード上限が15秒程度のため、
# 余裕を持たせて14秒にする。この値はsystemd(PID1)自身がOSが生きている限り
# 定期的に/dev/watchdogへ書き込むことで維持され、カーネルごとフリーズして
# 書き込みが止まると、この秒数だけ待って強制的に本体を再起動する
if [ -f /etc/systemd/system.conf ] && [ -n "$BOOT_CONFIG" ]; then
  if grep -q "^#\?RuntimeWatchdogSec=" /etc/systemd/system.conf; then
    sudo sed -i 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=14/' /etc/systemd/system.conf
  else
    echo "RuntimeWatchdogSec=14" | sudo tee -a /etc/systemd/system.conf > /dev/null
  fi
  sudo systemctl daemon-reexec 2>/dev/null || true
fi
if [ "$WATCHDOG_REBOOT_NEEDED" -eq 1 ]; then
  echo "⚠️  dtparam=watchdog=on を追加しました。有効化には再起動が必要です(このスクリプトでは自動再起動しません。準備ができたら sudo reboot してください)"
fi

echo ""
echo "✅ セットアップ完了。状態確認コマンド:"
echo "   systemctl status qzss-map@$USER_NAME"
echo "   systemctl status qzss-decoder@$USER_NAME"
echo "   systemctl list-timers | grep qzss"
echo "   tail -f $DIR/update_state/update_check.log"
echo "   tail -f $DIR/update_state/report_status.log"
