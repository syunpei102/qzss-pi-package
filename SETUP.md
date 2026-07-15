# ラズパイ セットアップ手順

QZSS災危通報 統合防災地図を、Raspberry Pi 3 Model B+ で動かすための手順。
2つのモードがある。

- **Web版**: ラズパイは受信・デコードだけ行い、Cloud Run(`eq.shum10.com`)へ送信する。地図はどの端末のブラウザからでも見られる。
- **ローカルkiosk版**: インターネット接続なしで、ラズパイ自身が地図アプリを起動し、直結したディスプレイにChromiumのkiosk表示(全画面)で映す。両方とも同じリポジトリ・同じコードを使う(送信先URLが違うだけ)。

## 0. 用意するもの

- Raspberry Pi 3 Model B+ 本体・ケース・電源・ファン
- MicroSDカード(32GB以上推奨。今回は64GBを用意済み)
- QZSS受信機(アンテナ + シリアル変換ケーブル)
- ローカルkiosk版を使う場合: HDMIディスプレイ

## 1. OSインストール

[Raspberry Pi Imager](https://www.raspberrypi.com/software/) で以下を書き込む。

- OS: **Raspberry Pi OS (64-bit) with desktop**(Lite不可。Chromiumのkiosk表示に必要)
- 詳細設定(歯車アイコン)で先に設定しておくと楽:
  - ホスト名
  - SSHを有効化
  - ユーザー名・パスワード
  - Wi-Fi(ローカルkiosk版だけで使うなら不要だが、セットアップ作業用にあると便利)

## 2. 初回セットアップ(SSHログイン後)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y nodejs npm python3-venv python3-pip git chromium-browser

# GPUメモリを増やす(WebGL描画がソフトウェアフォールバックして
# 著しく重くなるのを防ぐ)
sudo raspi-config
# → Performance Options → GPU Memory → 128 以上を入力 → 再起動
```

`nodejs`がaptで古いバージョンしか入らない場合(`node -v` で v18未満など)は
[NodeSource](https://github.com/nodesource/distributions)のセットアップスクリプトを使う:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

## 3. リポジトリを取得

2つのリポジトリは公開設定にしてあるので、認証なしでcloneできる。

```bash
mkdir -p ~/qzss && cd ~/qzss
git clone https://github.com/syunpei102/qzss-map.git
git clone https://github.com/syunpei102/qzss-pi-package.git
```

`qzss-pi-package`側でPython仮想環境を作る(初回のみ・起動スクリプトが自動で作るので手動でなくてもよい):

```bash
cd ~/qzss/qzss-pi-package
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 4. 受信機を接続してポート名を確認

```bash
ls /dev/ttyUSB* /dev/ttyACM*
```

出てきたパス(例: `/dev/ttyUSB0`)を、以降のコマンドの`<シリアルポート>`部分に使う。
ボーレートは受信機の仕様によるが、通常は`115200`。

## 5. 起動する

### Web版(Cloud Runへ送信)

```bash
cd ~/qzss/qzss-pi-package
export QZSS_CLOUD_URL="https://eq.shum10.com/ingest"
export QZSS_INGEST_TOKEN="<Cloud Run側のINGEST_TOKENと同じ値。デプロイ担当者に確認する>"
./start_pi.sh <シリアルポート> 115200
```

秘密のトークンなので、このファイルや`qzss.env`以外の場所(コミットする設定ファイル等)には書かないこと。

地図は `https://eq.shum10.com` を開けばどの端末からでも見られる。

### ローカルkiosk版(完全オフライン)

```bash
cd ~/qzss/qzss-pi-package
./start_pi_local.sh <シリアルポート> 115200
```

自動で以下が起動する:

1. `~/qzss/qzss-map` 内の地図アプリ(`node server.js`、ポート8080)
2. Chromiumのkioskモード(全画面、`http://localhost:8080` を表示)
3. 受信機からの取り込み(ローカルの地図アプリへ送信、インターネット不要)

終了する場合は `Ctrl+C`(地図アプリ・Chromiumも一緒に終了する)。

## 6. 起動確認のポイント

- `./start_pi_local.sh` 実行後、地図アプリの起動待ちで固まって見える場合は
  `/tmp/qzss_map_local.log` を確認する
- Chromiumが真っ黒/真っ白のままの場合、GPUメモリ設定(手順2)を見直す
- 受信機からデータが来ているかは、ターミナルに流れるログ
  (`📡 ingest受信: ...` 等)で確認できる
- テストデータで動作確認したい場合、`read_legacy_dual.py`起動中のターミナルで
  何も入力せずEnter → 重要情報のテスト送信/取消が交互に送られる
  (`c` + Enter で気象警報のテストも送れる)

## 7. 電源投入で自動起動させたい場合(本番展示用)

サイネージとして使う場合、SSHでログインせずに電源投入だけで
ローカルkiosk版が立ち上がるようにしたい。その場合は
`~/.config/autostart/` に `.desktop` ファイルを置くか、
`raspi-config` で自動ログインを有効にした上で `.bashrc` の末尾に
`start_pi_local.sh` の呼び出しを追記する方法がある。
展示直前に実機で動作確認できてから設定するのが安全。

## 8. リモート更新(OTA)を有効にする

重大なバグが見つかった時、ラズパイの実機を操作しなくても最新版を
反映できるようにする仕組み。組み込み機器と同じ「pull型」(ラズパイ側が
定期的にGitHubへ確認しに行く)にしているため、ラズパイ側でポートを
開けたりSSHを外部公開したりする必要は一切ない。

- **通常**: 毎晩3時ごろに1回、GitHub(qzss-map / qzss-pi-package)に
  新しいコミットが無いか確認し、あれば自動で取得・依存関係更新・
  サービス再起動まで行う
- **緊急時**: `qzss_pi_package/URGENT_UPDATE` というファイルの中身を
  書き換えてcommit・pushすると、最短15分以内にラズパイが気づいて
  夜を待たずにすぐ更新する
- **安全装置**: 更新後にアプリが正常に応答しなければ、自動的に直前の
  コミットへロールバックする(壊れた更新を適用したまま放置しない)

### セットアップ(初回のみ、実機で)

```bash
cd ~/qzss/qzss-pi-package
cp qzss.env.example qzss.env
nano qzss.env   # シリアルポート・送信先URL等を記入
./install_services.sh
```

これでサービス化(自動起動・クラッシュ時の自動再起動)と、
OTA更新の定期実行(毎晩 + 緊急時15分おきチェック)が有効になる。

### 緊急時の更新手順(開発側)

```bash
# qzss-pi-package リポジトリ内で
echo "最終更新: $(date '+%Y-%m-%d %H:%M') <修正内容の概要>" >> URGENT_UPDATE
git add URGENT_UPDATE
git commit -m "緊急更新の合図"
git push
```

最短15分以内にラズパイ側が気づいて更新される。
`update_state/update_check.log` に更新履歴・ロールバック履歴が残る。

### 状態確認

```bash
systemctl status qzss-map@$(whoami)
systemctl status qzss-decoder@$(whoami)
systemctl list-timers | grep qzss
tail -f ~/qzss/qzss-pi-package/update_state/update_check.log
```

## 9. デバイス管理ダッシュボードと温度監視

`./install_services.sh` を実行すると、上記のOTA更新に加えて
`qzss-report-status.timer` も有効になる。これは1時間おきに
`report_status.sh` を実行し、以下を行う:

- 本体温度・稼働時間・ディスク空き容量・現在のgitコミットをCloud Run
  (`qzss-map`)へ報告する
- 温度が閾値(既定: 警告70℃・危険80℃。`qzss.env` の
  `TEMP_WARN` / `TEMP_CRITICAL` で変更可)を超えたらDiscordへ通知する
- Discordのスラッシュコマンドで予約された「再起動」「更新確認」があれば
  その場で実行する(ラズパイ側は着信を受け付けない設計のため、
  この定期報告のついでにコマンドを受け取る)。実行結果(再起動が
  実際に完了したか等)もDiscordへ通知する
- `qzss-map` / `qzss-decoder` サービスが何らかの理由で停止していたら
  自動的に再起動を試みる(保険。通常はsystemdの
  `Restart=on-failure` + `StartLimitIntervalSec=0` により、
  クラッシュを繰り返しても上限なく自動復帰する)

### Discordからの操作(再起動・更新確認・拠点の地域設定)

以前はWeb管理画面(`/device-admin`)から操作していたが、現在はDiscordの
スラッシュコマンドに一本化されている。`/reboot device:<拠点ID>`・
`/update_check device:<拠点ID>`・`/set_region device:<拠点ID>
prefecture:<都道府県名>` が使える。Bot自体のセットアップ手順は
[DISCORD_SETUP.md](./DISCORD_SETUP.md) の「4. Discord Botを作る」を参照。

拠点に「対象地域」(都道府県)を割り当てると、その拠点向けの地図表示
(`https://eq.shum10.com/?device=<拠点ID>` のようにURLに`?device=`
パラメータを付けて開いた画面)が、その都道府県+周辺地方(例: 東京→関東)
だけにズーム固定され、対象地域外の通報はラズパイ側でもそれ以上処理
されなくなる(サイネージ設置先ごとに地元の情報だけを表示したい場合に
使う。`?device=`を付けずに開いた一般公開ビューは今まで通り全国対象の
まま)。`?device=`・Discordコマンドの`device`に指定する拠点IDは、
ラズパイの`qzss.env`の`QZSS_DEVICE_ID`(未設定ならhostname)と一致させる。

Web管理画面(`/device-admin`)のコードは残っているが、Cloud Run本番では
既定で無効(`qzss-map`リポジトリの`server.js`、環境変数
`ENABLE_WEB_ADMIN`が未設定だと該当ルート自体が登録されない)。

### 応用: ハードウェアウォッチドッグ(任意・上級者向け)

OSやカーネルごとフリーズしてしまうような重大な障害(ソフトウェア側の
自動再起動では復旧できないケース)まで自動復旧させたい場合は、
Raspberry Pi OS標準のハードウェアウォッチドッグを有効にできる:

```bash
# /boot/firmware/config.txt (または /boot/config.txt) に追記
dtparam=watchdog=on
sudo reboot
```

有効化後、`qzss-map.service` に `WatchdogSec=30` と `Type=notify`
相当の生存通知が必要になるため、アプリ側の対応が別途必要。
現状は導入していない(検証環境のない変更を安全のため見送っている)。
必要になった場合は相談してほしい。
