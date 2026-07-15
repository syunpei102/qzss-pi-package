"""
1つの受信機(1本のアンテナ、1つのシリアルポート)から受信・デコードした
災危通報(地震・津波・Jアラート・Lアラート・気象警報など)を、1つに
統合された地図サービス(重要地図+注意情報を1画面に表示する版)へ送信する。

以前は「重要情報」「注意情報」を別々のCloud Runサービスに振り分けて
いたが、地図側を1つのアプリ・1つのパネルに統合したため、送信先も
1つのURL/トークンに統一した。

使い方:
  QZSS_CLOUD_URL=https://xxxx.a.run.app/ingest \
  QZSS_INGEST_TOKEN=xxxxxxxx \
  python3 read_legacy_dual.py <シリアルポート> <ボーレート>

  ローカル(ラズパイ上のkiosk表示)向けにはCloud Runの代わりに
  http://localhost:8080/ingest のようなURLを指定すればよい。
"""
import argparse
import base64
import datetime
import http.client
import json
import operator
import os
import queue
import random
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from functools import reduce

import azarashi
import serial

# 地震・津波・南海トラフ・火山・降灰・気象警報・洪水を送信する。
# 海上警報(14)・北西太平洋津波(6)は地図描画できる地域データが無いため対象外。
# 台風(12)は消滅時に取消(取り下げ)信号が無く、表示が消えないまま残り続ける
# 問題があるため対象外(再度有効にする場合は12を追加する)。
ALLOWED_CATEGORY_NOS = {1, 2, 3, 4, 5, 8, 9, 10, 11}

# JMAの災危通報は同一内容が配信終了条件を満たすまで数秒おきに再送され続ける仕様の
# ため(同じ通報がそのまま繰り返し届く)、直近に送信済みの内容と完全一致する場合は
# クラウドへの再送信をスキップする。
# 判定にはデコード結果の raw(DCRメッセージ本体)を使う。プリアンブル(A/B/C)は
# 送信ごとに巡回し、sentence / message / nmea は内容が同じでも毎回変わってしまうが、
# raw はプリアンブル・CRC・衛星IDを含まないため、内容が同じなら常に一致する。
RECENT_CONTENT_HISTORY_SIZE = 50
recent_content_keys = deque(maxlen=RECENT_CONTENT_HISTORY_SIZE)

CLOUD_URL = os.environ.get("QZSS_CLOUD_URL", "").strip()
TOKEN = os.environ.get("QZSS_INGEST_TOKEN", "").strip()
DEVICE_ID = os.environ.get("QZSS_DEVICE_ID", "").strip() or socket.gethostname()

HEARTBEAT_INTERVAL_SEC = 30
serial_ok = threading.Event()

# ==================================================
# 拠点(このラズパイ)に割り当てられた地域設定の取得
#
# 管理サイトで拠点に都道府県を割り当てると、その周辺地方まで展開された
# 都道府県IDリストを /device-region/{DEVICE_ID} から取得できる。割り当て
# られている場合、対象外の都道府県だけの通報はデコード後すぐに処理を
# 打ち切る(「送信しない」のではなく「それ以上処理しない」)。
#
# allowed_prefecture_ids は None のときは絞り込みなし(全国対象、既定)。
# 取得に失敗した場合は直前のキャッシュ、キャッシュも無ければ絞り込み
# なしにフェイルオープンする(通信不調で通報が無言のまま消える事故を
# 防ぐため)。
# ==================================================
REGION_REFRESH_INTERVAL_SEC = 10 * 60
REGION_CACHE_PATH = os.path.expanduser("~/.qzss_region_cache.json")
allowed_prefecture_ids = None  # set[int] | None


def _cloud_base_url():
    return CLOUD_URL[: -len("/ingest")] if CLOUD_URL.endswith("/ingest") else CLOUD_URL


def _load_region_cache():
    global allowed_prefecture_ids
    try:
        with open(REGION_CACHE_PATH, "r", encoding="utf-8") as f:
            ids = json.load(f)
        if isinstance(ids, list) and ids:
            allowed_prefecture_ids = set(ids)
    except (OSError, ValueError):
        pass


def _save_region_cache(ids):
    try:
        with open(REGION_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(ids) if ids else None, f)
    except OSError:
        pass


