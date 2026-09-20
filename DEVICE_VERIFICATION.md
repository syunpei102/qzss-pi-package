# デバイス管理(Discord操作) 実機動作確認チェックリスト

`report_status.sh`・Discordスラッシュコマンドからの操作は、これまで
構文チェックとローカルcurlでの疎通確認のみで、物理ラズパイでの
エンドツーエンド動作確認がまだ済んでいない。実機投入時に以下を上から
順に確認する。(Web管理画面`/device-admin`は本番では無効化済みのため、
このチェックリストはDiscord経由の操作を確認する内容になっている)

> **2026-07-16、実機(qzss01)で状態報告・OTA(通常/緊急とも)・
> サービス自動再起動まで一通り検証済み。** この過程で2件のバグを発見・
> 修正した: `check_urgent.sh`の`URGENT_UPDATE`パス誤り(緊急更新機能が
> 実装以来ずっと無反応だった)と、`update_check.sh`の`restart_services()`
> が`pipefail`の影響で常に「サービスが見つからない」と誤判定し
> サービス再起動をスキップしていた問題。どちらも修正済み・pushで反映済み。
>
> **同日、キオスク表示(ローカルkiosk)の安定性・軽量化についても実機で
> 検証した。分かったこと:**
> - **GPU(`--use-angle=gl-egl`等)は使わないこと。** Pi 3B+のGPU
>   ドライバ(Mesa/VideoCore IV)がES3.0コンテキスト生成に失敗し、
>   `--type=renderer`のクラッシュダンプが数十分おきに発生した
>   (X11・Wayland両方で確認、部分的なGPU利用でも同様)。
>   `--use-angle=swiftshader --enable-unsafe-swiftshader`(ソフトウェア
>   WebGL)で安定する。単純な`--disable-gpu`はWebGL自体が初期化に
>   失敗するため使わないこと。
> - **Wayland(labwc)よりX11(rpd-x/openbox)の方が大幅に軽い。**
>   labwcの画面合成処理だけで常時CPU 69%前後を消費していたが、
>   X11+openboxに切り替えると合成コストがほぼ0になった
>   (`/etc/lightdm/lightdm.conf`の`user-session`/`autologin-session`を
>   `rpd-labwc`→`rpd-x`に変更)。
> - **DPMS(画面電源管理)を無効化し忘れると、無操作10分で画面が
>   消える。** キオスクはマウス・キーボード操作が一切無いため
>   必ず発生する。`xset s off; xset s noblank; xset -dpms`を
>   autostartに入れておくこと。
> - デスクトップのパネル・アイコン管理・スクリーンセーバー
>   (`lxpanel-pi`/`pcmanfm-pi`/`xscreensaver`)は
>   `~/.config/lxsession/rpd-x/autostart`を空ファイルにして止める
>   (labwcの場合は`~/.config/labwc/autostart`)。

## 0. 前提

```
cd ~/qzss/qzss-pi-package
git pull
./install_services.sh
```

`install_services.sh` はsystemdユニットの再インストール・有効化を行う。
実行後、対象ユニットが有効化されているか確認する:

```
systemctl is-enabled qzss-report-status.timer qzss-reception-watch.timer qzss-cpu-governor.service
systemctl is-enabled "qzss-map@$(whoami).service" "qzss-decoder@$(whoami).service"
```

`qzss-map` / `qzss-decoder` / `qzss-kiosk` はテンプレートユニット(`name@<ユーザー名>.service`)
として配置される。`qzss-map.service` のような`@`なしの名前は存在しないので使わないこと。
OTAで`systemd/`が変わった場合の反映には、root所有ヘルパー
`/usr/local/sbin/qzss-unit-helper` とsudoersが必要(この手順の`install_services.sh`が導入する)。

## 1. `qzss-report-status.timer` が動いているか

```
systemctl status qzss-report-status.timer
systemctl list-timers qzss-report-status.timer
```

`Active: active (waiting)` になっていること。`OnBootSec=2min` なので、
起動後2分以内に初回実行され、以後 `OnUnitActiveSec=5min` で5分おきに実行される
(リモートコマンドの最大遅延は約5分)。

## 2. 手動で1回発火させ、ログにエラーが無いか

```
sudo systemctl start qzss-report-status.service
journalctl -u qzss-report-status.service -n 50 --no-pager
```

`qzss.env` の `QZSS_DEVICE_ID` / `QZSS_INGEST_TOKEN` が正しく読み込まれ、
`report_status.sh` の `log()` 出力(`[日時] ...`)がエラー無く一通り出て
いることを確認する。

## 3. `curl https://eq.shum10.com/device-region/<拠点ID>` で状態が見えるか

