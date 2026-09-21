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

  ローカル(ラズパイ上のkiosk表示)のみで完結させたい場合は、
  QZSS_CLOUD_URLの代わりにhttp://localhost:8080/ingestのようなURLを
  指定すればよい。

  クラウド(Discordでの遠隔操作・公開マップ配信)を維持したまま、
  ラズパイ本体のkiosk表示にも同時送信したい場合は、QZSS_CLOUD_URLは
  そのままに、QZSS_LOCAL_URL=http://localhost:8080/ingest を追加で
  指定する(クラウドへの送信とは完全に別スレッドで動くため、
  ローカル送信が詰まってもクラウド側の速度には影響しない)。
"""
import argparse
import base64
import datetime
import http.client
import json
import operator
import os
import random
import socket
import threading
import time
import urllib.parse
import urllib.request
from functools import reduce

import azarashi
import serial

# 地震・津波・南海トラフ・火山・降灰・気象警報・洪水を送信する。
# 海上警報(14)・北西太平洋津波(6)は地図描画できる地域データが無いため対象外。
# 台風(12)は消滅時に取消(取り下げ)信号が無く、表示が消えないまま残り続ける
# 問題があるため対象外(再度有効にする場合は12を追加する)。
ALLOWED_CATEGORY_NOS = {1, 2, 3, 4, 5, 6, 8, 9, 10, 11}  # 6=北西太平洋津波(津波情報)

# JMAの災危通報は同一内容が配信終了条件を満たすまで数秒おきに再送され続ける仕様の
# ため(同じ通報がそのまま繰り返し届く)、直近に送信済みの内容と完全一致する場合は
# クラウドへの再送信をスキップする。
# 判定にはデコード結果の raw(DCRメッセージ本体)を使う。プリアンブル(A/B/C)は
# 送信ごとに巡回し、sentence / message / nmea は内容が同じでも毎回変わってしまうが、
# raw はプリアンブル・CRC・衛星IDを含まないため、内容が同じなら常に一致する…はずだった。
# 実機でL-Alert訓練放送を確認したところ、rawの中身自体が再送のたびに変わっており
# (おそらく仕様上のシーケンス番号等が埋め込まれている)、このバイト一致の判定を
# すり抜けて数分おきに「新規」として送信され続けていた。地図側(map/server.js の
# reportGroupKey、main.js の findMatchingGroup)には既に「災害種別+対象地域」による
# 意味的な重複統合を実装済みだが、受信機側から見ても無駄な送信・ログの積み重ねに
# なるため、同じ考え方の意味的キーで送信自体を間引く
RECENT_CONTENT_HISTORY_SIZE = 50
recent_content_keys = {}  # key -> (content, monotonic time)

# 意味的に同一とみなせる通報の再送を、SEMANTIC_DEDUP_WINDOW_SEC以内なら
# スキップする(それを過ぎたら「本当にまだ続いている」ことの確認も兼ねて
# 送り直す。地図側のTTL安全策と極端にズレないよう、5分程度に留める)
SEMANTIC_DEDUP_WINDOW_SEC = 5 * 60



def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def semantic_dedup_key(params):
    """map/server.js の reportGroupKey と同じ考え方: 災害種別+対象地域等で
    「同じ通報の再送」を意味的に判定する(rawが再送ごとに変わる通報にも効く)。
    判定できない場合はNoneを返し、呼び出し側はバイト一致の判定だけに頼る。"""
    report_type = params.get("type")
    if report_type in ("QzssDcxLAlert", "QzssDcxMTInfo"):
        hazard = params.get("a4_hazard_type") or ""
        area = params.get("ex1_target_area_code_raw")
        if area is not None:
            return f"lalert|{hazard}|ex1:{area}"
        # 緯度・経度が両方そろっている場合だけ楕円の中心を使う(片側だけの通報で
        # 例外になったり，経度0扱いで別地点と混同したりしない)
        lat = params.get("a12_ellipse_centre_latitude")
        lon = params.get("a13_ellipse_centre_longitude")
        if _is_number(lat) and _is_number(lon):
            return f"lalert|{hazard}|ellipse:{lat:.2f},{lon:.2f}"
        return None
    if report_type == "QzssDcxJAlert":
        hazard = params.get("a4_hazard_type") or ""
        areas = ",".join(sorted(params.get("ex9_target_area_list_ja") or []))
        return f"jalert|{hazard}|{areas}"
    return None


def is_recent_duplicate(params, sentence, now=None):
    """同じ対象の最後の内容だけを期限付きで比較する．解除後の再発表も通す．"""
    now = time.monotonic() if now is None else now
    group = semantic_dedup_key(params)
    if group is not None:
        # 電文表現・受信時刻・衛星の違いを除き，状態・深刻度・範囲・本文を比較する．
        volatile = {"raw", "message", "sentence", "nmea", "camf", "description",
                    "timestamp", "client_timestamps", "satellite_id", "satellite_prn"}
        content = json.dumps({k: v for k, v in params.items() if k not in volatile},
                             sort_keys=True, ensure_ascii=False, default=str)
        key = "semantic:" + group
    else:
        content = params.get("raw") or sentence
        key = "raw:" + content
    previous = recent_content_keys.get(key)
    if previous and previous[0] == content and now - previous[1] < SEMANTIC_DEDUP_WINDOW_SEC:
        return True
    recent_content_keys.pop(key, None)
    recent_content_keys[key] = (content, now)
    while len(recent_content_keys) > RECENT_CONTENT_HISTORY_SIZE:
        recent_content_keys.pop(next(iter(recent_content_keys)))
    return False

CLOUD_URL = os.environ.get("QZSS_CLOUD_URL", "").strip()
# 設定するとCLOUD_URLに加えてこちらへも同時送信する(例: ラズパイ本体で
# 地図をkiosk表示しつつ、Discordでの遠隔操作・公開マップ配信のため
# クラウドへの送信も維持したい場合、http://localhost:8080/ingest を指定する)
LOCAL_URL = os.environ.get("QZSS_LOCAL_URL", "").strip()
TOKEN = os.environ.get("QZSS_INGEST_TOKEN", "").strip()
DEVICE_ID = os.environ.get("QZSS_DEVICE_ID", "").strip() or socket.gethostname()
# デフォルトでは無効(ラズパイ本番のログ出力量を変えないため)。手元での
# 動作確認用(map_macbook等)にデコード結果(params)の中身を丸ごと
# ターミナルに流したい場合だけ QZSS_VERBOSE_DECODE=1 を設定する
VERBOSE_DECODE = os.environ.get("QZSS_VERBOSE_DECODE", "").strip() == "1"

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


# ==================================================
# ローカルkioskへの設定同期(訓練放送表示・Lアラート解析のON/OFF)
#
# Discordの操作はCloud Run(公開URL)の/discord/interactionsにしか届かず、
# ローカルkiosk(QZSS_LOCAL_URL)は完全に別インスタンスなので何も知らない。
# クラウドの/configを定期ポーリングし，変化があればローカルへ反映する。
# 以前は訓練放送とLアラートで別々のループが同じ/configを2分ごとに二重に
# 取得していた(Cloud Runへの無駄なリクエスト)ため，1回の取得で両方を
# 処理する。QZSS_LOCAL_URLが未設定(=ローカルkioskを併用していない)なら
# 何もしない
# ==================================================
LOCAL_CONFIG_REFRESH_INTERVAL_SEC = 2 * 60
# 設定名 -> (/configのキー, ローカル同期先パス, 既定値, 表示名)
LOCAL_SYNC_SETTINGS = {
    "training": ("showTrainingBroadcasts", "/local-sync/training-broadcasts", True, "訓練放送表示設定"),
    "lalert": ("lalertEnabled", "/local-sync/lalert", True, "Lアラート表示設定"),
}
last_known_local_settings = {name: None for name in LOCAL_SYNC_SETTINGS}  # None=未取得


def _local_base_url():
    return LOCAL_URL[: -len("/ingest")] if LOCAL_URL.endswith("/ingest") else LOCAL_URL


def _fetch_cloud_config():
    url = f"{_cloud_base_url()}/config?device={urllib.parse.quote(DEVICE_ID, safe='')}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_local_setting(path, enabled):
    req = urllib.request.Request(
        f"{_local_base_url()}{path}",
        data=json.dumps({"enabled": enabled}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def sync_local_settings_once(fetch=None, post=None):
    """/configを1回だけ取得し，変化した設定だけをローカルkioskへ反映する。"""
    fetch = fetch or _fetch_cloud_config
    post = post or _post_local_setting
    try:
        data = fetch()
    except Exception as e:
        print(f"⚠️ 設定の取得に失敗しました(次回また試します): {e}")
        return
    for name, (key, path, default, label) in LOCAL_SYNC_SETTINGS.items():
        enabled = bool(data.get(key, default))
        if enabled == last_known_local_settings[name]:
            continue
        try:
            post(path, enabled)
        except Exception as e:
            print(f"⚠️ {label}のローカル反映に失敗しました(次回また試します): {e}")
            continue
        last_known_local_settings[name] = enabled
        print(f"🔁 {label}をローカルkioskに反映しました: {enabled}")


def local_config_sync_loop():
    if not LOCAL_URL:
        return
    while True:
        sync_local_settings_once()
        time.sleep(LOCAL_CONFIG_REFRESH_INTERVAL_SEC)


def is_in_scope(params):
    """拠点に地域が割り当てられている場合、対象外の都道府県だけの通報を
    除外する。対象都道府県が判別できない通報(震源のみ・津波・Jアラート等)
    は現行通り常に処理する(誤って重要な情報をブロックするより、対象外の
    情報が多少混ざる方が安全、というfail-open方針)。
    以前は震度速報等が持つ prefectures_raw(都道府県IDそのもの)しか
    見ておらず、気象警報(weather_forecast_regions_raw)・降灰
    (local_governments_raw)は別のフィールド名・別のコード体系のため
    地域ロックが一切効いていなかった(実機で確認: 関東限定の拠点に鳥取県・
    富山県の気象警報が素通りしていた)。この2種別は都道府県IDへの変換方法が
    単純(コードの上位桁がJIS都道府県コードと一致)なので、同様にチェックする。
    EEW(eew_forecast_regions_raw、予報区コード)と洪水(flood_forecast_
    regions_raw、河川コード。1つの河川が複数県にまたがりうる)は単純な
    都道府県ID変換ができないため、従来通りfail-open(絞り込みなし)のまま。"""
    if allowed_prefecture_ids is None:
        return True
    prefs = params.get("prefectures_raw")
    if prefs:
        return any(p in allowed_prefecture_ids for p in prefs)
    # 気象警報の地域コードは6桁で、上位2桁(万の位から上)がJIS都道府県コード
    # (map/public/main.jsのregionDisplayNameと同じ導出方法: code // 10000)
    weather_codes = params.get("weather_forecast_regions_raw")
    if weather_codes:
        return any((c // 10000) in allowed_prefecture_ids for c in weather_codes)
    # 降灰の対象市区町村コードは7桁の全国地方公共団体コードで、上位2桁が
    # JIS都道府県コード(map/public/main.jsのbuildEventFromOtherCategoryと
    # 同じ導出方法: code // 100000)
    gov_codes = params.get("local_governments_raw")
    if gov_codes:
        return any((c // 100000) in allowed_prefecture_ids for c in gov_codes)
    return True

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
    # "L-Alert"(a3=1、消防庁経由の標準配信)と"Information from Local
    # Government"(a3=4、自治体からの直接配信。QzssDcxMTInfo)はフィールド
    # 構成が完全に同一(azarashi側でも両方ともQzssDcXtendedMessageBaseの
    # 単純なサブクラスで追加フィールドが無い)。当初は"L-Alert"だけを
    # 見ていたため、自治体が直接テスト配信した通報がここで弾かれ
    # (キーがNoneになりroute_reportの許可リストに乗らず送信スキップ)、
    # 実際に地域情報が入っているのに地図に何も描画されない不具合になって
    # いた(奈良県十津川村の実例で発覚)
    if params.get("dcx_message_type") in ("L-Alert", "Information from Local Government"):
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
# 送信キューは有界・優先度付きにする。以前は無制限のqueue.Queueで，送信失敗の
# たびにワーカーがsleepして待つ作りだったため，(1)通信断が長引くとメモリが
# 増え続ける，(2)再試行待ちの間に届いた新規の緊急通報が後ろに回される，という
# 問題があった。再試行は「not_before(次に送ってよい時刻)」付きでキューへ
# 戻すだけにし，ワーカーはsleepせず，送信可能な中で最も優先度の高いものを
# 常に先に取り出す。
PRIORITY_URGENT = 0     # 緊急通報(EEW・津波・震度・Jアラート・Lアラート等)
PRIORITY_NORMAL = 1     # その他の対象通報(気象・洪水・降灰等)
PRIORITY_HEARTBEAT = 2  # 死活監視(最新の1件だけ意味がある)
URGENT_CATEGORY_NOS = {1, 2, 3, 4, 5, 6, 8}
SEND_QUEUE_MAX = 200
LOCAL_QUEUE_MAX = 100


class QueuedItem:
    __slots__ = ("payload", "priority", "attempt", "retryable", "not_before", "seq")

    def __init__(self, payload, priority, attempt, retryable, not_before, seq):
        self.payload = payload
        self.priority = priority
        self.attempt = attempt
        self.retryable = retryable
        self.not_before = not_before
        self.seq = seq


class SendQueue:
    """有界の優先度付き送信キュー。満杯のときは最も優先度が低く古いものから
    捨てる(新規がそれより低優先なら新規を捨てる)。ハートビートは常に最新の
    1件だけを保持する。スレッドセーフ。"""

    def __init__(self, maxsize, clock=time.monotonic):
        self.maxsize = maxsize
        self.clock = clock
        self.dropped = 0
        self._items = []
        self._seq = 0
        self._cond = threading.Condition()

    def __len__(self):
        with self._cond:
            return len(self._items)

    def put(self, payload, priority=PRIORITY_NORMAL, attempt=0, retryable=True, not_before=0.0):
        """積めたらTrue，(満杯で低優先のため)捨てたらFalseを返す。"""
        with self._cond:
            if priority == PRIORITY_HEARTBEAT:
                self._items = [i for i in self._items if i.priority != PRIORITY_HEARTBEAT]
            if len(self._items) >= self.maxsize:
                victim = max(self._items, key=lambda i: (i.priority, -i.seq))
                if victim.priority < priority:
                    self.dropped += 1
                    return False
                self._items.remove(victim)
                self.dropped += 1
            self._seq += 1
            self._items.append(QueuedItem(payload, priority, attempt, retryable, not_before, self._seq))
            self._cond.notify()
            return True

    def get(self, timeout=None):
        """送信可能(not_beforeを過ぎた)な中で最も優先度が高く古いものを取り出す。
        無ければ，次に送信可能になるまで(または新規投入まで)待つ。timeout秒
        待っても無ければNone。"""
        deadline = None if timeout is None else self.clock() + timeout
        with self._cond:
            while True:
                now = self.clock()
                ready = [i for i in self._items if i.not_before <= now]
                if ready:
                    item = min(ready, key=lambda i: (i.priority, i.seq))
                    self._items.remove(item)
                    return item
                waits = [i.not_before - now for i in self._items]
                if deadline is not None:
                    waits.append(deadline - now)
                    if deadline - now <= 0:
                        return None
                self._cond.wait(min(waits) if waits else None)


send_queue = SendQueue(SEND_QUEUE_MAX)
# ローカル(ラズパイ内kiosk表示用)送信は、クラウド送信とは完全に別の
# キュー・スレッドにする。同じキュー/スレッドで直列に送ると、ローカル
# 送信が詰まったり遅延した場合にクラウドへの送信(緊急地震速報等)まで
# 遅れてしまうため
local_send_queue = SendQueue(LOCAL_QUEUE_MAX)


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
        self.last_status = None  # 直近のHTTPステータス(接続失敗ならNone)

    def _connect(self):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        self.conn = cls(self.host, self.port, timeout=5)

    def send(self, payload_dict):
        """送信を1回試みる。成功したらTrue、失敗したらFalseを返す
        (例外を投げない。呼び出し側でキューへの再投入を判断するため)。"""
        data = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Api-Key": self.token}
        self.last_status = None
        # 既存の接続が(サーバー側のタイムアウト等で)切れていることがあるため、
        # 使い回した接続で失敗したときだけ，1回接続を張り直してリトライする。
        # 新規接続で失敗したなら宛先自体が不通なので，タイムアウトを2倍待たない
        for attempt in range(2):
            reused = self.conn is not None
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("POST", self.path, body=data, headers=headers)
                resp = self.conn.getresponse()
                resp.read()
                self.last_status = resp.status
                if 200 <= resp.status < 300:
                    return True
                print(f"⚠️ HTTP送信失敗: {self.host} status={resp.status}")
                return False
            except Exception as e:
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                if attempt == 1 or not reused:
                    print("⚠️ 送信に失敗しました:", self.host, e)
                    return False
        return False


# 送信に失敗した通報は、ネットワークが一時的に不安定なだけの可能性が
# あるため、待ち時間を指数的に延ばしながらキューに戻して再送する(自動リトライ)。
# 災危通報(実際の警報)は取りこぼしたくないので複数回リトライするが、
# ハートビートは30秒おきに次が来るので古い1件に固執する意味が薄く、
# リトライ自体を行わない。認証エラー等の恒久的な4xxも再試行しない。
MAX_SEND_RETRIES = 5
RETRY_BACKOFF_SEC = 3
RETRY_BACKOFF_MAX_SEC = 60


def retry_delay(attempt):
    return min(RETRY_BACKOFF_SEC * (2 ** attempt), RETRY_BACKOFF_MAX_SEC)


def is_permanent_failure(status):
    return status is not None and 400 <= status < 500 and status not in (408, 429)


def process_send_item(sender, queue_, item, clock=time.monotonic):
    """1件送信し，失敗して再試行すべきなら待機せずにキューへ戻す。"""
    ok = sender.send(item.payload)
    if ok or not item.retryable:
        return ok
    if is_permanent_failure(sender.last_status):
        print(f"❌ 再試行しても成功しない応答のため諦めました(status={sender.last_status}): "
              f"{item.payload.get('type')}")
    elif item.attempt < MAX_SEND_RETRIES:
        delay = retry_delay(item.attempt)
        print(f"↻ 送信失敗、{delay}秒後以降に再試行します"
              f"({item.attempt + 1}/{MAX_SEND_RETRIES}): {item.payload.get('type')}")
        queue_.put(item.payload, item.priority, item.attempt + 1, item.retryable,
                   not_before=clock() + delay)
    else:
        print(f"❌ 送信を諦めました(再試行回数上限): {item.payload.get('type')}")
    return False


def _sender_worker_loop():
    sender = Sender(CLOUD_URL, TOKEN)
    while True:
        process_send_item(sender, send_queue, send_queue.get())


def _local_sender_worker_loop():
    """ラズパイ内のkiosk表示用地図アプリへの送信専用ループ。クラウド送信
    (_sender_worker_loop)とは別スレッド・別キューで動くため、こちらが
    詰まったり遅延してもクラウドへの送信速度には一切影響しない。
    同一機内なので基本的に失敗しない想定であり、リトライもしない
    (ベストエフォート)。"""
    local_sender = Sender(LOCAL_URL, "")
    while True:
        local_sender.send(local_send_queue.get().payload)


def report_priority(category_key):
    if category_key in ("jalert", "lalert") or category_key in URGENT_CATEGORY_NOS:
        return PRIORITY_URGENT
    return PRIORITY_NORMAL


def enqueue_send(payload, retryable=True, priority=PRIORITY_NORMAL):
    send_queue.put(payload, priority, 0, retryable)
    # put()はロック取得のみで一瞬で返る(送信そのものは別スレッドが行う)ため、
    # ここでLOCAL_URLへも積んでよい。クラウド側の速度には影響しない
    if LOCAL_URL:
        local_send_queue.put(payload, priority, 0, False)


def route_report(params, category_key, is_test_data=False, t0=None, t1=None):
    if is_test_data:
        params = dict(params)
        params["is_test_data"] = True

    if category_key in ("jalert", "lalert") or category_key in ALLOWED_CATEGORY_NOS:
        # レイテンシ計測用(T0受信・T1デコード完了)。以降サーバー側で
        # T2(受信)・T3(配信)、クライアント側でT4(描画完了)を追記し、
        # 各段階の所要時間をログで可視化する
        if t0 is not None and t1 is not None:
            params = dict(params)
            params["client_timestamps"] = {
                "t0_received_ms": int(t0 * 1000),
                "t1_decoded_ms": int(t1 * 1000),
            }
        enqueue_send(params, retryable=True, priority=report_priority(category_key))
        print("🛰️ 地図へ送信キューに追加:", params.get("type"))
    else:
        print("(対象外カテゴリのため送信スキップ)")


def send_heartbeat_loop():
    # 受信機(アンテナ)がまだシリアル接続できていなくても送る。この
    # ハートビートは「プログラム自体がクラウドに到達できているか」の
    # 指標であり、「受信機からデータが取れているか」とは別の関心事
    # なので、serial_okの状態に関わらず常時30秒おきに送る
    while True:
        payload = {
            "type": "Heartbeat",
            "timestamp": datetime.datetime.now().isoformat(),
            "satellite_id": last_satellite_seen["satellite_id"],
            "satellite_prn": last_satellite_seen["satellite_prn"],
            "serial_connected": serial_ok.is_set(),
        }
        # ハートビートは30秒おきに次が来るので、古い1件のために
        # リトライして詰まらせる必要はない(失敗したら諦めて次を待つ)
        enqueue_send(payload, retryable=False, priority=PRIORITY_HEARTBEAT)
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


def is_valid_nmea_sentence(sentence):
    """'$....*HH' 形式で，チェックサム(16進2桁・大文字小文字どちらも可)が一致するか。
    u-blox等は大文字，他の機器は小文字で出すため，文字列一致ではなく数値で比較する。"""
    sentence = sentence.strip()
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, given = sentence[1:].partition("*")
    if len(given) != 2:
        return False
    try:
        expected = int(given, 16)
    except ValueError:
        return False
    return nmea_checksum(body) == expected


def ubx_checksum(message):
    ck_a = 0
    ck_b = 0
    i = 0
    while i < len(message):
        ck_a = (ck_a + message[i]) & 0xff
        ck_b = (ck_b + ck_a) & 0xff
        i += 1
    return ck_a, ck_b


UBX_SYNC = b'\xb5\x62'
UBX_HEADER_LEN = 6            # sync(2) + class(1) + id(1) + length(2)
UBX_FRAME_OVERHEAD = 8        # ヘッダ6 + チェックサム2
UBX_MAX_PAYLOAD = 1024        # SFRBXは数十バイト。これを超える長さは不正(ノイズ)とみなす
NMEA_MAX_LINE = 256           # NMEAの規格上限は82文字


def ubx_payload_length(header):
    """UBXヘッダ(先頭6バイト以上)からペイロード長を返す。長さはリトルエンディアン2バイト。"""
    return int.from_bytes(header[4:6], "little")


class StreamFramer:
    """シリアルのバイト列から UBX フレームと NMEA 行を切り出す。
    不正な長さ・チェックサム不一致・途中欠落があっても，1バイトずつ読み捨てて
    次の同期ヘッダから再同期する(壊れたフレームに巻き込まれて後続の正常な
    フレームまで失わない)。フレームは ("ubx", bytes) / ("nmea", bytes)。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _next_frame(self):
        buf = self.buf
        while buf:
            first = buf[0]
            if first == 0xB5:
                if len(buf) < 2:
                    return None
                if buf[1] != 0x62:
                    del buf[0]
                    continue
                if len(buf) < UBX_HEADER_LEN:
                    return None
                payload = ubx_payload_length(buf)
                if payload > UBX_MAX_PAYLOAD:
                    del buf[0]
                    continue
                total = payload + UBX_FRAME_OVERHEAD
                if len(buf) < total:
                    return None
                frame = bytes(buf[:total])
                if (frame[-2], frame[-1]) == ubx_checksum(frame[2:-2]):
                    del buf[:total]
                    return ("ubx", frame)
                del buf[0]  # チェックサム不一致: 偽の同期ヘッダとみなして次から探し直す
                continue
            if first == 0x24:  # '$'
                newline = buf.find(b'\n')
                end = newline if newline != -1 else len(buf)
                # 行の途中にUBXの同期ヘッダがあれば，この'$'は誤検出。そこから再同期
                sync = buf.find(UBX_SYNC, 1, end)
                if sync != -1:
                    del buf[:sync]
                    continue
                if newline == -1:
                    if len(buf) > NMEA_MAX_LINE:
                        del buf[0]
                        continue
                    return None
                if newline + 1 > NMEA_MAX_LINE:
                    del buf[0]
                    continue
                line = bytes(buf[:newline + 1])
                del buf[:newline + 1]
                return ("nmea", line)
            del buf[0]
        return None


