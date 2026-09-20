# qzss-pi-package

Raspberry Pi 側の受信機デコーダ，監視，OTA更新，リモートコマンド受信のパッケージ．
セットアップは `SETUP.md`，実機確認は `DEVICE_VERIFICATION.md` を参照．

## リモートコマンド(reboot / force_update_check)の受信と二重実行防止

管理サイト(親リポジトリの `device-commands.js`)は，`report_status.sh` のstatus報告への応答で
`commands: [{id, command, requestedAt}]` を返す．端末は次のとおり処理する．

1. status報告に `"supports_ack": true` と，永続化済みの `"acked_command_ids": [...]` を付けて送る．
   サーバーはackされたコマンドの再配信を止める(未ackなら期限24時間・最大5回まで再配信)．
2. 応答の `{id, command}` は，実行の前に **durable inbox** (`update_state/command_inbox.json`)へ
   一時ファイル+fsync+renameで原子的に保存し，同時にそのidをackリストへ加える(実行前ack)．
   idは英数字・`-`・`_` の64文字以内，commandは許可リスト(`reboot`, `force_update_check`)のみ受け付け，
   シェルへは検証後の値だけを渡す(単語分割によるコマンド注入はない)．
3. **processed ledger** として各コマンドの状態を `received → executing → done / failed / interrupted`
   と記録する．同じidが再配信されても実行しない．ledgerは最大500件・7日間保持する．
4. 冪等な `force_update_check` は，実行中に電源断等で中断されても次回の報告で再実行する．
5. 非冪等な `reboot` は，`executing` を永続化してから実行する．

### クラッシュ時のトレードオフ

| クラッシュのタイミング | 結果 |
| --- | --- |
| inbox保存前 | 未ackなのでサーバーが再配信する(失われない) |
| inbox保存後・`executing` 記録前 | 次回の報告で実行される(at-least-once，失われない) |
| `executing` 記録後・再起動要求の前 | **そのrebootは実行されない**．次回 `interrupted` としてDiscordへ通知する．必要なら再度予約する |
| 再起動要求の後 | 再起動される．ledgerは `executing` のままだが二重には実行されない |

つまり `reboot` は **二重再起動を決して起こさない(at-most-once)** ことを優先し，
`executing` 記録直後の極めて短い窓でのクラッシュでは再起動が失われうる．
`systemctl reboot` が失敗した場合は成功通知や `reboot_requested` マーカーを残さず，失敗をDiscordへ通知する
(この場合もackは済んでいるため自動再試行せず，管理者が再予約する)．
旧サーバー(idなし)からのコマンドは1回だけ実行し，ackはできない．

status報告は5分おき(`qzss-report-status.timer`)なので，コマンドの最大遅延は約5分．

## OTA更新

* `update_check.sh` は `update_state/update.lock` への `flock` で排他される．他が実行中なら終了コード75で終わる
  (`check_urgent.sh` は失敗扱いにせず次回再確認，`report_status.sh` は死活確認・自動ロールバックをスキップする)．
* 緊急更新の合図(`URGENT_UPDATE`)は，更新が **成功した後だけ** `urgent_seen` に記録する．
  失敗時は15分→30分→…最大6時間のバックオフで再試行する．
* リポジトリの `systemd/` の変更は，OTA中に root 所有のヘルパー `/usr/local/sbin/qzss-unit-helper`
  (`qzss_unit_helper.sh`)経由で反映する．全ユニットを検証してから1ファイルずつ原子的に差し替え，
  `systemctl daemon-reload` の後に変更ユニットをenableする．ロールバック時はユニットも元へ戻す．
  `qzss-*.service` / `qzss-*.timer` 以外は受け付けず，配置先は `/etc/systemd/system` 固定．
  sudoersに許可するのはこのヘルパーと `systemctl daemon-reload` だけ(汎用のcp等は許可しない)．
  **既存の端末では，今回の更新後に一度 `./install_services.sh` を再実行してヘルパーとsudoersを導入する必要がある．**
  導入前は，OTAはユニットを反映せず警告ログを出すだけで，更新自体は失敗扱いにしない．
* ヘルパーは，リポジトリ内のユニット内容(`ExecStart` 等)を root で配置するため，リポジトリへ書き込める人は
  実質的に端末上でコードを実行できる．これは従来のOTA(リポジトリのコードをそのまま実行)と同じ信頼範囲である．

## 受信・送信

* 送信キューは有界(クラウド200件，ローカル100件)・優先度付き(緊急 > その他 > ハートビート)．
  再試行は待ち時間(3秒から指数的に最大60秒)付きでキューへ戻し，ワーカーはsleepしない．
  そのため再試行待ちの通報が，新しく届いた緊急通報を待たせない．満杯時は低優先・古いものから捨てる．
  認証エラー等の恒久的な4xxは再試行しない．
* `/config` の取得は訓練放送表示とLアラート解析で共通の1回にした(従来は2分ごとに二重取得)．
* UBXのペイロード長はリトルエンディアン2バイトで読み，上限(1024)を超える長さやチェックサム不一致は
  1バイト読み捨てて次の同期ヘッダから再同期する．シリアルは受信済みの分をまとめて読む．
* NMEAチェックサムは大文字・小文字どちらの16進も受け付ける．未知のPRNは読み捨てる．

## 監視・省リソース

* `qzss-reception-watch.timer`(30秒おき)で `reception_watch.py` を実行する．外部コマンド
  (`journalctl`，`systemctl`，USBリセット)の失敗は成功扱いせず，失敗時はクールダウンを残さない．
* クラウド死活監視は2分おきで，状態が変わったときだけログ・状態ファイルへ書く．
  各ログは `lib_log.sh` の `rotate_log` で512KB(死活監視は256KB)を超えたら1世代だけ退避する．
* CPUガバナーは常時performanceをやめ，`qzss-cpu-governor.service` で `ondemand`
  (`qzss.env` の `QZSS_CPU_GOVERNOR` で変更可)にする．
* `qzss-map.service` は `HOST=127.0.0.1` でloopbackのみ待ち受ける．

## テスト

```
python -m unittest discover -p 'test_*.py' -v
python test_is_in_scope.py
python test_reception_watch.py
```
