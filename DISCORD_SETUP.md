# Discord通知・操作の設定方法

Discordを通じて、(1)ラズパイの異常通知(OTA更新失敗・温度異常上昇など)
と、(2)スラッシュコマンドによる操作(再起動・更新確認・拠点の地域設定・
拠点用トークンの発行)の両方を行う。前者はWebhook(ボット不要)、後者は
Discord Bot(コマンドを受け取れるアプリ)が必要になる — 別々の仕組みなので
両方設定する。

## 1. Discord側でWebhookを作る(通知用)

1. 通知を受け取りたいDiscordサーバーの、通知用チャンネルを開く
2. チャンネル名の右の歯車アイコン(チャンネルの編集)→「連携サービス」
3. 「ウェブフック」→「新しいウェブフック」
4. 名前を分かりやすく変更(例: 「QZSS監視」)
5. 「ウェブフックURLをコピー」を押す

これで `https://discord.com/api/webhooks/xxxxxxxxx/yyyyyyyyy` のような
URLがコピーされる。**このURLは秘密情報**(知っている人は誰でもそのチャンネルに投稿できてしまう)なので、公開のgitリポジトリ等には絶対に貼らないこと。

## 2. ラズパイ側に設定する

```bash
cd ~/qzss/qzss-pi-package
cp qzss.env.example qzss.env   # まだ作っていない場合のみ
nano qzss.env
```

`DISCORD_WEBHOOK_URL=` の行の末尾に、コピーしたURLをそのまま貼り付けて保存する。

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxxxxxx/yyyyyyyyy
```

空欄のままなら通知機能は無効(何も送られない)。既にサービスを起動済みの場合は反映のため再起動する:

```bash
sudo systemctl restart qzss-decoder@$(whoami)
```

## 3. 動作確認

手動でテスト通知を送ってみる:

```bash
cd ~/qzss/qzss-pi-package
source qzss.env
curl -H "Content-Type: application/json" -d '{"content": "🧪 テスト通知です"}' "$DISCORD_WEBHOOK_URL"
```

指定したDiscordチャンネルにメッセージが届けば設定完了。

## 通知される内容(Webhook)

| タイミング | 内容 |
|---|---|
| OTA更新の成功 | 更新前→更新後のコミット |
| OTA更新後にアプリが応答しない | ロールバックを試みる旨 |
| ロールバックで復旧した | 原因調査を促すメッセージ |
| ロールバックしても復旧しない | **最重要。実機の確認が必要** |
| 本体温度が危険域(既定80℃以上) | 温度と閾値 |
| アプリのサービスが停止していて自動再起動した/できなかった | 対象サービス名 |
| 再起動を予約した/実際に完了した | それぞれのタイミングで通知 |
| クラウド(Cloud Run)がオフライン/オンラインに切り替わった | `check_urgent.sh`が15分おきに外形監視(オンライン↔オフラインの切り替わり時のみ通知) |

「更新なし」のように何も起きなかった場合のみ通知しない(何かしら状態が
変わった時は通知する方針)。ログは常に
`~/qzss/qzss-pi-package/update_state/update_check.log` と
`~/qzss/qzss-pi-package/update_state/report_status.log` に残る。

### 温度の閾値を変える

既定では 70℃ で記録のみ(警告)、80℃ で Discord 通知(危険)になる。
変更したい場合は `qzss.env` に以下を追記する:

```
TEMP_WARN=70
TEMP_CRITICAL=80
```

## 4. Discord Botを作る(スラッシュコマンド操作用)

Web管理画面(`/device-admin`)の代わりに、Discordから直接
`/reboot`・`/update_check`・`/set_region`・`/create_device_token`・
`/set_training_broadcasts`・`/delete_device`を実行できるようにする。
BotはDiscordから常時接続を保つタイプではなく、コマンド実行のたびに
Cloud Run側のURLへ通知が飛んでくる方式(HTTP Interactions)なので、
ラズパイ側の設定は不要。すべてCloud Run(`qzss-map`)側の設定になる。

1. https://discord.com/developers/applications で「New Application」
   から新規アプリケーションを作成する(名前は自由)
2. 左メニュー「General Information」ページの **PUBLIC KEY** を控える
   (Cloud Runの環境変数 `DISCORD_PUBLIC_KEY` に使う)
3. 同じページの **APPLICATION ID** を控える(コマンド登録スクリプトに使う)
4. 左メニュー「Bot」タブでBotを追加し、**Token** を控える(表示は
   1回だけ。コマンド登録スクリプトにのみ使う、実行中のサーバー自体は
   不要)
5. 左メニュー「OAuth2」→「URL Generator」で、SCOPESに
   `applications.commands` だけチェックを入れ、生成されたURLを
   ブラウザで開いて自分のDiscordサーバーに追加する(通常のBot招待とは
   別。メッセージ送受信の権限は不要)
6. 使いたいDiscordサーバーの**サーバーID**を控える(サーバー名を右クリック
   →「IDをコピー」。表示されない場合は設定→詳細設定で開発者モードを
   オンにする)
7. `map`リポジトリで、控えた値を使ってコマンドを登録する(1回だけ、
   コマンドの内容を変えた時だけ再実行する):
   ```bash
   cd map
   DISCORD_BOT_TOKEN=xxxx \
   DISCORD_APPLICATION_ID=xxxx \
   DISCORD_GUILD_ID=xxxx \
   node register_discord_commands.js
   ```
8. Cloud Runの環境変数に `DISCORD_PUBLIC_KEY`(必須)・
   `DISCORD_ADMIN_USER_ID`(自分のDiscordユーザーID、任意だが推奨。
   設定するとこのユーザー以外のコマンド実行を拒否する)を設定してデプロイする
9. Botの「General Information」ページの **INTERACTIONS ENDPOINT URL**
   欄に `https://eq.shum10.com/discord/interactions` を設定する
   (8のデプロイが完了していないと、Discordが送ってくる確認リクエストに
   正しく応答できず保存に失敗する)

