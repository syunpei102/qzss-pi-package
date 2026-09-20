#!/usr/bin/env python3
"""管理サイトから受け取ったリモートコマンドの durable inbox / processed ledger．

report_status.sh から呼ぶ．サーバー(map/device-commands.js)は，status報告に
supports_ack:true が付いた端末へ，ackされるまでコマンド({id, command, requestedAt})を
再配信する(at-least-once)．端末側は次の方針で安全に扱う．

  1. 受信したコマンドは，実行前にまず inbox へ fsync+原子的rename で永続化し，
     同時に acked_command_ids へ加える(サーバーは次の報告でackを受けて再配信を止める)．
  2. 状態は received -> executing -> done / failed / interrupted と進む．
  3. 冪等なコマンド(force_update_check)は，実行中に電源断等で中断しても，
     次回 recover() で received へ戻して再実行する．
  4. 非冪等なコマンド(reboot)は executing を永続化してから実行する．executing のまま
     残っていたら「実行されたかもしれない」ので再実行せず interrupted にして通知だけ行う．
     (トレードオフ: executing を書いた直後〜再起動要求を出す前にクラッシュすると，
     そのrebootは実行されない．管理者が再度予約する．逆に二重再起動は決して起きない)
  5. idは英数字・-・_ の64文字以内，commandは許可リストのみ受け付ける(シェルへ渡す前に検証)．

CLI:
  command_inbox.py acked                    永続化済みのackすべきidをJSON配列で出力
  command_inbox.py receive < response.json  status応答のcommandsをinboxへ保存
  command_inbox.py recover                  中断されたコマンドを整理(interruptedは「id command」を出力)
  command_inbox.py pending                  実行待ちを「id<TAB>command」で出力
  command_inbox.py start <id>               executing にする(永続化してから実行するため)
  command_inbox.py finish <id> done|failed  結果を記録
"""
import json
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(BASE_DIR, "update_state", "command_inbox.json")

IDEMPOTENT = {"force_update_check": True, "reboot": False}
SUPPORTED_COMMANDS = tuple(IDEMPOTENT)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_ACKED = 200        # 永続化・送信するackの上限(古いものから捨てる)
MAX_ENTRIES = 500      # ledgerに残す最大件数
MAX_AGE_SEC = 7 * 24 * 3600  # サーバーの再配信期限(24時間)より十分長く覚えておく


def _empty():
    return {"acked_ids": [], "entries": {}}


def load(path=DEFAULT_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        acked = [i for i in data.get("acked_ids", []) if isinstance(i, str) and ID_RE.match(i)]
        entries = {k: v for k, v in data.get("entries", {}).items()
                   if ID_RE.match(k) and isinstance(v, dict) and v.get("command") in SUPPORTED_COMMANDS}
        return {"acked_ids": acked[-MAX_ACKED:], "entries": entries}
    except FileNotFoundError:
        return _empty()
    except (ValueError, OSError, AttributeError):
        # 壊れたファイルは退避して空から始める(退避しないと毎回失敗し続ける)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return _empty()


def save(state, path=DEFAULT_PATH):
    """一時ファイルへ書いてfsyncし，renameで置き換え，ディレクトリもfsyncする．"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = "{}.tmp.{}".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _prune(state, now):
    entries = state["entries"]
    for cid in [c for c, e in entries.items()
                if e.get("state") not in ("received", "executing") and now - e.get("updated_at", now) > MAX_AGE_SEC]:
        del entries[cid]
    if len(entries) > MAX_ENTRIES:
        finished = sorted((c for c, e in entries.items() if e.get("state") not in ("received", "executing")),
                          key=lambda c: entries[c].get("updated_at", 0))
        for cid in finished[:len(entries) - MAX_ENTRIES]:
            del entries[cid]
    state["acked_ids"] = state["acked_ids"][-MAX_ACKED:]


def receive(state, commands, now=None):
    """status応答のcommandsを検証してinboxへ追加する(既知のidは無視)．
    受け付けたidはすべてackへ加える(実行前ack)．追加した件数を返す．"""
    now = time.time() if now is None else now
    added = 0
    if not isinstance(commands, list):
        return 0
    for n, item in enumerate(commands):
        if isinstance(item, str):  # 旧形式(文字列のみ)。idが無いため合成し，ackはできない
            item = {"command": item}
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        cid = item.get("id")
        legacy = cid is None
        if legacy:
            cid = "legacy-{}-{}".format(int(now), n)
        if not isinstance(cid, str) or not ID_RE.match(cid):
            continue
        if cid in state["entries"]:
            if cid not in state["acked_ids"] and not legacy:
                state["acked_ids"].append(cid)
            continue
        if command not in SUPPORTED_COMMANDS:
            # 未対応コマンドは実行せず，ackだけして再配信を止める
            state["entries"][cid] = {"command": "force_update_check", "state": "failed",
                                     "detail": "unsupported", "received_at": now, "updated_at": now}
            state["entries"][cid]["command"] = "force_update_check"
            state["entries"][cid]["rejected_command"] = str(command)[:64]
        else:
            state["entries"][cid] = {"command": command, "state": "received",
                                     "received_at": now, "updated_at": now}
            added += 1
        if not legacy:
            state["acked_ids"].append(cid)
    _prune(state, now)
    return added


def pending(state):
    items = [(e.get("received_at", 0), cid, e["command"])
             for cid, e in state["entries"].items() if e.get("state") == "received"]
    return [(cid, command) for _, cid, command in sorted(items)]


def set_state(state, cid, new_state, now=None):
    entry = state["entries"].get(cid)
    if entry is None:
        raise KeyError(cid)
    entry["state"] = new_state
    entry["updated_at"] = time.time() if now is None else now


def recover(state, now=None):
    """前回executingのまま残ったコマンドを整理する．冪等なものはreceivedへ戻して再実行し，
    非冪等(reboot)は二重実行を避けinterruptedにする．interruptedにしたものを返す．"""
    interrupted = []
    for cid, entry in state["entries"].items():
        if entry.get("state") != "executing":
            continue
        if IDEMPOTENT[entry["command"]]:
            set_state(state, cid, "received", now)
        else:
            set_state(state, cid, "interrupted", now)
            interrupted.append((cid, entry["command"]))
    return interrupted


def main(argv, stdin=None, out=None, path=DEFAULT_PATH):
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    cmd = argv[0] if argv else ""
    state = load(path)
    if cmd == "acked":
        out.write(json.dumps(state["acked_ids"]) + "\n")
    elif cmd == "receive":
        try:
            data = json.loads(stdin.read())
            commands = data.get("commands", []) if isinstance(data, dict) else []
        except ValueError:
            print("応答がJSONではないためコマンドを無視します", file=sys.stderr)
            return 1
        receive(state, commands)
        save(state, path)
    elif cmd == "recover":
        interrupted = recover(state)
        save(state, path)
        for cid, command in interrupted:
            out.write("{} {}\n".format(cid, command))
    elif cmd == "pending":
        for cid, command in pending(state):
            out.write("{}\t{}\n".format(cid, command))
    elif cmd == "start" and len(argv) == 2:
        set_state(state, argv[1], "executing")
        save(state, path)
    elif cmd == "finish" and len(argv) == 3 and argv[2] in ("done", "failed"):
        set_state(state, argv[1], argv[2])
        save(state, path)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], path=os.environ.get("QZSS_COMMAND_INBOX", DEFAULT_PATH)))
