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
export QZSS_INGEST_TOKEN="4552855f00070aecee0278b9ba8dbc7c"
./start_pi.sh <シリアルポート> 115200
```

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