Web管理画面が無効化されているため、まずはこのエンドポイント(認証不要)
で、そのデバイスに何か地域設定があるか確認できる。デバイスの温度等の
生データはこのセッションでは公開APIから見えないため、次項のDiscord
コマンドの応答メッセージや`journalctl`のログで実機の値を確認する。

## 4. Discordの `/reboot` コマンドの往復確認

1. Discordで `/reboot device:<拠点ID>` を実行し、autocomplete候補に
   実際のデバイスIDが出ること・実行後にephemeralな確認メッセージ
   (「✅ ... に再起動を予約しました」)が返ることを確認する
2. 実機側で次回の `qzss-report-status.service` 実行(タイマー待ち、また
   は手動で `sudo systemctl start qzss-report-status.service`)を待つ
3. `journalctl -u qzss-report-status.service -n 50` に再起動コマンドを
   受け取ったログが出ているか
4. 実際に実機が再起動されるか(`uptime` がリセットされるか)
5. 再起動後、`qzss-map@<ユーザー名>.service` / `qzss-decoder@<ユーザー名>.service` が自動的に
   立ち上がっているか(`systemctl status`)
6. 再起動完了後の次回`report_status.sh`実行で、Discordに
   「✅ 再起動が完了しました」の通知が届くか(`report_status.sh`の
   `reboot_requested`マーカー検知ロジック)

## 5. Discordの `/update_check` と `/set_region` の往復確認

1. `/update_check device:<拠点ID>` を実行し、ephemeralな確認メッセージ
   が返ることを確認する
2. 次回の状態報告時に `force_update_check` が実行され、GitHubの新着
   コミットがあれば通常の `update_check.sh` と同様に取得・依存関係更新・
   サービス再起動まで走ることを確認する。成功時にDiscordへ
   「✅ 更新を適用しました」の通知が届くことも確認する
3. `/set_region device:<拠点ID> prefecture:東京都` のように実行し、
   `prefecture`のautocomplete候補に47都道府県が出ること・
   `curl https://eq.shum10.com/device-region/<拠点ID>` で関東7都県分の
   `prefectureIds`が返るようになることを確認する

## 6. クラッシュループ耐性の確認

```
sudo systemctl stop "qzss-map@$(whoami).service"
```

の状態でしばらく待ち(次回の `report_status.sh` 実行まで)、以下を確認:

- `report_status.sh` が停止を検知し `sudo systemctl restart qzss-map@<ユーザー名>`(`.service`なし。sudoers定義と
  完全一致する形)を試みているか(ログに残る)
- 復旧に失敗した場合のみDiscordへ通知が飛ぶか(`DISCORD_WEBHOOK_URL` を
  設定している場合)
- 復旧に成功した場合はDiscord通知が飛ばない(正常系なので静かなままで
  良い)ことも合わせて確認

## 6.5 OTA以外のタイミングでの自動ロールバックの確認(非破壊)

`report_status.sh`は、OTA更新の直後だけでなく**毎回の実行時にqzss-mapへ
実際にHTTP応答があるか**を確認する。応答が無ければ再起動→それでも
直らなければ最後に動作確認できた安定版(`update_state/qzss-map.last_good`)
へ自動的に切り替える、という保険が入っている。

**稼働中のcheckoutは壊さない・`git reset --hard`もしない。** 使い捨ての一時cloneと、
`sudo`/`systemctl`を記録だけするスタブで、ロールバックの流れだけを確認する:

```bash
T="$(mktemp -d)"
git clone -q ~/qzss/qzss-pi-package "$T/qzss-pi-package"
git clone -q ~/qzss/qzss-map "$T/qzss-map"
mkdir -p "$T/bin" "$T/qzss-pi-package/update_state"
# sudo/systemctlはコマンドを記録するだけ(本番サービスは再起動されない)
printf '#!/bin/sh\necho "STUB sudo $*" >> "%s/stub.log"\n' "$T" > "$T/bin/sudo"
printf '#!/bin/sh\necho "STUB systemctl $*" >> "%s/stub.log"\nexit 0\n' "$T" > "$T/bin/systemctl"
chmod +x "$T/bin/sudo" "$T/bin/systemctl"
# 安定版=現在のHEAD。一時clone側だけ空コミットを積み、ロールバックで戻ることを見る
git -C "$T/qzss-map" rev-parse HEAD > "$T/qzss-pi-package/update_state/qzss-map.last_good"
git -C "$T/qzss-pi-package" rev-parse HEAD > "$T/qzss-pi-package/update_state/qzss-pi-package.last_good"
git -C "$T/qzss-map" -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m "temp change"
# 誰も待ち受けていないポートを見に行かせ、Cloud/Discordへは送らない
env -u QZSS_CLOUD_URL -u DISCORD_WEBHOOK_URL PATH="$T/bin:$PATH" HTTP_PORT=59999 \
  MAP_DIR="$T/qzss-map" "$T/qzss-pi-package/report_status.sh"
```

