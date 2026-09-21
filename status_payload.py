#!/usr/bin/env python3
"""report_status.sh が送る /device/status のJSON本文を，Python標準のJSONエンコーダーで生成する(#18)．

以前はシェルのヒアドキュメントへ値を直接埋め込んでいたため，ホスト名・コミット等に
引用符・バックスラッシュ・改行が含まれるとJSONが壊れた．値は環境変数で受け取り，
数値項目は数値として検証(不正・空・NaN/Infは null)してから json.dumps する．

環境変数: DEVICE_ID, HOSTNAME_STR, TEMPERATURE, UPTIME_SEC, DISK_FREE_PCT,
          GIT_COMMIT_MAP, GIT_COMMIT_PI, QZSS_COMMAND_INBOX(ackの永続化ファイル)
"""
import json
import math
import os
import sys

import command_inbox


def to_number(value):
    """'45.1' -> 45.1，'120' -> 120，空・不正・NaN・Infinity -> None(JSONのnull)"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
    return number


def build_payload(env, acked_ids):
    return {
        "device_id": env.get("DEVICE_ID", ""),
        "hostname": env.get("HOSTNAME_STR", ""),
        "temperature_c": to_number(env.get("TEMPERATURE")),
        "uptime_sec": to_number(env.get("UPTIME_SEC")),
        "disk_free_pct": to_number(env.get("DISK_FREE_PCT")),
        "git_commit_map": env.get("GIT_COMMIT_MAP", ""),
        "git_commit_pi": env.get("GIT_COMMIT_PI", ""),
        "supports_ack": True,
        "acked_command_ids": list(acked_ids),
    }


def main(env=None, out=None):
    env = os.environ if env is None else env
    out = out or sys.stdout
    path = env.get("QZSS_COMMAND_INBOX", command_inbox.DEFAULT_PATH)
    acked = command_inbox.load(path)["acked_ids"]
    # allow_nan=False: 万一NaNが紛れてもサーバーが解釈できない不正JSONを出さない
    out.write(json.dumps(build_payload(env, acked), ensure_ascii=False, allow_nan=False))
    out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
