"""盘中快照历史 → 量能与形态派生指标（不依赖真实分钟K）。

## 为什么需要

部署若用 stocksdk 全量分钟源, 其数据是"实时快照合成K"(volume/amount 恒为 0) 且
只写 `kline_minute_rt`, 不进 `kline_minute` 主分区 —— 于是 `intraday_features`
的 `vol_ratio_*` / `open_30m_*` 全为 null, 导致这四个条件无法判定:
  V型确认 / 点火突破 / 无量拉升诱多 / 量价背离

## 方案

行情循环每 tick 都拿得到**快照自带的累计成交量/成交额**(真实值, 非合成),
把关心的少数标的按**分钟**记一份极小历史, 即可派生与 `intraday_features`
**同口径**的量能特征:

    vol_ratio_N = 当前 N 分钟成交量之和 / 此前 N 分钟成交量之和
                  (按上/下午分组, 不跨午休)

    分钟成交量 = 累计成交量(本分钟) - 累计成交量(上一分钟)

同时记录现价/当日高低, 于是还能得到 `open_30m_high/low`(上午前30分钟高低)
以及分钟级 vwap 序列 —— 后者让"跌破均价线后收回"(V型) 这类**跨时点形态**
第一次变得可判定。

## 存储

`<data_dir>/user_data/intraday_history/date=YYYY-MM-DD.json` —— 按日分文件,
跨日自动新起一份; 体量 = 标的数 × 交易分钟数 × 数个字段(几 KB)。
只记录调用方指定的标的(通常 1~5 只), 不记录全市场。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.market_time import cn_now, cn_today

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# ── 派生阈值（与《海平分时擒牛》各讲对应；集中在此便于调参） ──────────
PUMP_PCT = 0.03            # 拉升/涨幅阈值: 相对开盘 ≥3% (第2/3讲)
IGNITE_VOL = 2.0           # 点火/突破需"放量": 5分钟量比 ≥2
SHRINK_VOL = 0.7           # "量能萎缩": 5分钟量比 <0.7
NEAR_DAY_HIGH = 0.003      # 距当日最高 ≤0.3% 视为"创新高"
V_SHAPE_LOOKBACK_MIN = 10  # V型: 最近 10 分钟内曾跌破均价线
OPEN_30M_MINUTES = 30      # 开盘 30 分钟(上午前30根)

_MORNING_END = 11 * 60 + 30
_AFTERNOON_START = 13 * 60


def _path(data_dir: Path, day: str) -> Path:
    d = data_dir / "user_data" / "intraday_history"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"date={day}.json"


def _minutes_of_day(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[2:4])


def _session_of(hhmm: str) -> int:
    """0=上午, 1=下午（午休不产生分钟, 故不会跨组）。"""
    return 0 if _minutes_of_day(hhmm) <= _MORNING_END else 1


def _fnum(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None      # 过滤 NaN
    except (TypeError, ValueError):
        return None


# ── 记录 ────────────────────────────────────────────────────────
def record(data_dir: Path, frame: Any, symbols: list[str], now: datetime | None = None) -> int:
    """把本 tick 的快照关键字段按分钟记入历史。返回写入的标的数。

    ``frame`` 为行情增强帧(需含 symbol 及以下部分列):
      raw_close/close(现价)、volume(当日累计量)、amount(当日累计额)、
      raw_high/raw_low(当日最高/最低)
    """
    if frame is None or not symbols:
        return 0
    try:
        import polars as pl
    except Exception:  # pragma: no cover
        return 0
    try:
        cols = set(getattr(frame, "columns", []) or [])
        if "symbol" not in cols:
            return 0
        pick = [c for c in ("symbol", "raw_close", "close", "volume", "amount", "raw_high", "raw_low")
                if c in cols]
        rows = frame.select(pick).filter(pl.col("symbol").cast(pl.Utf8).is_in(symbols)).to_dicts()
    except Exception:
        logger.debug("intraday history: frame extract failed", exc_info=True)
        return 0

    ts = now or cn_now()
    day = ts.strftime("%Y-%m-%d")
    hhmm = ts.strftime("%H%M")
    n = 0
    with _LOCK:
        try:
            p = _path(data_dir, day)
            data: dict[str, Any] = {"date": day, "symbols": {}}
            if p.exists():
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8"))
                    if str(loaded.get("date")) == day:
                        data = loaded
                except Exception:
                    data = {"date": day, "symbols": {}}
            syms = data.setdefault("symbols", {})
            for r in rows:
                sym = str(r.get("symbol") or "").strip()
                if not sym:
                    continue
                price = _fnum(r.get("raw_close"))
                if price is None:
                    price = _fnum(r.get("close"))
                if price is None or price <= 0:
                    continue
                syms.setdefault(sym, {}).setdefault("m", {})[hhmm] = {
                    "p": round(price, 4),
                    "v": _fnum(r.get("volume")),
                    "a": _fnum(r.get("amount")),
                    "h": _fnum(r.get("raw_high")),
                    "l": _fnum(r.get("raw_low")),
                }
                n += 1
            p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        except Exception:
            logger.debug("intraday history: record failed", exc_info=True)
    return n


def load(data_dir: Path, day: str | None = None) -> dict[str, Any]:
    """读某日历史(默认今日)。返回 {"date":..., "symbols":{...}}。"""
    d = day or cn_today().isoformat()
    p = _path(data_dir, d)
    if not p.exists():
        return {"date": d, "symbols": {}}
    try:
        with _LOCK:
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"date": d, "symbols": {}}


# ── 派生 ────────────────────────────────────────────────────────
def derive(data_dir: Path, symbols: list[str], day_open: dict[str, float] | None = None,
           now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """由快照历史派生量能与形态指标（与 intraday_features 同口径）。

    ``day_open``: {symbol: 当日开盘价}，用于算 `pct_from_open`（形态判定需要）。
                  缺省时该标的的 pct_from_open 为 None，相关形态布尔不成立。

    返回 {symbol: {...}}，字段:
      vol_ratio_1m/3m/5m_today   放量比（分上下午、不跨午休；窗口不满为 None）
      open_30m_high_dist / low_dist
      day_high_dist / day_low_dist
      vwap / price / minutes / last_minute
      pct_from_open / pct_vs_prev_close
      v_shape / ignite / no_vol_push / vol_divergence   四个形态布尔（确定性判定）
    """
    data = load(data_dir, (now or cn_now()).strftime("%Y-%m-%d"))
    syms = data.get("symbols") or {}
    days = day_open or {}
    out: dict[str, dict[str, Any]] = {}

    for sym in symbols:
        minutes: dict[str, dict[str, Any]] = ((syms.get(sym) or {}).get("m")) or {}
        if not minutes:
            continue
        order = sorted(minutes.keys())
        by_session: dict[int, list[str]] = {0: [], 1: []}
        for hh in order:
            by_session[_session_of(hh)].append(hh)

        res: dict[str, Any] = {"minutes": len(order), "last_minute": order[-1]}
        cur = minutes[order[-1]] or {}
        price = _fnum(cur.get("p"))
        cum_v, cum_a = _fnum(cur.get("v")), _fnum(cur.get("a"))
        vwap = round(cum_a / (cum_v * 100.0), 6) if (cum_v and cum_a and cum_v > 0) else None

        # 1) 分钟成交量 = 累计量差值（下午首分钟自然跨午休累计，差值仍正确）
        deltas: dict[int, list[float]] = {0: [], 1: []}
        for sess, hhs in by_session.items():
            prev_cum: float | None = None
            for hh in hhs:
                cum = _fnum((minutes[hh] or {}).get("v"))
                if cum is None:
                    deltas[sess].append(0.0)
                    continue
                deltas[sess].append(cum - prev_cum if prev_cum is not None else cum)
                prev_cum = cum

        # 2) vol_ratio_N = 当前N分钟量和 / 此前N分钟量和（优先当前时段）
        for n, key in ((1, "vol_ratio_1m_today"), (3, "vol_ratio_3m_today"), (5, "vol_ratio_5m_today")):
            val = None
            for sess in (1, 0):
                d = deltas[sess]
                if len(d) >= 2 * n:
                    prev = sum(d[-2 * n:-n])
                    if prev > 0:
                        val = round(sum(d[-n:]) / prev, 6)
                    break
            res[key] = val
        vr5 = res["vol_ratio_5m_today"]

        # 3) 开盘 30 分钟高低（上午前 30 分钟）
        am = by_session[0]
        o30h = o30l = None
        if len(am) >= OPEN_30M_MINUTES:
            highs = [_fnum((minutes[hh] or {}).get("h")) for hh in am[:OPEN_30M_MINUTES]]
            lows = [_fnum((minutes[hh] or {}).get("l")) for hh in am[:OPEN_30M_MINUTES]]
            highs = [h for h in highs if h]
            lows = [l for l in lows if l]
            o30h = round(max(highs), 4) if highs else None
            o30l = round(min(lows), 4) if lows else None
        res["open_30m_high"], res["open_30m_low"] = o30h, o30l
        res["open_30m_high_dist"] = (round(price / o30h - 1.0, 6) if (price and o30h) else None)
        res["open_30m_low_dist"] = (round(price / o30l - 1.0, 6) if (price and o30l) else None)

        # 4) 当日高低 + 相对开盘/昨收
        allh = [h for h in (_fnum((minutes[hh] or {}).get("h")) for hh in order) if h]
        alll = [l for l in (_fnum((minutes[hh] or {}).get("l")) for hh in order) if l]
        day_high = max(allh) if allh else None
        day_low = min(alll) if alll else None
        res["day_high_dist"] = (round(price / day_high - 1.0, 6) if (price and day_high) else None)
        res["day_low_dist"] = (round(price / day_low - 1.0, 6) if (price and day_low) else None)

        do = _fnum(days.get(sym))
        res["pct_from_open"] = (round(price / do - 1.0, 6) if (price and do) else None)
        res["vwap"], res["price"] = vwap, price

        # 5) 形态布尔（确定性，避免让模型自己算）
        #    V型: 当日曾拉升≥3% + 近 lookback 分钟内曾跌破均价线 + 当前已收回
        pumped = False
        if do:
            for hh in order:
                px = _fnum((minutes[hh] or {}).get("p"))
                if px and px / do - 1.0 >= PUMP_PCT:
                    pumped = True
                    break
        below_recent = False
        if price and vwap:
            for hh in order[-V_SHAPE_LOOKBACK_MIN:]:
                px = _fnum((minutes[hh] or {}).get("p"))
                cv, ca = _fnum((minutes[hh] or {}).get("v")), _fnum((minutes[hh] or {}).get("a"))
                if px and cv and ca and cv > 0 and px < ca / (cv * 100.0):
                    below_recent = True
                    break
        res["v_shape"] = bool(pumped and below_recent and price and vwap and price > vwap)

        pct_open = res["pct_from_open"]
        res["ignite"] = bool(
            price and o30h and price >= o30h
            and vr5 is not None and vr5 >= IGNITE_VOL
            and pct_open is not None and pct_open >= PUMP_PCT
        )
        res["no_vol_push"] = bool(
            vwap and price and price > vwap
            and pct_open is not None and pct_open >= PUMP_PCT
            and vr5 is not None and vr5 < SHRINK_VOL
        )
        res["vol_divergence"] = bool(
            price and day_high and abs(price / day_high - 1.0) <= NEAR_DAY_HIGH
            and vr5 is not None and vr5 < SHRINK_VOL
        )
        out[sym] = res
    return out
