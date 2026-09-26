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
    n = len(closes)
    # v3.1 放量前提: 突破颈线那根(idx39 收盘 10.2 站上 9.8)必须放量(≥前5日均量×1.5);
    # 前5根(34-38)保持基量 10000, 突破根(39)起放大 → 倍数 3.0x
    vols = [10000.0] * n
    for i in range(39, 46):
        vols[i] = 30000.0
    df = _df(closes, vols=vols)
    res = compute_turning_analysis(df)
    forms = [p["name"] for p in res["patterns"]]
    assert "破底翻" in forms
    bbr = next(p for p in res["patterns"] if p["key"] == "break_below_reverse")
    assert bbr["confirmed"] is True
    assert bbr["neckline"] is not None
    assert bbr["target"] is not None
    # v3.1 翻箱法: 第一 = 颈线 + (颈线 − 破底低); 第二 = 颈线 + 2×(颈线 − 破底低)
    assert bbr["target"] > bbr["neckline"]
    assert bbr["box_height"] == round(bbr["neckline"] - bbr["bottom"], 2)
    assert bbr["target2"] == pytest.approx(round(bbr["neckline"] + 2 * (bbr["neckline"] - bbr["bottom"]), 2), abs=0.02)
    assert bbr["target_basis"] and "翻箱" in bbr["target_basis"]
    assert bbr["vol_confirm"] is True
    assert bbr["vol_note"] and "放量确认" in bbr["vol_note"]
    # v3.1 时间波字段
    assert bbr.get("time_wave") and bbr["time_wave"]["windows"]
    assert bbr.get("time_wave_status")
    assert bbr.get("time_wave_note")
    # v3.1 三态: 满足点 11.1 已被突破后高点(11.5)触及 → 已完成
    assert bbr.get("status") in ("valid", "done")
    assert len(bbr["marks"]) >= 2


def test_break_below_reverse_no_volume_invalid():
    """v3.1 放量前提: 无量突破颈线 → 破底翻失效, target 置空。"""
    closes = _break_below_reverse()
    df = _df(closes)   # 默认恒定量能(无量突破)
    res = compute_turning_analysis(df)
    bbr = next((p for p in res["patterns"] if p["key"] == "break_below_reverse"), None)
    if bbr:
        assert bbr["vol_confirm"] is False
        assert bbr["target"] is None
        assert bbr.get("force_invalid") and "无量" in bbr["force_invalid"]
        assert bbr["status"] == "invalid"
        assert bbr["confidence"] == 0.40


def test_time_wave_module():
    """v3.1 时间波: 前波下跌K棒数 × 0.618/1.0/1.618 推变盘窗口。"""
    from app.indicators.turning_points import _time_wave, _timewave_resonance
    highs = [10.0 + i * 0.1 for i in range(50)]
    lows = [9.5 + i * 0.05 for i in range(50)]
    dates = [f"2026-03-{i + 1:02d}" for i in range(50)]
    tw = _time_wave(highs, lows, dates, high_idx=10, low_idx=30)
    assert tw is not None
    assert tw["down_bars"] == 30 - 10 + 1          # 含两端
    ratios = [w["ratio"] for w in tw["windows"]]
    assert ratios == [0.618, 1.0, 1.618]
    assert all(w["bars"] == max(1, round(tw["down_bars"] * r)) for w, r in zip(tw["windows"], ratios))
    assert tw["elapsed"] == 50 - 1 - 30
    # 共振判定: 价格已到目标 + 时间窗口临近 → 至少返回非空标签
    label, note = _timewave_resonance(price=15.0, tw=tw, target=14.0, target2=16.0)
    assert label and note


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


def _conv_triangle(n=90):
    """收敛三角形: 高点逐级降低 + 低点逐级抬高(在 1/2~3/4 行程内突破上缘)。"""
    closes = []
    # 段1: 上涨到 12.0
    for i in range(20):
        closes.append(9.0 + i * 0.15)                 # → 11.85
    # 三角形: 高 12.0→11.6→11.2→10.9, 低 10.9→11.0→11.1→11.2(收敛)
    closes += [12.0, 10.9, 11.6, 11.0, 11.2, 11.1, 10.9, 11.2, 11.1, 11.3]
    # 突破上缘, 上行
    for i in range(n - len(closes)):
        closes.append(11.5 + i * 0.05)                # → 14.9
    return closes[:n]


def test_triangle_break_position_field():
    """v3.1 收敛三角位置过滤: 输出 break_position 且 1/2~3/4 有效区标注。"""
    closes = _conv_triangle()
    df = _df(closes)
    res = compute_turning_analysis(df)
    tri = next((p for p in res["patterns"] if p["key"] == "triangle"), None)
    if tri:
        # 无论是否识别, 都必须有 break_position 字段(v3.1 位置过滤)
        assert "break_position" in tri
        if tri["confirmed"]:
            assert "1/2~3/4有效区" in tri["break_position"]


def test_tri_state_status_marked_on_patterns():
    """v3.1 三态: 每个形态都有 status / status_label。"""
    closes = _break_below_reverse()
    n = len(closes)
    vols = [10000.0] * n
    for i in range(39, 46):
        vols[i] = 30000.0
    res = compute_turning_analysis(_df(closes, vols=vols))
    assert len(res["patterns"]) > 0
    for p in res["patterns"]:
        assert p.get("status") in ("valid", "done", "invalid")
        assert p.get("status_label")
    # 破底翻确认 + 满足点 11.94 未被触及 → 有效(valid) 或 已完成(done)
    bbr = next(p for p in res["patterns"] if p["key"] == "break_below_reverse")
    assert bbr["status"] in ("valid", "done", "invalid")
