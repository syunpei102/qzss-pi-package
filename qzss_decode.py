"""QZQSM(NMEA風)センテンスを azarashi でデコードし、JSON化するヘルパー。

read_legacy.py(実機からの取り込み)と test.py(シミュレータ)の両方から
共通で使う。
"""
import base64
import datetime
import json

import azarashi

# 緊急地震速報・震源・震度速報・津波関連のみ「重要」とし、
# クラウドへの送信対象にする(それ以外は通信量/コストの
# 節約のため送らない)。地図側の表示絞り込みと合わせてある。
IMPORTANT_CATEGORY_NOS = {1, 2, 3, 5}


def _jsonify(value):
    """azarashiのレポートが持つ値をJSONで表現できる形に変換する。"""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return base64.b16encode(bytes(value)).decode("ascii").lower()
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # DCXレポートの camf など、内部状態を保持するだけの非公開オブジェクトは
    # 表示に使わないので文字列化のみしてクラッシュを避ける
    return str(value)


def decode_to_json(sentence):
    """QZQSMセンテンス(1行)を (JSON文字列, 重要かどうか) のタプルで返す。

    デコードできない/未対応のメッセージ種別の場合は、種別不明として
    生データだけを載せたJSONを返す(呼び出し側でクラッシュさせない)。
    重要度は disaster_category_no が IMPORTANT_CATEGORY_NOS に
    含まれるかどうかで判定する(送信するかどうかは呼び出し側で決める)。
    """
    try:
        report = azarashi.decode(sentence, msg_type="nmea")
    except Exception as e:
        payload = json.dumps(
            {
                "type": "DecodeError",
                "sentence": sentence,
                "error": str(e),
            },
            ensure_ascii=False,
        )
        return payload, False

    params = {k: _jsonify(v) for k, v in report.get_params().items()}
    params["type"] = type(report).__name__
    params["description"] = str(report)
    payload = json.dumps(params, ensure_ascii=False)

    important = (
        params.get("disaster_category_no") in IMPORTANT_CATEGORY_NOS
        or params.get("dcx_message_type") == "J-Alert"
    )
    return payload, important
