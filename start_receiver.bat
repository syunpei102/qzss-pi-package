@echo off
REM 実機の受信機からクラウド(Cloud Run)へ送信する一発起動スクリプト(Windows用)。
REM 地図側は critical/caution 統合済みのため送信先は1本(QZSS_CLOUD_URL)のみ。
REM 秘密のトークンはこのファイルに直接書かず、実行前に環境変数で設定すること
REM (公開リポジトリのため)。
REM
REM 使い方:
REM   set QZSS_INGEST_TOKEN=xxxxxxxx
REM   start_receiver.bat COM3 [ボーレート(既定9600)]

cd /d "%~dp0"

set PORT=%1
set BAUDRATE=%2
if "%BAUDRATE%"=="" set BAUDRATE=9600

if "%PORT%"=="" (
  echo 使い方: start_receiver.bat COM3 [ボーレート(既定9600)]
  echo.
  echo 接続中のポートは「デバイスマネージャー」の「ポート(COM と LPT)」で確認してください。
  exit /b 1
)

if "%QZSS_INGEST_TOKEN%"=="" (
  echo QZSS_INGEST_TOKEN が未設定です。事前に「set QZSS_INGEST_TOKEN=xxxx」を実行してください。
  exit /b 1
)

if not exist venv (
  echo Python venv が無いので作成します
  python -m venv venv
  call venv\Scripts\activate.bat
  pip install -q -r requirements.txt
) else (
  call venv\Scripts\activate.bat
)

if "%QZSS_CLOUD_URL%"=="" set QZSS_CLOUD_URL=https://eq.shum10.com/ingest

echo 受信機からの取り込みを開始し、重要な通報を地図へ送信します (%PORT% @ %BAUDRATE%) -^> %QZSS_CLOUD_URL%
python read_legacy_dual.py %PORT% %BAUDRATE% --nmea
