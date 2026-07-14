"""デコード済みJSONの送信先(ローカルFIFO or クラウド)を切り替えるヘルパー。

環境変数 QZSS_CLOUD_URL が設定されていればクラウド(Cloud Run等)の
/ingest エンドポイントへHTTPS POSTする。未設定ならこれまで通り
ローカルのFIFO(qzss_pipe)に書き込む(同一マシンでのテスト用)。
"""
import json
import os
import urllib.error
import urllib.request

CLOUD_URL = os.environ.get("QZSS_CLOUD_URL", "").strip()
INGEST_TOKEN = os.environ.get("QZSS_INGEST_TOKEN", "").strip()


def send(payload_json_str):
    if CLOUD_URL:
        _send_cloud(payload_json_str)
    else:
        _send_local_fifo(payload_json_str)


def _send_cloud(payload_json_str):
    req = urllib.request.Request(
        CLOUD_URL,
        data=payload_json_str.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": INGEST_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print("⚠️ クラウドへの送信に失敗しました:", e)


def _send_local_fifo(payload_json_str):
    with open("qzss_pipe", "w") as fifo:
        fifo.write(payload_json_str + "\n")
