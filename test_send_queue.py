"""送信キュー(有界・優先度付き・再試行待ちが新規緊急通報をブロックしない)の回帰テスト．"""
import threading
import time
import unittest
from unittest.mock import Mock

import read_legacy_dual as m


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class SendQueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def queue(self, size=5):
        return m.SendQueue(size, clock=self.clock)

    def test_urgent_is_taken_before_normal_and_heartbeat(self):
        q = self.queue()
        q.put({'n': 'hb'}, m.PRIORITY_HEARTBEAT)
        q.put({'n': 'normal'}, m.PRIORITY_NORMAL)
        q.put({'n': 'urgent'}, m.PRIORITY_URGENT)
        order = [q.get(timeout=0).payload['n'] for _ in range(3)]
        self.assertEqual(order, ['urgent', 'normal', 'hb'])

    def test_queue_is_bounded_and_drops_lowest_priority_oldest_first(self):
        q = self.queue(3)
        q.put({'n': 'normal1'}, m.PRIORITY_NORMAL)
        q.put({'n': 'normal2'}, m.PRIORITY_NORMAL)
        q.put({'n': 'urgent1'}, m.PRIORITY_URGENT)
        self.assertTrue(q.put({'n': 'urgent2'}, m.PRIORITY_URGENT))
        self.assertEqual(len(q), 3)
        names = sorted(q.get(timeout=0).payload['n'] for _ in range(3))
        self.assertEqual(names, ['normal2', 'urgent1', 'urgent2'])
        self.assertEqual(q.dropped, 1)

    def test_low_priority_newcomer_is_dropped_when_queue_holds_urgent_only(self):
        q = self.queue(2)
        q.put({'n': 'u1'}, m.PRIORITY_URGENT)
        q.put({'n': 'u2'}, m.PRIORITY_URGENT)
        self.assertFalse(q.put({'n': 'hb'}, m.PRIORITY_HEARTBEAT))
        self.assertEqual(len(q), 2)

    def test_unbounded_growth_is_impossible(self):
        q = self.queue(50)
        for i in range(10000):
            q.put({'i': i}, m.PRIORITY_NORMAL)
        self.assertEqual(len(q), 50)

    def test_only_latest_heartbeat_is_kept(self):
        q = self.queue()
        for i in range(10):
            q.put({'i': i}, m.PRIORITY_HEARTBEAT, retryable=False)
        self.assertEqual(len(q), 1)
        self.assertEqual(q.get(timeout=0).payload['i'], 9)

    def test_retry_wait_does_not_block_new_urgent_report(self):
        q = self.queue()
        q.put({'n': 'old'}, m.PRIORITY_URGENT, attempt=1, not_before=self.clock() + 30)
        q.put({'n': 'new-urgent'}, m.PRIORITY_URGENT)
        self.assertEqual(q.get(timeout=0).payload['n'], 'new-urgent')
        self.assertIsNone(q.get(timeout=0))  # 待機中の再試行はまだ取り出せない
        self.clock.t += 31
        self.assertEqual(q.get(timeout=0).payload['n'], 'old')

    def test_get_wakes_up_for_a_new_item_while_waiting_for_retry(self):
        q = m.SendQueue(5)  # 実時計
        q.put({'n': 'retry'}, m.PRIORITY_URGENT, attempt=1, not_before=time.monotonic() + 60)
        result = []
        thread = threading.Thread(target=lambda: result.append(q.get(timeout=5)))
        thread.start()
        time.sleep(0.1)
        q.put({'n': 'fresh'}, m.PRIORITY_URGENT)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].payload['n'], 'fresh')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.q = m.SendQueue(10, clock=self.clock)
        self.sender = Mock()
        self.sender.send.return_value = False
        self.sender.last_status = None

    def item(self, attempt=0, retryable=True):
        return m.QueuedItem({'type': 'X'}, m.PRIORITY_URGENT, attempt, retryable, 0, 1)

    def test_failed_send_is_requeued_without_sleeping(self):
        started = time.monotonic()
        m.process_send_item(self.sender, self.q, self.item(), clock=self.clock)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.q), 1)
        self.assertIsNone(self.q.get(timeout=0))
        self.clock.t += m.RETRY_BACKOFF_SEC + 0.1
        self.assertEqual(self.q.get(timeout=0).attempt, 1)

    def test_backoff_grows_and_is_capped(self):
        self.assertGreater(m.retry_delay(2), m.retry_delay(0))
        self.assertEqual(m.retry_delay(50), m.RETRY_BACKOFF_MAX_SEC)

    def test_gives_up_after_max_retries(self):
        m.process_send_item(self.sender, self.q, self.item(attempt=m.MAX_SEND_RETRIES), clock=self.clock)
        self.assertEqual(len(self.q), 0)

    def test_heartbeat_is_not_retried(self):
        m.process_send_item(self.sender, self.q, self.item(retryable=False), clock=self.clock)
        self.assertEqual(len(self.q), 0)

    def test_permanent_client_error_is_not_retried_but_429_and_5xx_are(self):
        for status, requeued in ((400, 0), (401, 0), (429, 1), (500, 1), (None, 1)):
            with self.subTest(status=status):
                q = m.SendQueue(10, clock=self.clock)
                self.sender.last_status = status
                m.process_send_item(self.sender, q, self.item(), clock=self.clock)
                self.assertEqual(len(q), requeued)

    def test_fresh_connection_failure_does_not_wait_twice(self):
        sender = m.Sender('http://unused.invalid/ingest', '')
        attempts = []

        def failing_connect():
            attempts.append(1)
            raise OSError('down')

        sender._connect = failing_connect
        self.assertFalse(sender.send({'type': 'X'}))
        self.assertEqual(len(attempts), 1)

    def test_routing_uses_priorities(self):
        self.assertEqual(m.report_priority('jalert'), m.PRIORITY_URGENT)
        self.assertEqual(m.report_priority('lalert'), m.PRIORITY_URGENT)
        self.assertEqual(m.report_priority(1), m.PRIORITY_URGENT)
        self.assertEqual(m.report_priority(10), m.PRIORITY_NORMAL)


class ConfigSyncTests(unittest.TestCase):
    def setUp(self):
        for name in m.last_known_local_settings:
            m.last_known_local_settings[name] = None

    def test_one_config_fetch_updates_both_settings(self):
        fetch = Mock(return_value={'showTrainingBroadcasts': False, 'lalertEnabled': True})
        post = Mock()
        m.sync_local_settings_once(fetch=fetch, post=post)
        self.assertEqual(fetch.call_count, 1)
        posted = {call.args[0]: call.args[1] for call in post.call_args_list}
        self.assertEqual(posted, {'/local-sync/training-broadcasts': False, '/local-sync/lalert': True})

    def test_unchanged_settings_are_not_posted_again_and_failed_ones_retry(self):
        fetch = Mock(return_value={'showTrainingBroadcasts': True, 'lalertEnabled': True})
        post = Mock(side_effect=[None, OSError('local down')])
        m.sync_local_settings_once(fetch=fetch, post=post)
        post = Mock()
        m.sync_local_settings_once(fetch=fetch, post=post)
        self.assertEqual([c.args[0] for c in post.call_args_list], ['/local-sync/lalert'])

    def test_fetch_failure_is_survived(self):
        m.sync_local_settings_once(fetch=Mock(side_effect=OSError('x')), post=Mock())


if __name__ == '__main__':
    unittest.main()
