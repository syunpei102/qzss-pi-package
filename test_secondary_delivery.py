import unittest
from unittest.mock import patch, Mock
import read_legacy_dual as m

class SecondaryTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(m,'send_queue',m.SendQueue(3)),patch.object(m,'local_send_queue',m.SendQueue(3)),patch.object(m,'secondary_send_queue',m.SendQueue(3)),patch.object(m,'SECONDARY_URL','https://review.invalid/ingest'),patch.object(m,'SECONDARY_TOKEN','review-only'),patch.object(m,'LOCAL_URL','http://localhost/ingest')]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def test_all_destinations_are_independent_and_payload_is_copied(self):
        payload={'raw':'test','values':[1]}
        m.enqueue_send(payload,priority=m.PRIORITY_URGENT)
        payload['values'].append(2)
        self.assertEqual(len(m.send_queue),1)
        self.assertEqual(len(m.local_send_queue),1)
        secondary=m.secondary_send_queue.get(timeout=0)
        self.assertEqual(secondary.payload['report']['values'],[1])
        sender=Mock();sender.send.return_value=False
        self.assertFalse(m.process_secondary_item(sender,secondary))
        self.assertEqual(len(m.send_queue),1)
        self.assertEqual(len(m.secondary_send_queue),0)

    def test_stale_queue_is_discarded_without_send(self):
        m.enqueue_send({'raw':'test'})
        item=m.secondary_send_queue.get(timeout=0)
        sender=Mock()
        self.assertFalse(m.process_secondary_item(sender,item,clock=lambda:item.payload['queued_at']+11))
        sender.send.assert_not_called()

    def test_missing_secondary_token_keeps_primary_and_local(self):
        with patch.object(m,'SECONDARY_TOKEN',''):
            m.enqueue_send({'raw':'test'})
        self.assertEqual(len(m.send_queue),1)
        self.assertEqual(len(m.local_send_queue),1)
        self.assertEqual(len(m.secondary_send_queue),0)

    def test_secondary_backlog_is_bounded_and_heartbeat_coalesces(self):
        for i in range(100):m.enqueue_send({'n':i},priority=m.PRIORITY_HEARTBEAT)
        self.assertEqual(len(m.secondary_send_queue),1)
        self.assertEqual(m.secondary_send_queue.get(timeout=0).payload['report']['n'],99)

if __name__=='__main__':unittest.main()
