@echo off
REM 実機の受信機からクラウド(Cloud Run)へ送信する一発起動スクリプト(Windows用)。
REM 使い方: start_receiver.bat COM3 [ボーレート(既定9600)]

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

if not exist venv (
  echo Python venv が無いので作成します
  python -m venv venv
  call venv\Scripts\activate.bat
  pip install -q -r requirements.txt
) else (
  call venv\Scripts\activate.bat
)

set QZSS_CLOUD_URL_CRITICAL=https://qzss-map-85436528666.asia-northeast1.run.app/ingest
set QZSS_INGEST_TOKEN_CRITICAL=4552855f00070aecee0278b9ba8dbc7c
set QZSS_CLOUD_URL_CAUTION=https://qzss-map-caution-85436528666.asia-northeast1.run.app/ingest
set QZSS_INGEST_TOKEN_CAUTION=51af2853155bdbc88f29957ad9e65be8

echo 受信機からの取り込みを開始し、重要情報はメイン地図へ、注意情報は注意情報マップへ送信します (%PORT% @ %BAUDRATE%)
python read_legacy_dual.py %PORT% %BAUDRATE% --nmea
