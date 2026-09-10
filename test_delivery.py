"""受信機の送信・重複判定の回帰テスト．外部通信は行わない．"""
import unittest
from unittest.mock import Mock
import read_legacy_dual as m


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        m.recent_content_keys.clear()
        self.alert = dict(type='QzssDcxLAlert', a4_hazard_type='Flood',
                          ex1_target_area_code_raw=13101, a1_message_type='Alert',
                          a5_severity='Moderate', raw='aa')

    def test_clear_escalation_and_reissue_are_delivered(self):
        sequence = [self.alert, {**self.alert, 'a5_severity': 'Extreme'},
                    {**self.alert, 'a1_message_type': 'All Clear'}, self.alert]
        for i, report in enumerate(sequence):
            self.assertFalse(m.is_recent_duplicate(report, '', now=i))

    def test_changed_instructions_and_extent_are_delivered(self):
        self.assertFalse(m.is_recent_duplicate(self.alert, '', now=0))
        self.assertFalse(m.is_recent_duplicate({**self.alert, 'a14_ellipse_semi_major_axis': 200}, '', now=1))

    def test_transport_changes_only_are_suppressed(self):
        self.assertFalse(m.is_recent_duplicate(self.alert, '', now=0))
        self.assertTrue(m.is_recent_duplicate({**self.alert, 'raw': 'bb', 'satellite_id': 2}, '', now=1))

    def test_raw_and_semantic_expire_without_sliding_window(self):
        for report in (self.alert, {'type': 'QzssDcReportJmaTsunami', 'raw': 'cc'}):
            with self.subTest(report=report):
                m.recent_content_keys.clear()
                self.assertFalse(m.is_recent_duplicate(report, '', now=0))
                self.assertTrue(m.is_recent_duplicate(report, '', now=299))
                self.assertFalse(m.is_recent_duplicate(report, '', now=300))

    def test_cache_is_bounded(self):
        for i in range(100):
            m.is_recent_duplicate({'raw': str(i)}, '', now=i)
        self.assertEqual(len(m.recent_content_keys), m.RECENT_CONTENT_HISTORY_SIZE)

    def test_http_status_is_checked(self):
        for status in (200, 201, 204, 400, 401, 429, 500, 503):
            with self.subTest(status=status):
                sender = m.Sender('http://unused.invalid/ingest', '')
                sender.conn = Mock()
                sender.conn.getresponse.return_value.status = status
                self.assertEqual(sender.send(self.alert), 200 <= status < 300)

    def test_jalert_area_change_is_not_a_duplicate(self):
        jalert = dict(type='QzssDcxJAlert', a4_hazard_type='Missile',
                      ex9_target_area_list_ja=['Tokyo'], raw='jj')
        self.assertFalse(m.is_recent_duplicate(jalert, '', now=0))
        self.assertFalse(m.is_recent_duplicate({**jalert, 'ex9_target_area_list_ja': ['Osaka']}, '', now=1))
        self.assertTrue(m.is_recent_duplicate(jalert, '', now=2))

    def test_unrecognized_type_falls_back_to_raw_comparison(self):
        report = {'type': 'QzssDcReportJmaTsunami', 'raw': 'zz'}
        self.assertFalse(m.is_recent_duplicate(report, '', now=0))
        self.assertTrue(m.is_recent_duplicate(report, '', now=1))
        self.assertFalse(m.is_recent_duplicate({**report, 'raw': 'other'}, '', now=2))

    def test_connection_failure_reconnects_once(self):
        sender = m.Sender('http://unused.invalid/ingest', '')
        failed, good = Mock(), Mock()
        failed.request.side_effect = OSError('connection closed')
        good.getresponse.return_value.status = 204
        sender.conn = failed
        sender._connect = lambda: setattr(sender, 'conn', good)
        self.assertTrue(sender.send(self.alert))
        failed.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
