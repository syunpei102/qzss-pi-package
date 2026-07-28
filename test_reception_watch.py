#!/usr/bin/env python3
"""reception_watch.decide() の単体テスト(外部依存なしで実行可能)。

  実行: python3 test_reception_watch.py
  受信健全性の判定・段階的リカバリ(USBリセット→再起動→物理対応要請)・
  回復通知の状態遷移を、時間を進めながら検証する。
"""
import reception_watch as rw

FAILS = []


def check(cond, msg):
    print(("✅ " if cond else "❌ FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def actions_of(acts):
    return [a[0] for a in acts]


def notify_texts(acts):
    return [a[1] for a in acts if a[0] == "notify"]


def run():
    # 1) 受信正常(age小) → 何もしない、ok維持
    acts, st = rw.decide(age=5, state=dict(rw.DEFAULT_STATE), now=1000)
    check(acts == [] and st["state"] == "ok", "受信正常時は何もせずokのまま")

    # 2) 判定不能(age=None) → 何もしない
    acts, st = rw.decide(age=None, state=dict(rw.DEFAULT_STATE), now=1000)
    check(acts == [] and st["state"] == "ok", "判定不能(None)時は何もしない")

    # 3) ok→unstable(初回途絶) → 不安定通知 + USBリセット
    t = 1000
    acts, st = rw.decide(age=rw.STALE_SEC + 10, state=dict(rw.DEFAULT_STATE), now=t)
    check("notify" in actions_of(acts) and "usb_reset" in actions_of(acts), "初回途絶で不安定通知＋USBリセット")
    check(st["state"] == "unstable" and st["reset_count"] == 1, "状態がunstable・reset_count=1")
    check(any("不安定" in x for x in notify_texts(acts)), "不安定通知の文言")

    # 4) 直後(クールダウン中)の再実行 → リセットしない(通知も無し=既にunstable)
    t2 = t + 30  # RESET_COOLDOWN(120)未満
    acts, st = rw.decide(age=rw.STALE_SEC + 40, state=st, now=t2)
    check("usb_reset" not in actions_of(acts), "クールダウン中はUSBリセットしない")
    check(st["reset_count"] == 1, "reset_countは増えない")

    # 5) クールダウン経過後 → 2回目のUSBリセット
    t3 = t + rw.RESET_COOLDOWN + 1
    acts, st = rw.decide(age=rw.STALE_SEC + 60, state=st, now=t3)
    check("usb_reset" in actions_of(acts) and st["reset_count"] == 2, "クールダウン後に2回目USBリセット")

    # 6) USBリセット規定回数超過 + 再起動クールダウン経過 → 受信機再起動
    t4 = t3 + rw.RESTART_COOLDOWN + 1
    acts, st = rw.decide(age=rw.STALE_SEC + 80, state=st, now=t4)
    check("restart" in actions_of(acts), "リセットで直らなければデコーダ再起動")
    check(any("再起動" in x for x in notify_texts(acts)), "再起動の通知文言")

    # 7) 10分以上復旧しない → 物理対応要請(1回のみ)
    t5 = t + rw.ESCALATE_SEC + 5
    acts, st = rw.decide(age=rw.STALE_SEC + 100, state=st, now=t5)
    check(any("物理" in x for x in notify_texts(acts)), "10分超で物理対応を要請")
    check(st["escalated"] is True, "escalatedフラグが立つ")
    # 同条件で再実行しても物理対応は繰り返さない
    acts2, st2 = rw.decide(age=rw.STALE_SEC + 100, state=st, now=t5 + 60)
    check(not any("物理" in x for x in notify_texts(acts2)), "物理対応要請は繰り返さない")

    # 8) 回復(age小)→ 回復通知 + ok へ、カウンタリセット
    acts, st = rw.decide(age=3, state=st, now=t5 + 120)
    check(any("回復" in x for x in notify_texts(acts)), "回復時に回復通知")
    check(st["state"] == "ok" and st["reset_count"] == 0 and st["escalated"] is False, "回復でok・カウンタ初期化")

    # 9) 回復直後にまた途絶 → 改めて不安定通知(ok→unstableなので)
    acts, st = rw.decide(age=rw.STALE_SEC + 10, state=st, now=t5 + 200)
    check("notify" in actions_of(acts) and st["state"] == "unstable", "回復後の再途絶で再び不安定検知")

    print("\n" + ("🎉 全テスト成功" if not FAILS else "❌ 失敗 {} 件: {}".format(len(FAILS), FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(run())
