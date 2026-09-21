"""受信監視: 外部コマンドが失敗したときに成功通知・クールダウンを残さないことの回帰テスト．"""
import unittest
from unittest.mock import Mock, patch

import reception_watch as rw


def unstable_state(**overrides):
    return {**rw.DEFAULT_STATE, "state": "unstable", "unstable_since": 1000, **overrides}


class ApplyActionsTests(unittest.TestCase):
    def runners(self, usb=True, restart=True):
        return {"notify": Mock(), "usb_reset": Mock(return_value=usb), "restart": Mock(return_value=restart)}

    def test_failed_restart_keeps_no_cooldown_and_sends_no_success_notice(self):
        old = unstable_state(reset_count=2, last_reset=1000, last_restart=0)
        actions, new = rw.decide(rw.STALE_SEC + 10, old, now=2000)
        self.assertIn(("restart", None), actions)
        runners = self.runners(restart=False)
        final = rw.apply_actions(actions, old, new, runners)
        self.assertEqual(final["last_restart"], 0)
        texts = [c.args[0] for c in runners["notify"].call_args_list]
        self.assertFalse(any("自動再起動しました" in t for t in texts))
        self.assertTrue(any("失敗" in t for t in texts))

    def test_successful_restart_records_cooldown_and_notifies(self):
        old = unstable_state(reset_count=2, last_reset=1000, last_restart=0)
        actions, new = rw.decide(rw.STALE_SEC + 10, old, now=2000)
        runners = self.runners()
        final = rw.apply_actions(actions, old, new, runners)
        self.assertEqual(final["last_restart"], 2000)
        texts = [c.args[0] for c in runners["notify"].call_args_list]
        self.assertTrue(any("自動再起動しました" in t for t in texts))

    def test_failed_usb_reset_can_be_retried_immediately(self):
        old = dict(rw.DEFAULT_STATE)
        actions, new = rw.decide(rw.STALE_SEC + 10, old, now=2000)
        final = rw.apply_actions(actions, old, new, self.runners(usb=False))
        self.assertEqual(final["last_reset"], 0)
        # クールダウンが無いので30秒後の次回監視でもう一度試行できる
        actions2, _ = rw.decide(rw.STALE_SEC + 40, final, now=2030)
        self.assertIn(("usb_reset", None), actions2)

    def test_successful_usb_reset_keeps_cooldown(self):
        old = dict(rw.DEFAULT_STATE)
        actions, new = rw.decide(rw.STALE_SEC + 10, old, now=2000)
        final = rw.apply_actions(actions, old, new, self.runners(usb=True))
        self.assertEqual(final["last_reset"], 2000)


class CommandFailureTests(unittest.TestCase):
    def test_restart_decoder_reports_timeout_and_success(self):
        with patch.object(rw.subprocess, "run", side_effect=rw.subprocess.TimeoutExpired("systemctl", 40)):
            self.assertFalse(rw.restart_decoder())
        with patch.object(rw.subprocess, "run", return_value=Mock(returncode=0, stdout="", stderr="")):
            self.assertTrue(rw.restart_decoder())

    def test_journalctl_success_with_no_valid_sentence_is_a_long_outage(self):
        with patch.object(rw.subprocess, "run", return_value=Mock(returncode=0, stdout="", stderr="")):
            self.assertGreater(rw.last_valid_age(now=1000), rw.ESCALATE_SEC)

    def test_journalctl_exception_is_indeterminate(self):
        with patch.object(rw.subprocess, "run", side_effect=FileNotFoundError("journalctl")):
            self.assertIsNone(rw.last_valid_age(now=1000))


if __name__ == "__main__":
    unittest.main()
