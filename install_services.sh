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
# qzss-map / qzss-decoder はユーザー名を差し替えられるテンプレート
# ユニット(@)のまま配置する。更新チェック系はタイマーとの紐付けを
# 単純にするため、%i をこのユーザー名に直接置き換えた通常ユニットにする
for f in "$DIR"/systemd/qzss-map.service "$DIR"/systemd/qzss-decoder.service; do
  name="$(basename "$f" .service)"
  sudo cp "$f" "/etc/systemd/system/${name}@.service"
done
for f in "$DIR"/systemd/qzss-update-check.service "$DIR"/systemd/qzss-urgent-check.service; do
  name="$(basename "$f")"
  sed "s/%i/$USER_NAME/g" "$f" | sudo tee "/etc/systemd/system/$name" > /dev/null
done
for f in "$DIR"/systemd/*.timer; do
  sudo cp "$f" "/etc/systemd/system/$(basename "$f")"
done

echo "🔑 更新スクリプトがsudo無しでサービス再起動できるようにします"
SUDOERS_LINE="$USER_NAME ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart qzss-map@$USER_NAME, /usr/bin/systemctl restart qzss-decoder@$USER_NAME, /usr/bin/systemctl restart qzss-map@$USER_NAME qzss-decoder@$USER_NAME"
echo "$SUDOERS_LINE" | sudo tee "/etc/sudoers.d/qzss-update" > /dev/null
sudo chmod 440 /etc/sudoers.d/qzss-update
sudo visudo -c -f /etc/sudoers.d/qzss-update

echo "🔄 systemdに反映します"
sudo systemctl daemon-reload

echo "▶️  アプリ本体のサービスを有効化・起動します"
sudo systemctl enable --now "qzss-map@$USER_NAME.service"
sudo systemctl enable --now "qzss-decoder@$USER_NAME.service"

echo "⏰ 更新チェックのタイマーを有効化します(毎晩3時 + 緊急時15分おき)"
sudo systemctl enable --now "qzss-update-check.timer"
sudo systemctl enable --now "qzss-urgent-check.timer"

echo ""
echo "✅ セットアップ完了。状態確認コマンド:"
echo "   systemctl status qzss-map@$USER_NAME"
echo "   systemctl status qzss-decoder@$USER_NAME"
echo "   systemctl list-timers | grep qzss"
echo "   tail -f $DIR/update_state/update_check.log"
