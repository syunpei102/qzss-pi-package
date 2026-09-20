"""OTA制御フローの退行を検出する軽量な静的テスト．"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class OtaScriptTests(unittest.TestCase):
    def test_update_process_has_an_exclusive_lock(self):
        script = (ROOT / 'update_check.sh').read_text(encoding='utf-8')
        self.assertIn('flock', script)

    def test_urgent_marker_is_written_only_after_successful_update(self):
        script = (ROOT / 'check_urgent.sh').read_text(encoding='utf-8')
        update_pos = script.index('"$DIR/update_check.sh"')
        seen_pos = script.index('> "$SEEN_FILE"')
        self.assertLess(update_pos, seen_pos)

    def test_ota_applies_systemd_changes(self):
        script = (ROOT / 'update_check.sh').read_text(encoding='utf-8')
        self.assertIn('systemctl daemon-reload', script)
        self.assertIn('systemd/', script)


if __name__ == '__main__':
    unittest.main()
