#!/usr/bin/env python3
"""QZSS 受信監視 + 自動復旧 + Discord通知。

背景:
  受信機(CH340 USBシリアル)が間欠的に物理切断/再認識を起こし、衛星データが
  数分〜十数分途切れることがある(2026-07-28の熊本地震ではこの途切れの間に
  EEW/震度速報/津波を取りこぼした)。ハートビートの serial_connected は
  障害中も true のままで当てにならないため、ここでは「有効な衛星文(QZQSM)を
  最後にデコードしてからの経過秒」で受信の健全性を判定する。

段階的リカバリ(衛星は同じ通報をしばらく複数回再送するので、素早く復旧すれば
後続の再送を拾える):
  1) 有効文が STALE_SEC 秒途絶 → CH340 を USB リセット(物理抜き差し相当)
  2) USB リセットを数回試してもダメ → 受信機(デコーダ)サービスを再起動
  3) 10分以上復旧しない → Discord で物理対応(挿し直し/ケーブル・アダプタ交換)を要請
  復帰したら回復を通知する。

systemd タイマー(qzss-reception-watch.timer)で 30 秒ごとに実行する想定。
判定は decide() という純粋関数に集約し、test_reception_watch.py で単体テストする。
"""
import os
import sys
import json
import time
import glob
import fcntl
import socket
import subprocess
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "update_state", "reception_state.json")
ENV_FILE = os.path.join(BASE_DIR, "qzss.env")

DECODER_UNIT = os.environ.get("QZSS_DECODER_UNIT", "qzss-decoder@qzss01.service")
CH340_VID, CH340_PID = "1a86", "7523"  # QinHeng CH340/CH341 serial converter

# --- 閾値(秒) ---
STALE_SEC = 90            # 有効文がこれ以上途絶したら「受信不安定」とみなす
RESET_COOLDOWN = 120      # USBリセットの最短間隔
RESETS_BEFORE_RESTART = 2 # USBリセットをこの回数試してもダメなら受信機再起動へ
RESTART_COOLDOWN = 300    # 受信機再起動の最短間隔
ESCALATE_SEC = 600        # これ以上復旧しなければ物理対応を要請(1回のみ)
LOOKBACK = "16 min ago"   # 有効文を遡って探す範囲

DEFAULT_STATE = {
    "state": "ok",          # ok | unstable
    "unstable_since": 0,
    "last_reset": 0,
    "reset_count": 0,
    "last_restart": 0,
    "escalated": False,
}


def read_env(key):
    """os.environ を優先し、無ければ qzss.env から KEY=VALUE を読む。"""
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


def last_valid_age(now=None):
    """最後の有効な衛星文(QZQSM)からの経過秒。ルックバック内に無ければ大きな値。
    journalctl が失敗した場合は None(判定不能=何もしない)。"""
    if now is None:
        now = time.time()
    try:
        out = subprocess.run(
            ["journalctl", "-u", DECODER_UNIT, "--since", LOOKBACK, "-o", "short-unix", "--no-pager"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:
        print("journalctl failed:", e, file=sys.stderr)
        return None
    last_ts = None
    for line in out.splitlines():
        if "QZQSM" in line:
            try:
                last_ts = float(line.split()[0])
            except (ValueError, IndexError):
                pass
    if last_ts is None:
        return 10 ** 9  # ルックバック内に有効文ゼロ = 完全に途絶
    return max(0.0, now - last_ts)


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        return {**DEFAULT_STATE, **s}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def notify(msg):
    url = read_env("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL 未設定のため通知スキップ:", msg, file=sys.stderr)
        return
    content = "📡 QZSS 受信監視 ({})\n{}".format(socket.gethostname(), msg)
    data = json.dumps({"content": content}).encode()
    try:
        # User-Agent を明示しないと urllib の既定UA("Python-urllib/x.y")が
        # Discord 側に 403 で弾かれる(curl は独自UAなので通っていた)
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "qzss-reception-watch/1.0 (+https://eq.shum10.com)",
        })
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("Discord通知に失敗:", e, file=sys.stderr)