def ubx2qzqsm(line):
    # UBX-RXM-SFRBX(QZSS, 9ワード)。ペイロードは8バイトのヘッダ+4バイト*9ワード=44
    if len(line) < 14 + 3 + 8 * 4 + 1:
        return None
    if line[:7] == b'\xB5\x62\x02\x13\x2C\x00\x05':  # UBX-RXM-SFRBX, 44 bytes, QZSS
        # 受信機が知らないPRN(将来の衛星・ノイズ)でKeyErrorにならないよう，未知なら読み捨てる
        satId = satellite_id.get(line[7] + 182)  # PRN -> Satellite ID
        if satId is None:
            return None
        data = b''
        for i in range(9):
            data += bytes((line[14+3+i*4], line[14+2+i*4], line[14+1+i*4], line[14+0+i*4]))
        if data[1] >> 2 == 43 or data[1] >> 2 == 44:  # Message Type 43=JMA-DC Report, 44=Other
            dcr_message = (data[:31] + bytes((data[31] & 0xC0,))).hex()[:-1]  # 256-4=252 bit
            sentence = '$QZQSM,' + satId + ',' + dcr_message + '*'
            return sentence + format(nmea_checksum(sentence), '02X')


def handle_ubx_frame(frame, t0_received):
    sentence = ubx2qzqsm(frame)
    if sentence is None:
        return
    print(sentence)
    params, key = decode_full(sentence)
    t1_decoded = time.time()  # T1: デコード完了
    if VERBOSE_DECODE:
        print(json.dumps(params, ensure_ascii=False, indent=2, default=str))
    note_satellite_seen(params)
    # 拠点に地域が割り当てられていて、かつこの通報が対象都道府県以外だけを
    # 対象にしている場合、ここで即座に処理を打ち切る(「送信しない」のではなく、
    # 重複排除の登録も含めて「それ以上処理しない」)。デコード自体はどの通報が
    # 対象かを判定するために避けられないが、それ以降は一切行わない。
    if not is_in_scope(params):
        print("(拠点の対象地域外のため処理をスキップ)")
        return
    # raw(プリアンブル・CRC・衛星IDを含まない本体)で重複判定する。
    # sentence はプリアンブルが送信ごとに巡回して毎回変わるため使えない。
    if is_recent_duplicate(params, sentence):
        print("(期限内の同一内容のため送信スキップ)")
    else:
        route_report(params, key, t0=t0_received, t1=t1_decoded)