1. `$T/qzss-pi-package/update_state/report_status.log` に「HTTP応答しません」
   →「再起動を試みます」→「安定版 ... へ切り替えます」の流れが記録されること
   (待ち受けの無いポートなので最終的に「応答なし」となるのは想定どおり)
2. `git -C "$T/qzss-map" rev-parse HEAD` が `qzss-map.last_good` の値に戻っていること
3. `$T/stub.log` に `STUB sudo systemctl restart qzss-map@...` が記録されていること
4. 稼働中の `~/qzss/qzss-map` / `~/qzss/qzss-pi-package` の `git rev-parse HEAD` が
   実行前と変わっていないこと、本番の `qzss-map@` が再起動されていないこと
5. 後片付け: `rm -rf "$T"`

実サービスでのエンドツーエンド(Discord通知まで)を確認したい場合は、上と同じ一時cloneで
`DISCORD_WEBHOOK_URL` だけをテスト用チャンネルにして実行する。稼働中のコードを
壊す手順は使わない。

## 6.6 状態報告の途絶とオフライン遷移(管理側, qzss-map #23)

サーバーは最後の状態報告から15分(5分×3回)以上途絶えた拠点をオフライン扱いにする。

1. 管理サイトの拠点一覧(`/admin/api/devices` の `online`、またはDiscordの状態表示)で
   対象拠点がオンラインであることを確認する
2. `sudo systemctl stop qzss-report-status.timer` で報告を止め、**10分後もまだオンライン**
   (2回の欠落は許容)、**15分を過ぎるとオフライン**になることを確認する
3. `sudo systemctl start qzss-report-status.timer` で戻し、次回報告(数分以内)で
   オンラインへ復帰することを確認する

## 6.7 受信監視(reception watchdog)

```bash
systemctl list-timers qzss-reception-watch.timer      # 30秒おきに動いている
systemctl cat qzss-reception-watch.service | grep QZSS_DECODER_UNIT   # qzss-decoder@<ユーザー名>
sudo systemctl start qzss-reception-watch.service
journalctl -u qzss-reception-watch.service -n 20 --no-pager   # age=... state=ok
cat update_state/reception_state.json
```

`state=ok` であること。途絶時の復旧動作(USBリセット→デコーダ再起動→物理対応要請)は、
保守時間帯に限り `sudo systemctl stop "qzss-decoder@$(whoami).service"` で90秒以上
受信を止めて確認する(**その間は受信が止まる**ので通常運用中は行わない)。
確認後は `sudo systemctl start "qzss-decoder@$(whoami).service"` で戻し、
「受信回復」の通知が届くことを確認する。`journalctl`が読めない等の判定不能時は
復旧動作を起こさない(何も実行されない)ことも`journalctl -u qzss-reception-watch`で確認できる。

## 6.8 CPUガバナーとloopback待ち受け

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # ondemand(performanceではない)
systemctl status qzss-cpu-governor.service
systemctl status qzss-cpu-performance.service              # 「could not be found」なら旧ユニット削除済み
ss -ltnp | grep ':8080'                                     # 127.0.0.1:8080 だけ(0.0.0.0/[::]ではない)
curl -fsS -m 5 http://localhost:8080/ -o /dev/null && echo local-ok
```

同じLAN上の別端末から `curl -m 5 http://<ラズパイのIP>:8080/` が**接続拒否/タイムアウト**
になること。キオスク表示(localhost)は従来どおり表示されること。

## 7. ハードウェアウォッチドッグの確認

```bash
wdctl
```

`Firmware Timeout` 等が表示されれば有効化されている(`install_services.sh`
実行後に`sudo reboot`していないと無効のまま)。実際にOSごとフリーズさせて
試すのは危険なので必須ではないが、以下で「systemdが定期的にウォッチ
ドッグへ合図を送っていること」だけは確認できる:

```bash
systemctl show -p WatchdogDevice,RuntimeWatchdogUSec
```

`RuntimeWatchdogUSec=14000000` (14秒)になっていればOK。

## 8. (任意)温度閾値超過時のDiscord通知

意図的に高温状態を作るのは実機に負荷がかかるため必須ではないが、可能で
あれば `TEMP_WARN` / `TEMP_CRITICAL`(`qzss.env`)を一時的に低い値に
下げて次回実行させ、Discordへ警告が飛ぶことだけ確認してから元の値に
戻す、という形でも代用できる。

## 全部確認できたら

このファイルの各項目にチェックが付いた時点で、「残っている課題」リスト
の「report_status.sh の実機エンドツーエンド動作確認」は完了とみなして
良い。
