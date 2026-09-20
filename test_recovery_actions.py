"""受信監視が外部コマンド失敗を成功扱いしないための回帰テスト．"""
import unittest
from unittest.mock import Mock, patch

import reception_watch as rw


class RecoveryActionTests(unittest.TestCase):
    def test_journalctl_failure_is_indeterminate_not_a_long_outage(self):
        result = Mock(returncode=1, stdout='', stderr='permission denied')
        with patch.object(rw.subprocess, 'run', return_value=result):
            self.assertIsNone(rw.last_valid_age(now=1000))

    def test_systemctl_failure_is_reported(self):
        result = Mock(returncode=1, stdout='', stderr='unit not found')
        with patch.object(rw.subprocess, 'run', return_value=result):
            self.assertFalse(rw.restart_decoder())


if __name__ == '__main__':
    unittest.main()
