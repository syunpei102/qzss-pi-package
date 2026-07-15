# Discord通知の設定方法

ラズパイで異常(OTA更新失敗・温度異常上昇など)が起きた時に、Discordの
指定チャンネルへ自動でメッセージを送る仕組み。Webhook(ボット不要)を
使うので設定は数分で終わる。

## 1. Discord側でWebhookを作る

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

## 通知される内容

| タイミング | 内容 |
|---|---|
| OTA更新後にアプリが応答しない | ロールバックを試みる旨 |
| ロールバックで復旧した | 原因調査を促すメッセージ |
| ロールバックしても復旧しない | **最重要。実機の確認が必要** |
| 本体温度が危険域(既定80℃以上) | 温度と閾値 |
| アプリのサービスが停止していて自動再起動した/できなかった | 対象サービス名 |
| 管理サイトからの指示で再起動する | その旨 |

「更新なし」「正常に更新できた」など、問題が無い時は通知しない
(鳴らしすぎると見逃されるようになるため)。ログは常に
`~/qzss/qzss-pi-package/update_state/update_check.log` と
`~/qzss/qzss-pi-package/update_state/report_status.log` に残る。

### 温度の閾値を変える

既定では 70℃ で記録のみ(警告)、80℃ で Discord 通知(危険)になる。
変更したい場合は `qzss.env` に以下を追記する:

```
TEMP_WARN=70
TEMP_CRITICAL=80
```

## トラブルシューティング

- **通知が届かない**: `qzss.env` のURLに余分な空白・改行が入っていないか確認。`curl`での手動テスト(上記)がまず通るか確認する
- **Webhookを間違えて公開してしまった**: Discord側のWebhook設定画面から
  そのWebhookを削除し、新しく作り直す(URLは再発行される)
