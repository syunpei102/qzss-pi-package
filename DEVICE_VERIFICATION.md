# デバイス管理(Discord操作) 実機動作確認チェックリスト

`report_status.sh`・Discordスラッシュコマンドからの操作は、これまで
構文チェックとローカルcurlでの疎通確認のみで、物理ラズパイでの
エンドツーエンド動作確認がまだ済んでいない。実機投入時に以下を上から
順に確認する。(Web管理画面`/device-admin`は本番では無効化済みのため、
このチェックリストはDiscord経由の操作を確認する内容になっている)

## 0. 前提

```
cd ~/qzss/qzss-pi-package
git pull
./install_services.sh
```

`install_services.sh` はsystemdユニットの再インストール・有効化を行う。
実行後、対象ユニットが有効化されているか確認する:

```
systemctl is-enabled qzss-report-status.timer
systemctl is-enabled qzss-map.service qzss-decoder.service
```

## 1. `qzss-report-status.timer` が動いているか

```
systemctl status qzss-report-status.timer
systemctl list-timers qzss-report-status.timer
```

`Active: active (waiting)` になっていること。`OnBootSec=2min` なので、
起動後2分以内に初回実行されるはず。

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
5. 再起動後、`qzss-map.service` / `qzss-decoder.service` が自動的に
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
sudo systemctl stop qzss-map.service
```

の状態でしばらく待ち(次回の `report_status.sh` 実行まで)、以下を確認:

- `report_status.sh` が停止を検知し `sudo systemctl restart qzss-map.service`
  を試みているか(ログに残る)
- 復旧に失敗した場合のみDiscordへ通知が飛ぶか(`DISCORD_WEBHOOK_URL` を
  設定している場合)
- 復旧に成功した場合はDiscord通知が飛ばない(正常系なので静かなままで
  良い)ことも合わせて確認

## 6.5 OTA以外のタイミングでの自動ロールバックの確認

`report_status.sh`は、OTA更新の直後だけでなく**毎回の実行時にqzss-mapへ
実際にHTTP応答があるか**を確認する。応答が無ければ再起動→それでも
直らなければ最後に動作確認できた安定版(`update_state/qzss-map.last_good`)
へ自動的に切り替える、という保険が入っている。以下で確認する:

1. `cat update_state/qzss-map.last_good` で安定版のコミットが記録されて
   いることを確認(通常運用で一度でも正常に動いていれば記録される)
2. 意図的に`qzss-map`のコードを壊す(例: `~/qzss/qzss-map`で
   `git commit --allow-empty -m test`してから、動かないコードに書き換えて
   コミット、またはpackage.jsonを壊す等)
3. `sudo systemctl restart qzss-map@$(whoami)`で反映させ、次回の
   `report_status.sh`実行(または手動実行)を待つ
4. ログ(`update_state/report_status.log`)に「HTTP応答しません」
   →「再起動を試みます」→(直らなければ)「安定版へ切り替えます」の
   流れが記録され、実際に`git log`のHEADが安定版のコミットに戻っている
   ことを確認
5. 復旧後、Discordに「🚨 ...安定版へ自動的に切り替えて復旧しました」の
   通知が届くことを確認
6. 確認後、壊したコミットは削除するか`git reset --hard`で元に戻しておく

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
