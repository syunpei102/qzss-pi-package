# デバイス管理ダッシュボード 実機動作確認チェックリスト

`report_status.sh`・デバイス管理ダッシュボード(`/device-admin`)は、これ
まで構文チェックとローカルcurlでの疎通確認のみで、物理ラズパイでの
エンドツーエンド動作確認がまだ済んでいない。実機投入時に以下を上から
順に確認する。

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

## 3. デバイス管理ダッシュボードに反映されるか

`https://eq.shum10.com/device-admin` を開いてログインし、数分以内に
そのデバイスのカードが表示されることを確認する。以下の値が実機の値と
一致しているか:

- 本体温度(`vcgencmd measure_temp` の値と近いか)
- 稼働時間
- ディスク空き
- `qzss-map` / `qzss-pi-package` のgitコミット(直近pull後のコミットに
  なっているか)
- オンライン状態(🟢オンライン表示になっているか)

## 4. 「再起動を予約」の往復確認

1. ダッシュボードでそのデバイスの「再起動を予約」を押す
2. 実機側で次回の `qzss-report-status.service` 実行(タイマー待ち、また
   は手動で `sudo systemctl start qzss-report-status.service`)を待つ
3. `journalctl -u qzss-report-status.service -n 50` に再起動コマンドを
   受け取ったログが出ているか
4. 実際に実機が再起動されるか(`uptime` がリセットされるか)
5. 再起動後、`qzss-map.service` / `qzss-decoder.service` が自動的に
   立ち上がっているか(`systemctl status`)

## 5. 「更新確認を予約」の往復確認

1. ダッシュボードで「更新確認を予約」を押す
2. 次回の状態報告時に `force_update_check` が実行され、GitHubの新着
   コミットがあれば通常の `update_check.sh` と同様に取得・依存関係更新・
   サービス再起動まで走ることを確認する

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

## 7. (任意)温度閾値超過時のDiscord通知

意図的に高温状態を作るのは実機に負荷がかかるため必須ではないが、可能で
あれば `TEMP_WARN` / `TEMP_CRITICAL`(`qzss.env`)を一時的に低い値に
下げて次回実行させ、Discordへ警告が飛ぶことだけ確認してから元の値に
戻す、という形でも代用できる。

## 全部確認できたら

このファイルの各項目にチェックが付いた時点で、「残っている課題」リスト
の「report_status.sh の実機エンドツーエンド動作確認」は完了とみなして
良い。
