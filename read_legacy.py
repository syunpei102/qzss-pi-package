import sys
import argparse
import operator
from functools import reduce
from collections import deque
import json
import threading
import datetime
import serial
import time

import qzss_sink
from qzss_decode import decode_to_json

HEARTBEAT_INTERVAL_SEC = 30

# 災危通報は同一内容が配信終了条件を満たすまで数秒おきに再送され続ける仕様の
# ため、直近に送信済みの内容と完全一致する通報はクラウドへ再送しない。
# 判定にはデコード結果の raw(DCRメッセージ本体)を使う。プリアンブル(A/B/C)は
# 送信ごとに巡回し、sentence は内容が同じでも毎回変わるが、raw はプリアンブル・
# CRC・衛星IDを含まないため、内容が同じなら常に一致する。
RECENT_CONTENT_HISTORY_SIZE = 50
recent_content_keys = deque(maxlen=RECENT_CONTENT_HISTORY_SIZE)

# シリアル接続が実際に確立できている間だけ立てるフラグ。
# ハートビートはこれを見て送るかどうかを決めるので、アンテナ/USBが
# 抜けて再接続待ちになっている間は、プロセス自体が生きていても
# ハートビートが止まり、ブラウザ側は正しく「応答なし」を検知できる。
serial_ok = threading.Event()


def send_heartbeat_loop():
    """受信機(このプロセス)が生きていることを一定間隔でサーバーに知らせる。
    重要な災危通報は滅多に来ないため、これが無いと「受信機が本当に
    動いているか」をブラウザ側から判断できない。"""
    while True:
        if serial_ok.is_set():
            payload = json.dumps({
                "type": "Heartbeat",
                "timestamp": datetime.datetime.now().isoformat(),
            }, ensure_ascii=False)
            try:
                qzss_sink.send(payload)
            except Exception as e:
                print("⚠️ ハートビート送信に失敗しました:", e)
        time.sleep(HEARTBEAT_INTERVAL_SEC)


# 平常時(実際の災害が起きていない時)でも、パイプライン全体
# (受信機→デコード→クラウド→ブラウザ)がちゃんと生きているかを
# その場で確認できるよう、ターミナルでEnterキーを押すとテスト通報を
# 送信できるようにする。1回目はテスト通報、2回目は取消(終了)信号、と
# 交互に送信する(表示され続けるか、取消でちゃんと消えるかの両方を確認できる)。
TEST_SENTENCE = '$QZQSM,58,9AAF899C80000324000039000548C5E2C000000003DFF8001C000012FE4B0FC*7F'


def send_test_signal_loop():
    print("💡 動作確認したい時は、このターミナルでEnterキーを押してください")
    print("   (1回目: テスト通報を送信 → 2回目: 取消(終了)信号を送信、を繰り返します)")
    is_active = False
    while True:
        try:
            input()
        except EOFError:
            return
        payload, important = decode_to_json(TEST_SENTENCE)
        data = json.loads(payload)
        data["is_test_data"] = True
        if not is_active:
            qzss_sink.send(json.dumps(data, ensure_ascii=False))
            print("🧪 テスト通報(緊急地震速報のサンプル)を送信しました。地図に反映されるか確認してください")
            is_active = True
        else:
            data["information_type"] = "取消"
            data["information_type_en"] = "Cancel"
            data["information_type_no"] = 2
            qzss_sink.send(json.dumps(data, ensure_ascii=False))
            print("🛑 取消(終了)信号を送信しました。表示が消えるか確認してください")
            is_active = False


VAL_SET_RAM_UBX_RXM_SFRBX_UART1_ON = bytes([0xB5, 0x62, 0x06, 0x8A, 0x09, 0x00, 0x01, 0x01, 0x00, 0x00, 0x32, 0x02, 0x91, 0x20, 0x01, 0x81, 0x30])

satellite_id = {
    # PRNの下位6bitを衛星番号文字列に対応させる。名称はL1S公式PRN割当に準拠
    # (185=QZS-4/4号機, 189=QZS-3/3号機。DCR同人誌の表は185↔189が逆なので注意)
    184: '56', # QZS-2  (2号機)
    185: '57', # QZS-4  (4号機)
    189: '61', # QZS-3  (3号機)
    183: '55', # QZS-1  (初号機・運用終了済み)
    186: '58', # QZS-1R (初号機後継機)
}

def nmea_checksum(sentence):
    data = sentence.strip("$").split('*', 1)[0]
    cksum = reduce(operator.xor, (ord(s) for s in data), 0)
    return cksum

def ubx_checksum(message):
    ck_a = 0
    ck_b = 0
    i = 0
    while i < len(message):
        ck_a = (ck_a + message[i]) & 0xff
        ck_b = (ck_b + ck_a) & 0xff
        i += 1
    return ck_a, ck_b