def _fetch_region_config_once():
    global allowed_prefecture_ids
    url = f"{_cloud_base_url()}/device-region/{urllib.parse.quote(DEVICE_ID, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = data.get("prefectureIds")
        if ids:
            allowed_prefecture_ids = set(ids)
            _save_region_cache(ids)
        else:
            allowed_prefecture_ids = None
            _save_region_cache(None)
    except Exception as e:
        print(f"⚠️ 拠点の地域設定の取得に失敗しました(直前のキャッシュのまま続行): {e}")


def region_config_refresh_loop():
    _load_region_cache()
    while True:
        _fetch_region_config_once()
        time.sleep(REGION_REFRESH_INTERVAL_SEC)


def is_in_scope(params):
    """拠点に地域が割り当てられている場合、対象外の都道府県だけの通報を
    除外する。対象都道府県が判別できない通報(震源のみ・津波・Jアラート等)
    は現行通り常に処理する。"""
    if allowed_prefecture_ids is None:
        return True
    prefs = params.get("prefectures_raw")
    if not prefs:
        return True
    return any(p in allowed_prefecture_ids for p in prefs)

# 直近に受信できた通報がどの衛星からのものだったかを覚えておき、ハートビートに
# 乗せて送る。個々の災危通報は特定カテゴリ(重要/注意情報)しかクラウドへ送らない
# ため、それだけでは「今どの号機から受信できているか」が分かりにくい。
# ハートビートは受信さえできていれば常に一定間隔で送るので、こちらに載せる
# ことで死活監視の画面と同時にリアルタイムに近い形で表示できる。
last_satellite_seen = {"satellite_id": None, "satellite_prn": None}


def note_satellite_seen(params):
    sat_id = params.get("satellite_id")
    if sat_id is not None:
        last_satellite_seen["satellite_id"] = sat_id
        last_satellite_seen["satellite_prn"] = params.get("satellite_prn")


