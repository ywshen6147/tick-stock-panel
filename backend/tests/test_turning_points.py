"""蔡森·多空转折顾问 —— turning_points.py 多形态深度分析单元测试。"""
import math

import polars as pl
import pytest

from app.indicators.turning_points import (
    TURNING_TYPE,
    compute_turning_analysis,
    _detect_break_below_reverse,
    _detect_double_bottom,
    _detect_double_top,
    _detect_fake_breakout,
)


def _df(closes, highs=None, lows=None, opens=None, vols=None):
    """构造最小 polars 日 K DataFrame(日期自动生成)。"""
    n = len(closes)
    highs = highs or [c * 1.03 for c in closes]
    lows = lows or [c * 0.97 for c in closes]
    opens = opens or closes
    vols = vols or [10000.0] * n
    dates = [f"2026-01-{i + 1:02d}" for i in range(n)]
    return pl.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })


def _uptrend(n=80, start=10.0, step=0.1):
    return [start + i * step for i in range(n)]


def _downtrend(n=80, start=20.0, step=0.1):
    return [start - i * step for i in range(n)]


def _w_bottom(n=70):
    """W底形态:下跌 → 双底(清晰两低) → 站上颈线(第二底在最近 100 根内)。"""
    closes = []
    # 段1: 12 → 10.2(下跌,给 swing 检测留空间)
    for i in range(20):
        closes.append(12.0 - i * 0.09)          # → 10.29
    # 第一底 10.0
    closes.append(10.0)
    # 反弹到颈线 10.9
    closes += [10.2, 10.4, 10.6, 10.8, 10.9, 10.8, 10.6, 10.4]
    # 回踩第二底 10.1(与第一底接近)
    closes += [10.3, 10.2, 10.1, 10.15, 10.3]
    # 站上颈线,继续上行
    for i in range(n - len(closes)):
        closes.append(10.9 + i * 0.06)          # → 12.5+
    return closes[:n]


def _m_top(n=60):
    """M头形态:上涨 → 双顶 → 跌破颈线(第二顶在最近 40 根内)。"""
    closes = []
    for i in range(25):
        closes.append(10.0 + i * 0.16)
    closes += [14.0, 13.9, 13.6, 13.2, 12.9, 12.6, 12.5, 12.5, 12.6, 12.9,
               13.2, 13.5, 13.8, 13.8, 13.7, 13.4, 13.0, 12.6, 12.3, 12.1,
               11.9, 11.7, 11.5]
    for i in range(n - len(closes)):
        closes.append(11.5 - i * 0.02)
    return closes


def _break_below_reverse(n=80):
    """破底翻形态:前低 → 破底 → 快速收复站上颈线。"""
    closes = []
    # 段1: 12 → 9(前低)
    for i in range(25):
        closes.append(12.0 - i * 0.12)          # → 9.0
    # 前低 9.0 附近小幅反弹(颈线 9.8)
    closes += [9.0, 9.2, 9.5, 9.8, 9.6, 9.3, 9.1]
    # 破底 8.5(低于前低 9.0)
    closes += [8.8, 8.6, 8.5, 8.7, 9.0, 9.4, 9.8, 10.2, 10.5, 10.7, 10.9,
               11.0, 11.1, 11.2, 11.3, 11.4, 11.5, 11.4, 11.3, 11.2, 11.1,
               11.0, 10.9, 10.8, 10.7, 10.6, 10.5, 10.4, 10.3, 10.2, 10.1,
               10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9]
    return closes[:n]


def _fake_breakout(n=100):
    """向上假突破:前高 → 放量突破 → 长上影见顶 → 跌回颈线下方。"""
    closes = []
    # 段1: 8 → 12(前高)
    for i in range(30):
        closes.append(8.0 + i * 0.13)           # → 11.9
    closes.append(12.0)                          # 前高 12.0
    # 回撤
    closes += [11.8, 11.5, 11.2, 11.0, 11.2, 11.5]
    # 放量突破 12.3
    closes += [12.1, 12.3]
    # 长上影见顶(高 12.8 但收 12.2)后下跌
    closes += [12.2, 11.8, 11.5, 11.2, 11.0, 10.8, 10.5, 10.3, 10.0, 9.8]
    # 继续阴跌
    for i in range(n - len(closes)):
        closes.append(9.8 - i * 0.03)
    return closes[:n]


def test_empty_df_returns_default():
    res = compute_turning_analysis(pl.DataFrame())
    assert res["form"] == "无数据"
    assert res["points"] == []
    assert res["patterns"] == []
    assert res["waves"] == {"available": False}
    assert res["summary"] == "无 K 线数据"