def ubx2qzqsm(line):
    if line[:7] == b'\xB5\x62\x02\x13\x2C\x00\x05': # UBX-RXM-SFRBX, 44 bytes, QZSS
        satId = satellite_id[line[7] + 182] # PRN -> Satellite ID
        data = b''
        for i in range(9):
            data += bytes((line[14+3+i*4], line[14+2+i*4], line[14+1+i*4], line[14+0+i*4]))
        if data[1] >> 2 == 43 or data[1] >> 2 == 44: # Message Type 43=JMA-DC Report, 44=Other
            dcr_message = (data[:31] + bytes((data[31] & 0xC0,))).hex()[:-1] # 256-4=252 bit
            sentence = '$QZQSM,' + satId + ',' + dcr_message + '*'
            return sentence + format(nmea_checksum(sentence), 'x')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Print QZQSM NMEA format sentence')  
    parser.add_argument('port', help='serial port. ex: /dev/ttyUSB0')
    parser.add_argument('baudrate', help='baudrate. ex: 115200')
    parser.add_argument('-n', '--nmea', help='print other standard NMEA sentence', action='store_true')
    args = parser.parse_args()

    threading.Thread(target=send_heartbeat_loop, daemon=True).start()
    threading.Thread(target=send_test_signal_loop, daemon=True).start()

    RECONNECT_WAIT_SEC = 5
    IDLE_TIMEOUT_SEC = 20  # これだけ何も受信しなければ切断とみなして再接続する

    # USBの抜き差し等でシリアル接続が切れてもプロセスごと終了させず、
    # ポートが復帰し次第自動で再接続する(受信機のオンライン/オフライン表示は
    # ハートビートの有無で判断されるので、ここで復帰できればそちらも自動で戻る)。
    # OS/ドライバによっては切断時に例外を出さず、読み取りが無音のまま
    # 固まることがある(Windowsで確認)ため、read()にタイムアウトを設け、
    # 一定時間データが来なければ強制的に再接続扱いにする。
    while True:
        try:
            with serial.Serial(args.port, args.baudrate, timeout=1) as ser:
                print('初期化中')
                ser.write(VAL_SET_RAM_UBX_RXM_SFRBX_UART1_ON) # UBX-RXM-SFRBX Output ON
                time.sleep(1)
                print('start!')
                serial_ok.set()
                last_byte_time = time.time()

                while True:
                    line = b''
                    nmea_flag = False
                    ubx_flag = False
                    count = 0
                    payload_length = 0
                    while True:
                        if ubx_flag:
                            if count > 4 and payload_length == 0:
                                payload_length = int.from_bytes(line[4:5], "little")
                            if payload_length > 0 and count == payload_length + 8: # header 6 bytes + checksum 2 bytes
                                break
                        b = ser.read()
                        if not b:
                            # timeout=1 による空読み(データが来ていないだけ)
                            if time.time() - last_byte_time > IDLE_TIMEOUT_SEC:
                                print(f"🔴 オフライン({IDLE_TIMEOUT_SEC}秒間データを受信していません)")
                                raise serial.SerialException(
                                    f"{IDLE_TIMEOUT_SEC}秒間データを受信していません(切断の可能性)")
                            continue
                        last_byte_time = time.time()
                        if b == b'$' and not ubx_flag:
                            nmea_flag = True
                        if b == b'\x62' and line == b'\xB5':
                            ubx_flag = True
                        if b == b'\n':
                            if line.endswith(b'\r'):
                                line += b
                                break
                            else:
                                line += b
                        else:
                            line += b
                        count += 1

                    if args.nmea and nmea_flag:
                        try:
                            sentence = line.decode().strip('\r\n')
                            ck = nmea_checksum(sentence)
                            if format(ck, 'x') == sentence.split('*', 1)[1]:
                                print(sentence)
                        except (UnicodeDecodeError, IndexError):
                            # バイナリ(UBX)データの中の'$'に偶然反応しただけの
                            # ノイズなので、無視して読み取りを続ける
                            pass

                    if ubx_flag:
                        ck_a, ck_b = ubx_checksum(line[2:payload_length+6])
                        if line[-2] == ck_a and line[-1] == ck_b:
                            sentence = ubx2qzqsm(line)
                            if sentence is not None:
                                print(sentence)
                                payload, important = decode_to_json(sentence)
                                if not important:
                                    print("重要度低のため送信スキップ:", payload)
                                else:
                                    # raw(プリアンブル/CRC/衛星IDを含まない本体)で重複判定する。
                                    try:
                                        dedup_key = json.loads(payload).get("raw")
                                    except (ValueError, TypeError):
                                        dedup_key = None
                                    dedup_key = dedup_key or sentence
                                    if dedup_key in recent_content_keys:
                                        print("前回と同一内容のため送信スキップ")
                                    else:
                                        recent_content_keys.append(dedup_key)
                                        qzss_sink.send(payload)
                                        print("送信:", payload)
        except (serial.SerialException, OSError) as e:
            serial_ok.clear()
            print(f"⚠️ シリアル接続が切れました({e})。{RECONNECT_WAIT_SEC}秒後に再接続を試みます...")
            time.sleep(RECONNECT_WAIT_SEC)
