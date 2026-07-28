#!/usr/bin/env python3
"""read_legacy_dual.is_in_scope() の単体テスト(外部依存なしで実行可能)。

  実行: python3 test_is_in_scope.py

  実機(qzss01、関東限定拠点)で発覚したバグの回帰テスト: is_in_scope()が
  prefectures_raw(震度速報・震源が持つフィールド)しか見ておらず、気象警報
  (weather_forecast_regions_raw)・降灰(local_governments_raw)は別の
  フィールド名のため地域ロックが一切効いていなかった(関東限定の拠点に
  鳥取県・富山県の気象警報が素通りしていた)。
"""
import read_legacy_dual as m

FAILS = []


def check(cond, msg):
    print(("✅ " if cond else "❌ FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def run():
    KANTO = {8, 9, 10, 11, 12, 13, 14}  # 茨城/栃木/群馬/埼玉/千葉/東京/神奈川

    # --- 地域ロックが無い(全国対象)なら常にTrue ---
    m.allowed_prefecture_ids = None
    check(m.is_in_scope({"disaster_category_no": 3, "prefectures_raw": [43]}) is True,
          "地域ロック無し(全国対象)なら熊本県の震度速報も通す")

    m.allowed_prefecture_ids = KANTO

    # --- 震度速報/震源(prefectures_raw) ---
    check(m.is_in_scope({"prefectures_raw": [13]}) is True, "震度速報: 対象地域(東京都)は通す")
    check(m.is_in_scope({"prefectures_raw": [43]}) is False, "震度速報: 対象外(熊本県)は除外する【回帰確認: 既存動作】")
    check(m.is_in_scope({"prefectures_raw": [43, 13]}) is True, "震度速報: 複数県のうち1つでも対象地域なら通す")

    # --- 気象警報(weather_forecast_regions_raw) 【今回の修正対象】 ---
    tokyo_weather = 130010  # 13(東京都) * 10000 + 10
    tottori_weather = 310010  # 31(鳥取県) * 10000 + 10
    check(m.is_in_scope({"weather_forecast_regions_raw": [tokyo_weather]}) is True,
          "気象警報: 対象地域(東京都)は通す")
    check(m.is_in_scope({"weather_forecast_regions_raw": [tottori_weather]}) is False,
          "気象警報: 対象外(鳥取県)は除外する【修正前は素通りしていたバグ】")
    check(m.is_in_scope({"weather_forecast_regions_raw": [tottori_weather, tokyo_weather]}) is True,
          "気象警報: 複数県のうち1つでも対象地域なら通す")

    # --- 降灰(local_governments_raw) 【今回の修正対象】 ---
    tokyo_gov = 1310100  # 13(東京都)*100000 + 10100
    kagoshima_gov = 4620100  # 46(鹿児島県)*100000 + 20100 (実データの桜島降灰と同じ値)
    check(m.is_in_scope({"local_governments_raw": [tokyo_gov]}) is True,
          "降灰: 対象地域(東京都)は通す")
    check(m.is_in_scope({"local_governments_raw": [kagoshima_gov]}) is False,
          "降灰: 対象外(鹿児島県)は除外する【修正前は素通りしていたバグ】")

    # --- 判別できない通報はfail-open(常に通す、意図的な既存方針) ---
    check(m.is_in_scope({"disaster_category_no": 2}) is True, "震源のみ(都道府県情報なし)は常に通す")
    check(m.is_in_scope({"disaster_category_no": 5}) is True, "津波(都道府県情報なし)は常に通す")
    check(m.is_in_scope({"disaster_category_no": 1, "eew_forecast_regions_raw": [270]}) is True,
          "EEW(予報区コードは都道府県に単純変換できないためfail-open)")
    check(m.is_in_scope({"disaster_category_no": 11, "flood_forecast_regions_raw": [123456789012]}) is True,
          "洪水(河川コードは都道府県に単純変換できないためfail-open)")
    check(m.is_in_scope({}) is True, "空データは常に通す")

    m.allowed_prefecture_ids = None  # 他のテストに影響しないよう元に戻す

    print("\n" + ("🎉 全テスト成功" if not FAILS else "❌ 失敗 {} 件: {}".format(len(FAILS), FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(run())
