"""ex_factor 口径判别 (累积型 vs 事件型) 的回归测试。

背景: `_apply_adj_factor` 对因子表做 cum_prod(); 若数据源给的本来就是**累积因子**,
再累积一次等于把同一因子连乘 N 次 —— 实测 688553.SH 一年前价格被除 1.596^5 ≈ 10.4 倍。
判别必须同时满足两个方向:
  - 累积型 (stock-sdk/TickFlow: 窗口内每个交易日一行) 只归一一次, 不得重复累积;
  - 事件型 (扶摇: 只有除权事件日有行, 个股常常 1 行) 照常应用因子, 不得被误判成累积型
    而把因子归一成 1.0 整个丢掉。

上游 tests/test_adj_factor_event_columns.py::test_apply_adj_factor_tolerates_detail_columns
覆盖的是单个事件行的最短用例 (无相邻对、无日历证据 → 必须按事件型)。本文件补齐
累积型方向与两个兜底判据。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.indicators.pipeline import _apply_adj_factor


def test_cumulative_source_rows_are_not_accumulated_twice():
    """真实案例: 688553.SH 因子表窗口内每个交易日一行、值恒在 1.5967 附近。

    旧实现 (直接 cum_prod) 把窗口前的 2025-09-03 除以 1.596^5 ≈ 10.4 倍
    (raw 23.67 → 2.27); 正确结果应是 raw × f当日 / f最新 ≈ 23.68。
    """
    f_window = [1.597628, 1.599445, 1.598174, 1.596396, 1.596745]
    window_days = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16),
                   date(2026, 9, 17), date(2026, 9, 18)]
    raw = pl.DataFrame({
        "symbol": ["688553.SH"] * 6,
        "date": [date(2025, 9, 3), *window_days],
        "open": [23.0] * 6,
        "high": [24.0] * 6,
        "low": [22.5] * 6,
        "close": [23.67, 11.0, 11.0, 11.0, 11.0, 11.0],
    })
    factors = pl.DataFrame({
        "symbol": ["688553.SH"] * 5,
        "trade_date": window_days,
        "ex_factor": f_window,
    })

    out = _apply_adj_factor(raw, factors).sort("date")

    # 累积型归一: ratio = f当日 / f最新; 窗口前的日期没有因子行 → cum = 1 → ratio = f首/f尾
    ratio_head = f_window[0] / f_window[-1]
    assert out["close"][0] == pytest.approx(23.67 * ratio_head)   # ≈ 23.68
    assert out["close"][0] > 20.0, "旧实现在这里得到 23.67 / 1.596^5 ≈ 2.27"
    assert out["close"][1] == pytest.approx(11.0 * ratio_head)
    assert out["close"][-1] == pytest.approx(11.0)                # 最新日不动
    # 窗口内相邻行只有 ~0.1% 的推导噪声, 没有 >0.5% 的真除权事件
    assert out["ex_rights"].to_list() == [False] * 6


def test_single_day_cumulative_window_is_not_read_as_one_giant_event():
    """只拉了 1 天窗口的累积型同步: 因子 161.68 远超任何单次除权比值, 不得当成"当天除权 161 倍"。

    判据三兜底: 单行表既无相邻对也无日历密度证据, 但单次除权事件比值上限约 5
    (10转30 ≈ 4), 161.68 只能是累积因子 → 按累积型处理, 历史价不动。
    """
    raw = pl.DataFrame({
        "symbol": ["000001.SZ"] * 3,
        "date": [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)],
        "open": [10.0] * 3, "high": [10.2] * 3, "low": [9.8] * 3,
        "close": [10.0] * 3,
    })
    factors = pl.DataFrame({
        "symbol": ["000001.SZ"], "trade_date": [date(2026, 9, 24)], "ex_factor": [161.68],
    })

    out = _apply_adj_factor(raw, factors).sort("date")

    assert out["close"].to_list() == pytest.approx([10.0, 10.0, 10.0])
    assert out["ex_rights"].to_list() == [False] * 3


def test_sparse_event_table_with_tiny_factors_stays_event_type():
    """事件型 + 小额分红 (相邻事件因子只差 0.005%) 必须靠日历密度判成事件型。

    扶摇等事件型源的因子只覆盖事件日: 10 年 10 次 0.3% 分红 → 相邻行几乎不变
    (平整度兜底判据会误判成累积型), 但因子行只占窗口内交易日的 ~22% → 事件型。
    """
    event_years = list(range(2016, 2026))
    event_days = [date(y, 6, 15) for y in event_years]
    event_factors = [1.0030 + i * 0.00005 for i in range(len(event_days))]

    raw_days = [date(y, m, 15) for y in event_years for m in (1, 3, 6, 9, 12)]
    raw = pl.DataFrame({
        "symbol": ["A"] * len(raw_days),
        "date": raw_days,
        "open": [10.0] * len(raw_days),
        "high": [10.2] * len(raw_days),
        "low": [9.8] * len(raw_days),
        "close": [10.0] * len(raw_days),
    })
    factors = pl.DataFrame({
        "symbol": ["A"] * len(event_days),
        "trade_date": event_days,
        "ex_factor": event_factors,
    })

    out = _apply_adj_factor(raw, factors).sort("date")
    total = 1.0
    for f in event_factors:
        total *= f

    first = out.filter(pl.col("date") == date(2016, 1, 15))
    assert first["close"][0] == pytest.approx(10.0 / total)
    assert first["close"][0] < 9.95, "误判成累积型会得到 ≈10.0 (因子被归一成 1.0 丢掉)"
    last = out.filter(pl.col("date") == date(2025, 12, 15))
    assert last["close"][0] == pytest.approx(10.0)  # 最新价不动

    # 事件型: 只在事件当日标记除权, 不随 join_asof 传播到事件之后的每一根 K 线
    assert out.filter(pl.col("date") == date(2016, 6, 15))["ex_rights"][0] is True
    assert out.filter(pl.col("date") == date(2016, 9, 15))["ex_rights"][0] is False