def handle_nmea_line(line, print_all):
    if not print_all:
        return
    try:
        sentence = line.decode().strip('\r\n')
    except UnicodeDecodeError:
        return
    if is_valid_nmea_sentence(sentence):
        print(sentence)


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
    if LOCAL_URL:
        threading.Thread(target=_local_sender_worker_loop, daemon=True).start()
    threading.Thread(target=send_heartbeat_loop, daemon=True).start()
    threading.Thread(target=send_test_signal_loop, daemon=True).start()
    threading.Thread(target=region_config_refresh_loop, daemon=True).start()
    threading.Thread(target=local_config_sync_loop, daemon=True).start()

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

                framer = StreamFramer()
                while True:
                    # 1バイトずつread()するとシステムコールが多くPi 3のCPUを
                    # 無駄に使うため，受信済みの分をまとめて読む(無ければ
                    # timeout=1秒まで1バイト待つ)
                    chunk = ser.read(max(1, ser.in_waiting))
                    if not chunk:
                        if time.time() - last_byte_time > IDLE_TIMEOUT_SEC:
                            print(f"🔴 オフライン({IDLE_TIMEOUT_SEC}秒間データを受信していません)")
                            raise serial.SerialException(
                                f"{IDLE_TIMEOUT_SEC}秒間データを受信していません(切断の可能性)")
                        continue
                    last_byte_time = time.time()
                    # T0: 信号受信(バイト列を読み終えた時刻)。レイテンシ計測
                    # (T0受信→T1デコード→T2サーバー受信→T3配信→T4描画完了)の起点
                    t0_received = last_byte_time
                    for kind, frame in framer.feed(chunk):
                        if kind == "ubx":
                            handle_ubx_frame(frame, t0_received)
                        else:
                            handle_nmea_line(frame, args.nmea)
        except (serial.SerialException, OSError) as e:
            serial_ok.clear()
            print(f"⚠️ シリアル接続が切れました({e})。{RECONNECT_WAIT_SEC}秒後に再接続を試みます...")
            time.sleep(RECONNECT_WAIT_SEC)
