"""リモートコマンドの durable inbox / processed ledger(ack・二重実行防止)の回帰テスト．"""
import io
import json
import os
import tempfile
import unittest

import command_inbox as ci


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "update_state", "command_inbox.json")

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *args, stdin=""):
        out = io.StringIO()
        code = ci.main(list(args), stdin=io.StringIO(stdin), out=out, path=self.path)
        return code, out.getvalue()

    def receive(self, commands):
        return self.run_cli("receive", stdin=json.dumps({"ok": True, "commands": commands}))

    def test_received_command_is_persisted_and_acked_before_execution(self):
        self.receive([{"id": "abc-1", "command": "reboot", "requestedAt": 1}])
        # プロセスを跨いでも(ファイルから読み直しても)残っている
        self.assertEqual(json.loads(self.run_cli("acked")[1]), ["abc-1"])
        self.assertEqual(self.run_cli("pending")[1], "abc-1\treboot\n")

    def test_redelivered_command_is_not_executed_twice(self):
        self.receive([{"id": "abc-1", "command": "reboot"}])
        self.run_cli("start", "abc-1")
        self.run_cli("finish", "abc-1", "done")
        self.receive([{"id": "abc-1", "command": "reboot"}])  # 応答喪失による再配信
        self.assertEqual(self.run_cli("pending")[1], "")
        self.assertEqual(json.loads(self.run_cli("acked")[1]), ["abc-1"])

    def test_crash_before_start_keeps_command_pending_so_it_is_not_lost(self):
        self.receive([{"id": "r1", "command": "reboot"}])
        # クラッシュ(何もせず再起動)後の再開
        self.assertEqual(self.run_cli("recover")[1], "")
        self.assertEqual(self.run_cli("pending")[1], "r1\treboot\n")

    def test_crash_after_executing_marker_never_reruns_reboot(self):
        self.receive([{"id": "r1", "command": "reboot"}])
        self.run_cli("start", "r1")
        code, out = self.run_cli("recover")
        self.assertEqual(out, "r1 reboot\n")  # 中断として通知される
        self.assertEqual(self.run_cli("pending")[1], "")
        self.assertEqual(self.run_cli("recover")[1], "")  # 2回目は通知も出ない

    def test_idempotent_command_is_retried_after_interruption(self):
        self.receive([{"id": "u1", "command": "force_update_check"}])
        self.run_cli("start", "u1")
        self.assertEqual(self.run_cli("recover")[1], "")
        self.assertEqual(self.run_cli("pending")[1], "u1\tforce_update_check\n")

    def test_invalid_entries_are_never_passed_to_the_shell(self):
        self.receive([
            {"id": "x; rm -rf /", "command": "reboot"},
            {"id": "ok-1", "command": "reboot; poweroff"},
            {"id": "a" * 65, "command": "reboot"},
            "garbage", 42, None, {"command": 1},
        ])
        pending = self.run_cli("pending")[1]
        self.assertNotIn("rm", pending)
        self.assertNotIn("poweroff", pending)
        self.assertEqual(pending, "")
        # 未対応コマンドはackして再配信を止めるが，実行はしない
        self.assertEqual(json.loads(self.run_cli("acked")[1]), ["ok-1"])

    def test_legacy_server_without_ids_still_works_but_cannot_ack(self):
        self.receive([{"command": "force_update_check"}])
        self.assertEqual(json.loads(self.run_cli("acked")[1]), [])
        self.assertEqual(len(self.run_cli("pending")[1].splitlines()), 1)

    def test_non_json_response_is_rejected_without_touching_state(self):
        code, _ = self.run_cli("receive", stdin="<html>502</html>")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.run_cli("pending")[1], "")

    def test_corrupt_ledger_is_quarantined_and_service_continues(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{broken")
        self.assertEqual(json.loads(self.run_cli("acked")[1]), [])
        self.assertTrue(os.path.exists(self.path + ".corrupt"))

    def test_ack_list_and_ledger_are_bounded(self):
        state = ci._empty()
        for i in range(ci.MAX_ENTRIES + 50):
            ci.receive(state, [{"id": "id%d" % i, "command": "force_update_check"}], now=1000 + i)
            ci.set_state(state, "id%d" % i, "done", now=1000 + i)
        self.assertLessEqual(len(state["entries"]), ci.MAX_ENTRIES)
        self.assertLessEqual(len(state["acked_ids"]), ci.MAX_ACKED)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        state = ci._empty()
        ci.save(state, self.path)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["command_inbox.json"])

    def test_failed_and_unfinished_commands_are_not_pruned(self):
        state = ci._empty()
        ci.receive(state, [{"id": "keep", "command": "reboot"}], now=0)
        ci.receive(state, [], now=ci.MAX_AGE_SEC * 10)
        self.assertIn("keep", state["entries"])  # received のままなら古くても残す


class ReportStatusWiringTests(unittest.TestCase):
    def setUp(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "report_status.sh"), encoding="utf-8") as f:
            self.script = f.read()

    def test_status_report_advertises_ack_support_and_sends_persisted_ids(self):
        # 本文はstatus_payload.py(JSONエンコーダー)が生成する．内容は test_status_payload.py で検証
        self.assertIn('status_payload.py', self.script)
        self.assertNotIn('"supports_ack": true', self.script)  # シェルへ手書きしない

    def test_commands_are_processed_through_the_ledger_not_word_splitting(self):
        self.assertNotIn("for cmd in $commands", self.script)
        self.assertIn('"$INBOX" receive', self.script)
        self.assertIn('"$INBOX" start "$cmd_id"', self.script)

    def test_reboot_is_marked_executing_before_it_is_run(self):
        self.assertLess(self.script.index('"$INBOX" start "$cmd_id"'),
                        self.script.index("sudo -n /usr/bin/systemctl reboot"))

    def test_failed_reboot_clears_the_success_marker(self):
        tail = self.script[self.script.index("sudo -n /usr/bin/systemctl reboot"):]
        self.assertIn('rm -f "$STATE_DIR/reboot_requested"', tail)

    def test_update_command_status_is_checked(self):
        self.assertIn("update_status=$?", self.script)


if __name__ == "__main__":
    unittest.main()
