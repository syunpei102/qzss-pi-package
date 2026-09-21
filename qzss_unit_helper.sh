#!/bin/bash
# systemdユニットの配置・有効化を行う root 専用ヘルパー。
#
# install_services.sh が /usr/local/sbin/qzss-unit-helper へ root 所有(755)で
# コピーし，sudoers で「このヘルパーだけ」をパスワードなしで実行できるようにする。
# OTA(update_check.sh)はリポジトリの systemd/ を変更しても，このヘルパー経由で
# 実機へ反映する。ユーザー書き込み可能なリポジトリ内のスクリプトを直接 root で
# 実行させず，また任意のパスへコピーできる汎用の sudo 権限を与えないための構成。
# 受け付けるのは qzss-*.service / qzss-*.timer という名前のファイルだけで，
# 配置先は /etc/systemd/system に固定する。
#
# 使い方(root):
#   qzss-unit-helper install <リポジトリのsystemd/ディレクトリ>
#       全ユニットを一時ディレクトリへ生成・検証してから，1ファイルずつ rename で
#       原子的に差し替える。変更したユニットを「changed <名前>」，廃止して削除した
#       ユニットを「removed <名前>」として1行ずつ標準出力へ出す。
#   qzss-unit-helper enable <changedされたユニット名>...
#       (daemon-reload の後に呼ぶ)timerとインストール対象のserviceを有効化し，
#       変更されたtimer・cpu-governorは再起動して新しい設定を反映する。
set -euo pipefail

UNIT_DIR="/etc/systemd/system"
SYSTEMCTL="systemctl"
# テンプレートユニット(qzss-map@<user>.service 等)。ファイル名を name@.service にする
TEMPLATE_UNITS=" qzss-map qzss-decoder qzss-kiosk "
# 廃止したユニット。実機に残っていたら無効化して削除する
OBSOLETE_UNITS="qzss-cpu-performance.service"
NAME_RE='^qzss-[a-z0-9-]+@?\.(service|timer)$'

if [ "$(id -u)" -eq 0 ]; then
  USER_NAME="${SUDO_USER:-}"
  [ -n "$USER_NAME" ] && [ "$USER_NAME" != "root" ] || { echo "SUDO_USER が必要です" >&2; exit 2; }
else
  # 非rootで動かすのはテスト用。sudo経由(root)では環境変数による差し替えは効かない
  UNIT_DIR="${QZSS_UNIT_DIR:?非rootで実行するにはQZSS_UNIT_DIRが必要です}"
  SYSTEMCTL="${QZSS_SYSTEMCTL:-true}"
  USER_NAME="${QZSS_UNIT_USER:?非rootで実行するにはQZSS_UNIT_USERが必要です}"
fi
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "不正なユーザー名です" >&2; exit 2; }

render() {  # render <src> <basename-without-.service> <kind> -> stdout
  local src="$1" base="$2"
  if [[ "$TEMPLATE_UNITS" == *" $base "* ]]; then
    cat "$src"
  else
    sed "s/%i/$USER_NAME/g" "$src"
  fi
}

installed_name() {  # ソースのファイル名 -> /etc/systemd/system 上の名前
  local file="$1" base
  base="${file%.service}"
  if [[ "$file" == *.service && "$TEMPLATE_UNITS" == *" $base "* ]]; then
    echo "${base}@.service"
  else
    echo "$file"
  fi
}

cmd_install() {
  local src_dir="$1"
  if [ "$(id -u)" -eq 0 ]; then
    local home expected
    home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
    expected="$(realpath "$home/qzss/qzss-pi-package/systemd")"
    [ "$(realpath "$src_dir")" = "$expected" ] || { echo "許可されていないディレクトリです: $src_dir" >&2; exit 2; }
  fi
  [ -d "$src_dir" ] || { echo "ディレクトリがありません: $src_dir" >&2; exit 2; }

  STAGE=""
  STAGE="$(mktemp -d "$UNIT_DIR/.qzss-stage.XXXXXX")"
  trap 'rm -rf "$STAGE"' EXIT

  # 第1段階: すべて生成・検証する(1つでも失敗したら何も配置しない)
  local file name changed=()
  for path in "$src_dir"/qzss-*.service "$src_dir"/qzss-*.timer; do
    [ -e "$path" ] || continue
    file="$(basename "$path")"
    [[ "$file" =~ $NAME_RE ]] || continue
    [ -f "$path" ] && [ ! -L "$path" ] || { echo "通常ファイルではありません: $file" >&2; exit 2; }
    name="$(installed_name "$file")"
    render "$path" "${file%.service}" > "$STAGE/$name"
    grep -q '^\[Unit\]' "$STAGE/$name" || { echo "[Unit]セクションがありません: $file" >&2; exit 2; }
    chmod 644 "$STAGE/$name"
    if ! cmp -s "$STAGE/$name" "$UNIT_DIR/$name" 2>/dev/null; then
      changed+=("$name")
    fi
  done

  # 第2段階: 変更があったものだけ，同一ファイルシステム内のrenameで原子的に差し替える
  for name in ${changed[@]+"${changed[@]}"}; do
    mv -f "$STAGE/$name" "$UNIT_DIR/$name"
    echo "changed $name"
  done

  for name in $OBSOLETE_UNITS; do
    if [ -e "$UNIT_DIR/$name" ]; then
      "$SYSTEMCTL" disable --now "$name" >/dev/null 2>&1 || true
      rm -f "$UNIT_DIR/$name"
      echo "removed $name"
    fi
  done
}

cmd_enable() {
  local name
  for name in "$@"; do
    [[ "$name" =~ $NAME_RE ]] || { echo "不正なユニット名です: $name" >&2; exit 2; }
    [ -e "$UNIT_DIR/$name" ] || continue
    case "$name" in
      *.timer)
        "$SYSTEMCTL" enable "$name"
        "$SYSTEMCTL" restart "$name"   # 未起動なら起動し，スケジュール変更も反映する
        ;;
      qzss-cpu-governor.service)
        "$SYSTEMCTL" enable "$name"
        "$SYSTEMCTL" restart "$name"
        ;;
      *@.service) ;;                   # テンプレートは各インスタンス側で管理する
      *.service)
        if grep -q '^\[Install\]' "$UNIT_DIR/$name"; then
          "$SYSTEMCTL" enable "$name"
        fi
        ;;
    esac
  done
}

case "${1:-}" in
  install) [ $# -eq 2 ] || { echo "使い方: $0 install <systemdディレクトリ>" >&2; exit 2; }; cmd_install "$2" ;;
  enable) shift; cmd_enable "$@" ;;
  *) echo "使い方: $0 install <dir> | enable <unit>..." >&2; exit 2 ;;
esac