def usb_reset():
    """CH340 を USBDEVFS_RESET ioctl で再認識させる(物理的な抜き差し相当)。
    root 権限が必要(/dev/bus/usb/... への書き込み)。成功したら True。"""
    USBDEVFS_RESET = (ord("U") << 8) | 20  # _IO('U', 20) == 0x5514
    reset_any = False
    for devdir in glob.glob("/sys/bus/usb/devices/*/"):
        try:
            with open(devdir + "idVendor") as f:
                vid = f.read().strip()
            with open(devdir + "idProduct") as f:
                pid = f.read().strip()
        except OSError:
            continue
        if vid != CH340_VID or pid != CH340_PID:
            continue
        try:
            with open(devdir + "busnum") as f:
                busnum = int(f.read())
            with open(devdir + "devnum") as f:
                devnum = int(f.read())
        except (OSError, ValueError):
            continue
        path = "/dev/bus/usb/{:03d}/{:03d}".format(busnum, devnum)
        try:
            fd = os.open(path, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                reset_any = True
                print("USBリセット成功:", path)
            finally:
                os.close(fd)
        except OSError as e:
            print("USBリセット失敗 {}: {}".format(path, e), file=sys.stderr)
    if not reset_any:
        print("CH340が見つからずUSBリセットできませんでした(完全に切断されている可能性)", file=sys.stderr)
    return reset_any


def restart_decoder():
    try:
        subprocess.run(["systemctl", "restart", DECODER_UNIT], timeout=40, check=False)
        return True
    except Exception as e:
        print("デコーダ再起動に失敗:", e, file=sys.stderr)
        return False


def decide(age, state, now):
    """受信状況から実行すべきアクションと次stateを決める純粋関数(テスト対象)。
    age: 最後の有効文からの経過秒(None=判定不能)。state: 前回状態dict。now: 現在時刻。
    戻り値: (actions, new_state)。actions は ("notify"|"usb_reset"|"restart", 引数) のリスト。"""
    s = dict(state)
    actions = []
    if age is None:
        return actions, s  # 判定不能時は現状維持

    if age <= STALE_SEC:
        # 受信は正常。直前まで不安定だったなら回復を通知してリセット。
        if s["state"] == "unstable":
            downtime = int(max(0, now - s.get("unstable_since", now)))
            actions.append(("notify", "✅ 受信回復。約{}分{}秒の途切れから復帰しました。".format(downtime // 60, downtime % 60)))
        s.update(DEFAULT_STATE)
        return actions, s

    # age > STALE_SEC : 受信不安定
    if s["state"] == "ok":
        s.update(state="unstable", unstable_since=now, last_reset=0, reset_count=0, last_restart=0, escalated=False)
        actions.append(("notify", "⚠️ 受信不安定: 有効な衛星データが約{}秒途絶しています。自動復旧を試みます。".format(int(age))))

    # 自動復旧(USBリセット/デコーダ再起動)は escalate 前だけ試みる。
    # escalate 後(=物理対応が必要と判断済み)は、繰り返しても直らないので
    # 止めて回復を待つ(無駄な再起動ループと通知スパムを防ぐ)。
    if not s.get("escalated"):
        if s.get("reset_count", 0) < RESETS_BEFORE_RESTART and now - s.get("last_reset", 0) >= RESET_COOLDOWN:
            # 段階1: USBリセット(物理抜き差し相当)
            actions.append(("usb_reset", None))
            s["last_reset"] = now
            s["reset_count"] = s.get("reset_count", 0) + 1
        elif s.get("reset_count", 0) >= RESETS_BEFORE_RESTART and now - s.get("last_restart", 0) >= RESTART_COOLDOWN:
            # 段階2: 受信機(デコーダ)再起動
            actions.append(("restart", None))
            actions.append(("notify", "🔄 USBリセットで回復しないため、受信機(デコーダ)を自動再起動しました。"))
            s["last_restart"] = now

    # 段階3: 長時間復旧しない → 物理対応を要請(1回のみ)。以降は自動復旧を止める。
    if not s.get("escalated") and now - s.get("unstable_since", now) >= ESCALATE_SEC:
        actions.append(("notify", "❗ 受信が10分以上復旧しません。自動復旧(USBリセット/再起動)では直らないため停止します。"
                                  "\nGNSSアンテナの空(特に真上=QZSS)の見通し、アンテナの向き/転倒、ケーブル・コネクタ、"
                                  "またはCH340 USB変換器の物理確認が必要です。"))
        s["escalated"] = True

    return actions, s


def main():
    now = time.time()
    age = last_valid_age(now)
    state = load_state()
    actions, new_state = decide(age, state, now)
    for kind, arg in actions:
        if kind == "notify":
            notify(arg)
        elif kind == "usb_reset":
            usb_reset()
        elif kind == "restart":
            restart_decoder()
    save_state(new_state)
    age_str = "n/a" if age is None else "{:.0f}s".format(age)
    print("age={} state={} actions={}".format(age_str, new_state["state"], [a[0] for a in actions]))


if __name__ == "__main__":
    main()