設定後、Discordのチャンネルで各コマンドを入力すると、`device`(拠点ID)・
`prefecture`(都道府県名、`set_region`のみ)を入力補完付きで選べる。
実行結果は自分にだけ見えるメッセージで返る。

`/set_training_broadcasts enabled:<true/false>` は、月2回程度配信される
公式の訓練/試験放送を地図に表示するかどうかを切り替える(表示する場合、
本物の警報と全く同じズーム・塗りつぶしだが、バッジ・タイトルに「[訓練]」
が付く)。`device:<拠点ID>`を付ければその拠点(の表示ブラウザ、
`?device=`で開いている画面)だけに適用され、省略すれば拠点ごとの上書きが
無いすべての画面(一般公開ビュー含む)に一斉適用される。

`/create_device_token device:<拠点ID>` は、その拠点専用の`INGEST_TOKEN`
を新規発行する(新しいラズパイの登録手順は
[NEW_DEVICE_ONBOARDING.md](./NEW_DEVICE_ONBOARDING.md)参照)。拠点ごとに
別々のトークンなので、1台分が漏えいしても他の拠点は無事(そのトークンを
再発行するだけで良い)。既存の拠点IDに対して実行すると再発行になり、
旧トークンはその場で失効する。

`/delete_device device:<拠点ID>` は、その拠点の記録(状態報告履歴・地域
設定・発行済みトークン・訓練放送の個別設定)を全て削除する。トークンは
即座に失効するので、廃止したラズパイからそれ以降データが送られてきても
拒否される。元に戻せないので実行前に拠点IDをよく確認すること。

## トラブルシューティング

- **通知が届かない**: `qzss.env` のURLに余分な空白・改行が入っていないか確認。`curl`での手動テスト(上記)がまず通るか確認する
- **Webhookを間違えて公開してしまった**: Discord側のWebhook設定画面から
  そのWebhookを削除し、新しく作り直す(URLは再発行される)
- **スラッシュコマンドが候補に出てこない**: `register_discord_commands.js`
  を実行し忘れていないか確認する(guild限定登録は通常すぐ反映されるが、
  反映まで数分かかることもある)
- **コマンドを実行すると「このアプリケーションは応答していません」と出る**:
  Cloud Run側で `DISCORD_PUBLIC_KEY` が正しく設定されているか、
  `INTERACTIONS ENDPOINT URL` が `https://eq.shum10.com/discord/interactions`
  になっているか確認する