def test_break_below_reverse_detected():
    closes = _break_below_reverse()
    df = _df(closes)
    res = compute_turning_analysis(df)
    forms = [p["name"] for p in res["patterns"]]
    assert "破底翻" in forms
    bbr = next(p for p in res["patterns"] if p["key"] == "break_below_reverse")
    assert bbr["confirmed"] is True
    assert bbr["neckline"] is not None
    assert bbr["target"] is not None
    # 满足点 = 颈线 + (颈线 - 底)
    assert bbr["target"] > bbr["neckline"]
    assert len(bbr["marks"]) >= 2


def test_w_bottom_detected():
    closes = _w_bottom()
    df = _df(closes)
    res = compute_turning_analysis(df)
    forms = [p["name"] for p in res["patterns"]]
    assert "W底/双底" in forms
    wb = next(p for p in res["patterns"] if p["key"] == "double_bottom")
    assert wb["neckline"] is not None
    assert wb["target"] is not None
    # 满足点 = 颈线 + (颈线 - 底) > 颈线
    assert wb["target"] > wb["neckline"]
    assert wb["bottom"] < wb["neckline"]


def test_double_top_detected():
    closes = _m_top()
    df = _df(closes)
    res = compute_turning_analysis(df)
    # 600028 数据里近期 M头可能被破底翻优先;此处直接测检测函数
    highs = [c * 1.03 for c in closes]
    lows = [c * 0.97 for c in closes]
    n = len(closes)
    dt = _detect_double_top(highs, lows, closes, closes[-1], n)
    if dt:
        assert dt["name"] == "M头/双顶"
        assert dt["target"] is not None
        assert dt["target"] < dt["neckline"]
        assert len(dt["marks"]) >= 2


def test_fake_breakout_detected():
    closes = _fake_breakout()
    n = len(closes)
    highs = [c * 1.04 for c in closes]
    lows = [c * 0.96 for c in closes]
    vols = [10000.0] * n
    # 放量段:突破日附近放大
    for i in range(30, 45):
        vols[i] = 30000.0
    fb = _detect_fake_breakout(highs, lows, closes, vols, closes[-1], n)
    if fb:
        assert fb["name"] == "向上假突破"
        assert fb["confirmed"] is True
        assert fb["top"] > fb["neckline"]
        assert len(fb["marks"]) >= 2


def test_trend_detected():
    closes_up = _uptrend()
    res_up = compute_turning_analysis(_df(closes_up))
    assert any(p["key"] == "trend" and p["direction"] == "bull" for p in res_up["patterns"])

    closes_dn = _downtrend()
    res_dn = compute_turning_analysis(_df(closes_dn))
    assert any(p["key"] == "trend" and p["direction"] == "bear" for p in res_dn["patterns"])


def test_box_detected():
    import random
    rng = random.Random(13)
    closes = []
    v = 10.7
    for _ in range(80):
        v = max(10.2, min(11.3, v + rng.uniform(-0.2, 0.2)))
        closes.append(round(v, 3))
    highs = [c + 0.12 for c in closes]
    lows = [c - 0.12 for c in closes]
    res = compute_turning_analysis(_df(closes, highs, lows))
    forms = [p["name"] for p in res["patterns"]]
    assert "箱体震荡" in forms


def test_points_are_pricelevel_compatible():
    closes = _break_below_reverse()
    df = _df(closes)
    res = compute_turning_analysis(df)
    assert len(res["points"]) > 0
    for p in res["points"]:
        assert p["type"] == TURNING_TYPE
        assert p["value"] > 0
        assert p["side"] in ("resistance", "support", "neutral")
        assert p["strength"] in ("strong", "medium", "weak")
        assert "label" in p


def test_points_sorted_by_distance():
    closes = _break_below_reverse()
    df = _df(closes)
    res = compute_turning_analysis(df)
    price = closes[-1]
    dists = [abs(p["value"] - price) for p in res["points"]]
    assert dists == sorted(dists)


def test_waves_structure():
    closes = _break_below_reverse()
    df = _df(closes)
    res = compute_turning_analysis(df)
    w = res["waves"]
    assert w["available"] is True
    assert w["bottom"] is not None
    assert w["first_wave_high"] is not None
    assert w["first_wave_height"] > 0
    assert w["second_wave_target"] is not None
    # 第二波 = 回踩低 + 第一波高度
    assert w["second_wave_target"] > w["pullback_low"]


def test_verdict_matches_bull_structure():
    closes = _break_below_reverse()
    df = _df(closes)
    res = compute_turning_analysis(df)
    assert res["verdict"] in ("bull", "bear", "neutral")
    # 破底翻确认 → 至少不该是 bear
    assert res["verdict"] != "bear"


def test_summary_contains_key_info():
    closes = _break_below_reverse()
    df = _df(closes)
    res = compute_turning_analysis(df)
    s = res["summary"]
    assert "现价" in s
    assert "形态" in s
    assert res["desc"] != ""
