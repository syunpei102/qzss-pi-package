"""UBX/NMEAのフレーム切り出し(長さ2バイト・上限・再同期)の回帰テスト．"""
import unittest

import read_legacy_dual as m


def ubx(cls, mid, payload):
    body = bytes([cls, mid]) + len(payload).to_bytes(2, 'little') + payload
    ck_a, ck_b = m.ubx_checksum(body)
    return m.UBX_SYNC + body + bytes([ck_a, ck_b])


def nmea(body):
    return ('$%s*%02X\r\n' % (body, m.nmea_checksum(body))).encode()


class FramerTests(unittest.TestCase):
    def test_length_above_255_uses_both_bytes(self):
        frame = ubx(0x0A, 0x04, bytes(300))
        self.assertEqual(m.ubx_payload_length(frame), 300)
        self.assertEqual(m.StreamFramer().feed(frame), [('ubx', frame)])

    def test_frame_split_across_reads(self):
        frame = ubx(0x02, 0x13, bytes(range(44)))
        framer = m.StreamFramer()
        out = []
        for i in range(0, len(frame), 5):
            out += framer.feed(frame[i:i + 5])
        self.assertEqual(out, [('ubx', frame)])

    def test_oversized_length_is_resynchronised_not_waited_for(self):
        good = ubx(0x02, 0x13, bytes(44))
        bogus = m.UBX_SYNC + b'\x02\x13\xff\xff'  # 65535バイトを待ち続けてはいけない
        out = m.StreamFramer().feed(bogus + good)
        self.assertEqual(out, [('ubx', good)])

    def test_bad_checksum_frame_does_not_swallow_following_frame(self):
        good = ubx(0x02, 0x13, bytes(44))
        broken = bytearray(ubx(0x02, 0x13, bytes(44)))
        broken[-1] ^= 0xFF
        out = m.StreamFramer().feed(bytes(broken) + good)
        self.assertEqual(out, [('ubx', good)])

    def test_garbage_and_partial_headers_are_skipped(self):
        good = ubx(0x02, 0x13, bytes(44))
        out = m.StreamFramer().feed(b'\x00\xb5\xb5\x00\xff' + good)
        self.assertEqual(out, [('ubx', good)])

    def test_nmea_and_ubx_are_both_extracted_in_order(self):
        good = ubx(0x02, 0x13, bytes(44))
        line = nmea('GPGGA,1')
        out = m.StreamFramer().feed(line + good + line)
        self.assertEqual([k for k, _ in out], ['nmea', 'ubx', 'nmea'])

    def test_stray_dollar_before_ubx_does_not_hide_the_frame(self):
        good = ubx(0x02, 0x13, bytes(44))
        out = m.StreamFramer().feed(b'$abc' + good)
        self.assertIn(('ubx', good), out)

    def test_endless_line_without_newline_is_bounded(self):
        framer = m.StreamFramer()
        framer.feed(b'$' + b'A' * 5000)
        self.assertLessEqual(len(framer.buf), m.NMEA_MAX_LINE + 1)

    def test_endless_garbage_keeps_buffer_small(self):
        framer = m.StreamFramer()
        framer.feed(b'\x01' * 100000)
        self.assertEqual(len(framer.buf), 0)

    def test_short_or_truncated_sfrbx_is_ignored(self):
        self.assertIsNone(m.ubx2qzqsm(b'\xb5\x62\x02\x13\x2c\x00\x05'))
        self.assertIsNone(m.ubx2qzqsm(b''))

    def test_generated_sentence_checksum_is_two_uppercase_digits(self):
        for prn_offset in m.satellite_id:
            frame = bytearray(52)
            frame[:7] = b'\xb5\x62\x02\x13\x2c\x00\x05'
            frame[7] = prn_offset - 182
            frame[14 + 2] = 43 << 2  # 先頭ワードの2バイト目 = message type 43
            sentence = m.ubx2qzqsm(bytes(frame))
            self.assertIsNotNone(sentence)
            self.assertRegex(sentence, r'\*[0-9A-F]{2}$')
            self.assertTrue(m.is_valid_nmea_sentence(sentence))


if __name__ == '__main__':
    unittest.main()
