# 新しいラズパイが届いた時の登録手順

実機のセットアップ自体(OSインストール・依存パッケージ等)の詳細は
[SETUP.md](./SETUP.md) を参照。このファイルは「箱から出してから、
Discordで操作できる状態になるまで」を1本の流れでまとめた簡易版。

## 1. この拠点の「拠点ID」を決める

Discordコマンド(`/reboot device:...`)や地図の`?device=...`で使う識別子。
決めたら控えておく(例: `pi-shibuya-01`)。

## 2. Discordで、この拠点専用のトークンを発行する

```
/create_device_token device:pi-shibuya-01
```

実行すると、自分にだけ見えるメッセージでトークンが発行される
(既に同じ拠点IDでトークンを発行済みの場合は再発行になり、旧トークンは
その場で失効する)。**拠点ごとに別々のトークンなので、この端末分が
万一漏えいしても他の拠点には影響しない。** 表示された
`QZSS_DEVICE_ID`/`QZSS_INGEST_TOKEN`の2行を控えておく(手順4で使う)。

## 3. OS〜サービス起動までのセットアップ

[SETUP.md](./SETUP.md) の 1〜4節の通り実施(OSインストール→初回セット
アップ→リポジトリ取得→受信機のポート確認)。

## 4. `qzss.env` に拠点固有の値を設定する

```bash
cd ~/qzss/qzss-pi-package
cp qzss.env.example qzss.env
nano qzss.env
```

最低限、以下を埋める(`QZSS_DEVICE_ID`/`QZSS_INGEST_TOKEN`は手順2で
発行されたものをそのまま貼り付ける):

```
QZSS_SERIAL_PORT=/dev/ttyUSB0        # 手順3で確認したポート
QZSS_CLOUD_URL=https://eq.shum10.com/ingest
QZSS_INGEST_TOKEN=<手順2で発行されたトークン>
QZSS_DEVICE_ID=pi-shibuya-01          # 手順2で指定した拠点ID
DISCORD_WEBHOOK_URL=<温度異常等の通知用。DISCORD_SETUP.md参照>
```

公開リポジトリのため、このファイル(`qzss.env`)以外にトークンの実際の
値を書かないこと(`qzss.env`自体は`.gitignore`対象)。

## 5. サービス化する(OTA更新・状態報告・自動復旧をまとめて有効化)

```bash
cd ~/qzss/qzss-pi-package
./install_services.sh
sudo reboot   # ハードウェアウォッチドッグの有効化に必要(初回のみ)
```

`install_services.sh`はハードウェアウォッチドッグ(OSごとフリーズした
場合に自動で電源を再投入する仕組み)の設定も行うが、有効化には再起動が
必要なので、初回セットアップの最後に必ず`sudo reboot`しておく
(2回目以降の`install_services.sh`実行では既に設定済みなら再起動不要)。

これで以下が自動的に動き出す:
- 受信・デコード・送信(`qzss-decoder.service`)
- 地図アプリ(ローカルkiosk版を使う場合。`qzss-map.service`)
- 毎晩のOTA更新チェック(`qzss-update-check.timer`)・緊急更新チェック
  (`qzss-urgent-check.timer`)
- 1時間おきの状態報告(`qzss-report-status.timer`) — これが
  Cloud Run側にこの拠点を認識させる第一歩

## 6. 拠点がCloud Run側に認識されるのを待つ(最大1時間+α)

`qzss-report-status.timer`は`OnBootSec=2min`なので、起動後2分以内に
初回の状態報告が飛ぶ。以下で確認できる:

```bash
systemctl status qzss-report-status.timer
journalctl -u qzss-report-status.service -n 30 --no-pager
```

一度でも状態報告が成功すれば、Discordの `/reboot`・`/update_check`・
`/set_region` の `device` オートコンプリート候補にこの拠点IDが
出るようになる(`deviceStatus`に登録された時点で候補に入るため)。

## 7. Discordから対象地域を設定する(必要な場合のみ)

その拠点でその地域(+周辺)だけを表示・送信させたい場合(サイネージ
設置など)、Discordで:

```
/set_region device:pi-shibuya-01 prefecture:東京都
```

未指定のままなら全国対象・絞り込みなしの通常表示になる。

## 8. (サイネージ設置の場合)kiosk表示用URLを設定する

その拠点専用の表示URLとして `https://eq.shum10.com/?device=pi-shibuya-01`
をChromiumのkiosk起動先に設定する(手順は[SETUP.md](./SETUP.md) 7節、
`start_pi_local.sh`を使う場合は自動でローカルの地図を表示する設定に
なっているので、ローカル版ではなくWeb版URLをkiosk表示させたい場合のみ
この手順が必要)。

## 9. 動作確認

一通り届いた段階で以下を確認しておくと安心:

- `/reboot device:pi-shibuya-01` → 次回の状態報告で実際に再起動され、
  完了後にDiscordへ「✅ 再起動が完了しました」が届くか
- `/update_check device:pi-shibuya-01` → 新しいコミットがあれば更新
  され、Discordへ結果が届くか
- 本体温度が閾値を超えた場合にDiscordへ通知が届くか(意図的に高温を
  作る必要はなく、`qzss.env`の`TEMP_WARN`/`TEMP_CRITICAL`を一時的に
  下げて確認してもよい)

より詳しいエンドツーエンド確認項目は
[DEVICE_VERIFICATION.md](./DEVICE_VERIFICATION.md) を参照。
