"""③ 近 5 天短线热度 + 龙头谱系。

- 近 5 交易日热度趋势：每日涨停家数 / 最高连板 / 炸板率（akshare 涨停池，按日）。
- 龙头谱系：每日最高标龙头 + 该龙头此后每日的累计兑现（腾讯 hist，回答"前几天龙头现在怎么样"）。
数据源都不被封（akshare 涨停池 + 腾讯 hist）。
"""

from __future__ import annotations

from typing import Optional

from . import reflection
from . import trade_calendar

from . import fetchers as dr


def _last_trade_dates(n: int = 5) -> list[str]:
    """最近 n 个已收盘交易日（升序）。交易日历统一在 trade_calendar。"""
    return trade_calendar.last_trade_dates(n)


def _followthrough(code: str, appear_date: str, end_date: str) -> list[dict]:
    """龙头自 appear_date 收盘起，到 end_date 每日累计收益（%）。"""
    try:
        import akshare as ak

        sym = reflection._tx_symbol(code)
        df = ak.stock_zh_a_hist_tx(
            symbol=sym, start_date=appear_date.replace("-", ""), end_date=end_date.replace("-", "")
        )
        if df is None or len(df) == 0 or "close" not in df.columns:
            return []
        df = df.reset_index(drop=True)
        base: Optional[float] = None
        out = []
        for _, r in df.iterrows():
            dt = str(r["date"])
            c = float(r["close"])
            if dt == appear_date:
                base = c
            if base and base > 0 and dt >= appear_date:
                out.append({"date": dt, "cum_ret": round((c / base - 1) * 100, 2)})
        return out
    except Exception:
        return []


LINEAGE_SCHEMA = 2


def peak_drawdown(series: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(区间最高累计收益, 现价距该最高点的跌幅%)。两者都以**收盘价**口径"""
    vals = [s["cum_ret"] for s in (series or [])
            if isinstance(s.get("cum_ret"), (int, float))]
    if len(vals) < 2:
        return (None, None)
    peak = max(vals)
    cur = vals[-1]
    if 1 + peak / 100 <= 0:      # 理论上到不了（跌 100%），但除零要防
        return (round(peak, 2), None)
    dd = ((1 + cur / 100) / (1 + peak / 100) - 1) * 100
    # 浮点噪声会让"就在最高点"渲染成 -0.00%，钳到 0
    return (round(peak, 2), round(min(dd, 0.0), 2))


def build_weekly(n: int = 5) -> dict:
    dates = _last_trade_dates(n)
    if not dates:
        return {"error": "取交易日历失败", "days": [], "leader_lineage": []}

    daily = []
    for d in dates:
        try:
            zt = dr.fetch_zt_pool(d.replace("-", ""))
            if zt.get("error_zt") or zt.get("zt") is None:
                daily.append({"date": d, "limit_up": None, "broken_rate": None,
                              "highest_consec": None, "leader": None, "unavailable": True})
                continue
            ztdf = zt.get("zt")
            n_zt = int(len(ztdf))
            hc = int(zt.get("highest_consec", 0) or 0)
            if zt.get("error_zb"):  # 炸板池失败 → 炸板率不可知，不算 0
                br = None
            else:
                n_zb = int(zt.get("zb_count", 0) or 0)
                br = round(n_zb / (n_zb + n_zt), 3) if (n_zb + n_zt) else 0
            ladder = zt.get("ladder", [])
            top = ladder[0] if ladder else None
            daily.append({
                "date": d, "limit_up": n_zt, "broken_rate": br, "highest_consec": hc,
                "leader": ({"code": top["code"], "name": top["name"],
                            "boards": top["consec_boards"], "sector": top.get("sector", "")}
                           if top else None),
            })
        except Exception as exc:  # noqa: BLE001  单日彻底失败不拖累整周
            daily.append({"date": d, "error": type(exc).__name__, "limit_up": None,
                          "broken_rate": None, "highest_consec": None,
                          "leader": None, "unavailable": True})

    current_top_code = None
    for row in reversed(daily):
        if row.get("leader"):
            current_top_code = row["leader"]["code"]
            break

    # 龙头谱系：每个"当日最高标龙头"（去重，取其最早出现日）+ 此后走势
    lineage = []
    seen = set()
    end_date = dates[-1]
    for row in daily:
        ld = row.get("leader")
        if not ld or ld["code"] in seen:
            continue
        seen.add(ld["code"])
        series = _followthrough(ld["code"], row["date"], end_date)
        cum = series[-1]["cum_ret"] if series else None
        peak, drawdown = peak_drawdown(series)
        lineage.append({
            "code": ld["code"], "name": ld["name"], "sector": ld["sector"],
            "appear_date": row["date"], "boards_then": ld["boards"],
            "cum_return_since": cum, "series": series,
            "peak_cum_ret": peak, "drawdown_from_peak": drawdown,
            "is_current_top": ld["code"] == current_top_code,
        })

    return {"days": daily, "leader_lineage": lineage, "lineage_schema": LINEAGE_SCHEMA}
