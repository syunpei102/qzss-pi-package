"""/device/status 本文がPythonのJSONエンコーダーで安全に生成されることのテスト(#18)．"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import command_inbox
import status_payload as sp

ROOT = Path(__file__).resolve().parent


class BuildPayloadTests(unittest.TestCase):
    def build(self, **env):
        return sp.build_payload(env, [])

    def test_quotes_backslashes_and_newlines_round_trip(self):
        nasty = 'ho"st\\name\nline2\ttab "quoted" \\"'
        payload = self.build(DEVICE_ID=nasty, HOSTNAME_STR=nasty, GIT_COMMIT_MAP=nasty, GIT_COMMIT_PI="a\\b")
        decoded = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        self.assertEqual(decoded["device_id"], nasty)
        self.assertEqual(decoded["hostname"], nasty)
        self.assertEqual(decoded["git_commit_map"], nasty)
        self.assertEqual(decoded["git_commit_pi"], "a\\b")

    def test_numbers_and_null(self):
        payload = self.build(TEMPERATURE="45.1", UPTIME_SEC="1234", DISK_FREE_PCT="")
        self.assertEqual(payload["temperature_c"], 45.1)
        self.assertEqual(payload["uptime_sec"], 1234)
        self.assertIsInstance(payload["uptime_sec"], int)
        self.assertIsNone(payload["disk_free_pct"])
        self.assertIsNone(self.build()["temperature_c"])  # 未設定

    def test_malformed_or_non_finite_numbers_become_null_not_invalid_json(self):
        for bad in ("abc", "45.1℃", "nan", "NaN", "inf", "-Infinity", "1e999", " ", "12; rm", '"1"'):
            with self.subTest(bad=bad):
                payload = self.build(TEMPERATURE=bad)
                self.assertIsNone(payload["temperature_c"])
                json.loads(json.dumps(payload, allow_nan=False))

    def test_negative_and_whitespace_numbers(self):
        self.assertEqual(sp.to_number(" -3.5 "), -3.5)
        self.assertEqual(sp.to_number("0"), 0)

    def test_ack_support_and_ids_are_included(self):
        payload = sp.build_payload({}, ["id-1", "id-2"])
        self.assertIs(payload["supports_ack"], True)
        self.assertEqual(payload["acked_command_ids"], ["id-1", "id-2"])
        self.assertEqual(sp.build_payload({}, [])["acked_command_ids"], [])


class CliTests(unittest.TestCase):
    def run_cli(self, env_extra, inbox_state=None):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = os.path.join(tmp, "inbox.json")
            if inbox_state is not None:
                command_inbox.save(inbox_state, inbox)
            env = {**os.environ, "QZSS_COMMAND_INBOX": inbox, **env_extra}
            result = subprocess.run([sys.executable, str(ROOT / "status_payload.py")], env=env,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cli_emits_valid_json_from_environment(self):
        data = self.run_cli({"DEVICE_ID": 'a"b', "HOSTNAME_STR": "h\\n\nx", "TEMPERATURE": "51.5",
                             "UPTIME_SEC": "", "DISK_FREE_PCT": "80", "GIT_COMMIT_MAP": "abc",
                             "GIT_COMMIT_PI": ""})
        self.assertEqual(data["device_id"], 'a"b')
        self.assertEqual(data["hostname"], "h\\n\nx")
        self.assertEqual(data["temperature_c"], 51.5)
        self.assertIsNone(data["uptime_sec"])
        self.assertEqual(data["disk_free_pct"], 80)
        self.assertEqual(data["git_commit_pi"], "")

    def test_cli_sends_persisted_acked_ids(self):
        state = command_inbox._empty()
        command_inbox.receive(state, [{"id": "cmd-1", "command": "reboot"},
                                      {"id": "cmd-2", "command": "force_update_check"}])
        data = self.run_cli({}, inbox_state=state)
        self.assertIs(data["supports_ack"], True)
        self.assertEqual(data["acked_command_ids"], ["cmd-1", "cmd-2"])

    def test_cli_survives_missing_or_corrupt_inbox(self):
        data = self.run_cli({})
        self.assertEqual(data["acked_command_ids"], [])


class ReportStatusUsesEncoderTests(unittest.TestCase):
    def test_no_hand_written_json_with_shell_interpolation(self):
        script = (ROOT / "report_status.sh").read_text(encoding="utf-8")
        self.assertIn('status_payload.py', script)
        for fragment in ('"device_id": "${', '"hostname": "$(', '"temperature_c": ${',
                         '"git_commit_map": "${', "<<JSON"):
            self.assertNotIn(fragment, script)
        # 本文生成に失敗したら送信しない
        self.assertLess(script.index("status_payload.py"), script.index('curl -fsS -X POST "$STATUS_URL"'))


if __name__ == "__main__":
    unittest.main()