def _jsonify(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return base64.b16encode(bytes(value)).decode("ascii").lower()
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # DCXレポートのcamfなど、内部状態のみの非公開オブジェクトは文字列化する
    return str(value)


def decode_full(sentence):
    """azarashiでデコードし、(パラメータdict, 分類キー) を返す。
    分類キーは 'jalert' / カテゴリ番号(int) / None(デコード失敗)。"""
    try:
        report = azarashi.decode(sentence, msg_type="nmea")
    except Exception as e:
        return {"type": "DecodeError", "sentence": sentence, "error": str(e)}, None

    params = {k: _jsonify(v) for k, v in report.get_params().items()}
    params["type"] = type(report).__name__
    params["description"] = str(report)
    if params.get("dcx_message_type") == "J-Alert":
        return params, "jalert"
    if params.get("dcx_message_type") == "L-Alert":
        # ex1(市区町村コード)の生の数値はget_params()には含まれないが、
        # 都道府県への対応付け(コード÷1000の整数部=JIS都道府県コード=
        # prefectures.geojsonのidと同じ)に使うため、report内部のcamfから
        # 直接拾って追加する(市区町村コードが実際に使われている場合のみ)
        if params.get("ignore_ex1") is False:
            camf = getattr(report, "camf", None)
            ex1 = getattr(camf, "ex1", None) if camf is not None else None
            if ex1 is not None:
                params["ex1_target_area_code_raw"] = ex1
        return params, "lalert"
    return params, params.get("disaster_category_no")


# ==================================================
# サーバーへの送信(非同期・keep-alive)
#
# 実測(https://eq.shum10.com/ingestへの本番相当の送信):
#   - デコード+JSON変換: 0.1ms未満(ボトルネックではない)
#   - HTTP送信: 接続を毎回新規に張る場合は平均150ms前後、
#     TCP/TLS接続を使い回す(keep-alive)場合は平均90ms前後
#     (TLSハンドシェイクの分だけ確実に速くなる)
#
# 以前は受信ループの中で urllib.request.urlopen() を直接呼んでおり、
# 1件送るたびに新規TCP/TLS接続を張った上に、送信が終わるまで次の
# シリアルバイトの読み取りが止まっていた(=通報が連続して届くと
# 後続の受信が遅延・最悪データ落ちのリスクがあった)。
# 送信専用のキュー+ワーカースレッドに分離し、受信ループは
# キューへ積むだけ(ほぼ一瞬)で次のバイトの読み取りに戻れるようにする。
# ==================================================
send_queue = queue.Queue()


class Sender:
    """1つの宛先に対して、TCP/TLS接続を使い回しながら送信するクラス。
    切断されていたら次回送信時に自動で張り直す。"""

    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path or "/"
        self.token = token
        self.conn = None

    def _connect(self):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        self.conn = cls(self.host, self.port, timeout=5)

    def send(self, payload_dict):
        """送信を1回試みる。成功したらTrue、失敗したらFalseを返す
        (例外を投げない。呼び出し側でキューへの再投入を判断するため)。"""
        data = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Api-Key": self.token}
        # 既存の接続が(サーバー側のタイムアウト等で)切れていることがあるため、
        # 失敗したら1回だけ接続を張り直してリトライする
        for attempt in range(2):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("POST", self.path, body=data, headers=headers)
                resp = self.conn.getresponse()
                resp.read()
                return True
            except Exception as e:
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                if attempt == 1:
                    print("⚠️ 送信に失敗しました:", self.host, e)
        return False


# 送信に失敗した通報は、ネットワークが一時的に不安定なだけの可能性が
# あるため、少し待ってからキューに戻して再送する(自動リトライ)。
# 災危通報(実際の警報)は取りこぼしたくないので複数回リトライするが、
# ハートビートは30秒おきに次が来るので古い1件に固執する意味が薄く、
# リトライ自体を行わない(キューが詰まって本来の通報の送信が遅れるのを防ぐ)。
MAX_SEND_RETRIES = 5
RETRY_BACKOFF_SEC = 3


def _sender_worker_loop():
    sender = Sender(CLOUD_URL, TOKEN)
    while True:
        payload, attempt, retryable = send_queue.get()
        ok = sender.send(payload)
        if not ok and retryable:
            if attempt < MAX_SEND_RETRIES:
                print(f"↻ 送信失敗、{RETRY_BACKOFF_SEC}秒後に再試行します"
                      f"({attempt + 1}/{MAX_SEND_RETRIES}): {payload.get('type')}")
                time.sleep(RETRY_BACKOFF_SEC)
                send_queue.put((payload, attempt + 1, retryable))
            else:
                print(f"❌ 送信を諦めました(再試行回数上限): {payload.get('type')}")
        send_queue.task_done()


def enqueue_send(payload, retryable=True):
    send_queue.put((payload, 0, retryable))


def route_report(params, category_key, is_test_data=False):
    if is_test_data:
        params = dict(params)
        params["is_test_data"] = True

    if category_key in ("jalert", "lalert") or category_key in ALLOWED_CATEGORY_NOS:
        enqueue_send(params, retryable=True)
        print("🛰️ 地図へ送信キューに追加:", params.get("type"))
    else:
        print("(対象外カテゴリのため送信スキップ)")


def send_heartbeat_loop():
    while True:
        if serial_ok.is_set():
            payload = {
                "type": "Heartbeat",
                "timestamp": datetime.datetime.now().isoformat(),
                "satellite_id": last_satellite_seen["satellite_id"],
                "satellite_prn": last_satellite_seen["satellite_prn"],
            }
            # ハートビートは30秒おきに次が来るので、古い1件のために
            # リトライして詰まらせる必要はない(失敗したら諦めて次を待つ)
            enqueue_send(payload, retryable=False)
        time.sleep(HEARTBEAT_INTERVAL_SEC)


TEST_SENTENCE_CRITICAL = '$QZQSM,58,9AAF899C80000324000039000548C5E2C000000003DFF8001C000012FE4B0FC*7F'
TEST_SENTENCE_CAUTION = '$QZQSM,61,c6ade3a99900031803006024007b700eb400f64a1e00000000000013ede5034*70'

# 全国いろいろな地域・組み合わせでテストできるよう、地域プールから
# 「出る地域」も「同時に出る個数」も毎回完全ランダムに選ぶ
# (地域コード・名称は実際のweather_regions.geojsonに存在するものを使用)
WEATHER_TEST_REGION_POOL = [
    (11000, "宗谷地方"), (12010, "上川地方"),
    (20010, "津軽"), (30010, "内陸"),
    (130010, "東京地方"), (140010, "東部"),
    (190010, "中・西部"), (200010, "北部"),
    (270000, "大阪府"), (260010, "南部"),
    (400010, "福岡地方"), (430010, "熊本地方"),
    (471010, "本島中南部"), (471020, "本島北部"),
]
# 気象(Dc=10)で実際に配信される災害副種別は次の11種類のみ
# (IS-QZSS-DCR仕様 Table35 / azarashi の
#  qzss_dcr_jma_weather_related_disaster_sub_category と一致)。
# 通常レベルの警報・注意報(大雨警報・強風注意報 等)は配信されないため含めない。
WEATHER_TEST_SUB_CATEGORIES = [
    (1, "暴風雪特別警報"),
    (2, "大雨特別警報"),
    (3, "暴風特別警報"),
    (4, "大雪特別警報"),
    (5, "波浪特別警報"),
    (6, "高潮特別警報"),
    (7, "全ての気象特別警報"),
    (21, "記録的短時間大雨情報"),
    (22, "竜巻注意情報"),
    (23, "土砂災害警戒情報"),
    (31, "その他の警報等情報要素"),
]


def random_weather_test_payload():
    count = random.randint(1, min(5, len(WEATHER_TEST_REGION_POOL)))
    chosen = random.sample(WEATHER_TEST_REGION_POOL, count)
    codes = [c for c, _ in chosen]
    names = [n for _, n in chosen]
    sub = [random.choice(WEATHER_TEST_SUB_CATEGORIES) for _ in chosen]
    sub_raw = [c for c, _ in sub]
    sub_names = [n for _, n in sub]
    return {
        "type": "QzssDcReportJmaWeather",
        "disaster_category": "気象",
        "disaster_category_no": 10,
        "information_type": "発表",
        "information_type_no": 0,
        "weather_warning_state": "発表",
        "weather_forecast_regions_raw": codes,
        "weather_forecast_regions": names,
        "weather_related_disaster_sub_categories": sub_names,
        "weather_related_disaster_sub_categories_raw": sub_raw,
        "description": f"テスト: {'・'.join(names)}に気象警報が発表されました",
        "satellite_id": 57,
        "satellite_prn": 185,
    }


def send_test_signal_loop():
    print("💡 動作確認コマンド(このターミナルに入力してEnter):")
    print("   何も入力せずEnter = 重要情報のテスト送信/取消を交互に送信")
    print("   c + Enter          = 注意情報(台風+気象警報)のテスト送信/終了信号を交互に送信")
    critical_active = False
    caution_active = False
    last_weather_payload = None
    while True:
        try:
            line = input()
        except EOFError:
            return

        if line.strip().lower() == 'c':
            if not caution_active:
                params, key = decode_full(TEST_SENTENCE_CAUTION)
                route_report(params, key, is_test_data=True)
                print("🧪 注意情報のテスト通報(台風)を送信しました")
                last_weather_payload = random_weather_test_payload()
                route_report(dict(last_weather_payload), 10, is_test_data=True)
                print(f"🧪 注意情報のテスト通報(気象警報: {'・'.join(last_weather_payload['weather_forecast_regions'])})を送信しました")
                caution_active = True
            else:
                params, key = decode_full(TEST_SENTENCE_CAUTION)
                params["information_type"] = "取消"
                params["information_type_en"] = "Cancel"
                params["information_type_no"] = 2
                route_report(params, key, is_test_data=True)
                print("🛑 取消(終了)信号を送信しました(台風)")
                weather_end = dict(last_weather_payload) if last_weather_payload else random_weather_test_payload()
                weather_end["information_type"] = "取消"
                weather_end["information_type_en"] = "Cancel"
                weather_end["information_type_no"] = 2
                route_report(weather_end, 10, is_test_data=True)
                print("🛑 取消(終了)信号を送信しました(気象警報)")
                caution_active = False
                last_weather_payload = None
            continue

        if not critical_active:
            params, key = decode_full(TEST_SENTENCE_CRITICAL)
            route_report(params, key, is_test_data=True)
            print("🧪 テスト通報(重要情報)を送信しました")
            critical_active = True
        else:
            params, key = decode_full(TEST_SENTENCE_CRITICAL)
            params["information_type"] = "取消"
            params["information_type_en"] = "Cancel"
            params["information_type_no"] = 2
            route_report(params, key, is_test_data=True)
            print("🛑 取消(終了)信号を送信しました(重要情報側)")
            critical_active = False


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
    if line[:7] == b'\xB5\x62\x02\x13\x2C\x00\x05':  # UBX-RXM-SFRBX, 44 bytes, QZSS
        satId = satellite_id[line[7] + 182]  # PRN -> Satellite ID
        data = b''
        for i in range(9):
            data += bytes((line[14+3+i*4], line[14+2+i*4], line[14+1+i*4], line[14+0+i*4]))
        if data[1] >> 2 == 43 or data[1] >> 2 == 44:  # Message Type 43=JMA-DC Report, 44=Other
            dcr_message = (data[:31] + bytes((data[31] & 0xC0,))).hex()[:-1]  # 256-4=252 bit
            sentence = '$QZQSM,' + satId + ',' + dcr_message + '*'
            return sentence + format(nmea_checksum(sentence), 'x')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='QZSS受信機からのデータをデコードして統合地図サービスへ送信する')
    parser.add_argument('port', help='serial port. ex: /dev/tty.usbserial-XXXX')
    parser.add_argument('baudrate', help='baudrate. ex: 9600')
    parser.add_argument('-n', '--nmea', help='print other standard NMEA sentence', action='store_true')
    args = parser.parse_args()

    if not CLOUD_URL:
        print("❌ QZSS_CLOUD_URL が未設定です。送信先(Cloud RunのURL、またはローカルのhttp://localhost:PORT/ingest)を設定してください。")
        raise SystemExit(1)

    threading.Thread(target=_sender_worker_loop, daemon=True).start()
    threading.Thread(target=send_heartbeat_loop, daemon=True).start()
    threading.Thread(target=send_test_signal_loop, daemon=True).start()
    threading.Thread(target=region_config_refresh_loop, daemon=True).start()

    RECONNECT_WAIT_SEC = 5
    IDLE_TIMEOUT_SEC = 20

    while True:
        try:
            with serial.Serial(args.port, args.baudrate, timeout=1) as ser:
                print('初期化中')
                ser.write(VAL_SET_RAM_UBX_RXM_SFRBX_UART1_ON)
                time.sleep(1)
                print('start!')
                serial_ok.set()
                last_byte_time = time.time()

                while True:
                    # bytesの += は毎回新しいオブジェクトを作り直す(O(n))ため、
                    # bytearrayにして.extend()相当のin-place追記(償却O(1))にする。
                    # 1メッセージ分(数十バイト程度)なので体感できる差ではないが、
                    # 積み重なるバイト単位ループの無駄を削る意味で変更する。
                    line = bytearray()
                    nmea_flag = False
                    ubx_flag = False
                    count = 0
                    payload_length = 0
                    while True:
                        if ubx_flag:
                            if count > 4 and payload_length == 0:
                                payload_length = int.from_bytes(line[4:5], "little")
                            if payload_length > 0 and count == payload_length + 8:
                                break
                        b = ser.read()
                        if not b:
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
                            pass

                    if ubx_flag:
                        ck_a, ck_b = ubx_checksum(line[2:payload_length+6])
                        if line[-2] == ck_a and line[-1] == ck_b:
                            sentence = ubx2qzqsm(line)
                            if sentence is not None:
                                print(sentence)
                                params, key = decode_full(sentence)
                                note_satellite_seen(params)
                                # 拠点に地域が割り当てられていて、かつこの通報が対象都道府県
                                # 以外だけを対象にしている場合、ここで即座に処理を打ち切る
                                # (「送信しない」のではなく、重複排除の登録も含めて
                                # 「それ以上処理しない」)。デコード自体はどの通報が対象かを
                                # 判定するために避けられないが、それ以降は一切行わない。
                                if not is_in_scope(params):
                                    print("(拠点の対象地域外のため処理をスキップ)")
                                    continue
                                # raw(プリアンブル・CRC・衛星IDを含まない本体)で重複判定する。
                                # sentence はプリアンブルが送信ごとに巡回して毎回変わるため使えない。
                                dedup_key = params.get("raw") or sentence
                                if dedup_key in recent_content_keys:
                                    print("(前回と同一内容のため送信スキップ)")
                                else:
                                    recent_content_keys.append(dedup_key)
                                    route_report(params, key)
        except (serial.SerialException, OSError) as e:
            serial_ok.clear()
            print(f"⚠️ シリアル接続が切れました({e})。{RECONNECT_WAIT_SEC}秒後に再接続を試みます...")
            time.sleep(RECONNECT_WAIT_SEC)
