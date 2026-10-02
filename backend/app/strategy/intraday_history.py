"""盘中快照历史 → 量能与形态派生指标（不依赖真实分钟K）。

## 为什么需要

部署若用 stocksdk 全量分钟源, 其数据是"实时快照合成K"(volume/amount 恒为 0) 且
只写 `kline_minute_rt`, 不进 `kline_minute` 主分区 —— 于是 `intraday_features`
的 `vol_ratio_*` / `open_30m_*` 全为 null, 导致这四个条件无法判定:
  V型确认 / 点火突破 / 无量拉升诱多 / 量价背离

## 方案

行情循环每 tick 都拿得到**快照自带的累计成交量/成交额**(真实值, 非合成),
把关心的少数标的按**分钟**记一份极小历史, 即可与 `intraday_features`
**同口径**地重建:

    vol_ratio_N = 当前 N 分钟成交量之和 / 此前 N 分钟成交量之和
                  (按上/下午分组, 不跨午休)
    分钟成交量  = 累计成交量(本分钟) - 累计成交量(上一分钟)

同时记录现价/当日高低, 于是还能得到 `open_30m_high/low`(上午前30分钟高低)
以及分钟级 vwap 序列 —— 后者让"跌破均价线后收回"(V型) 这类**跨时点形态**
第一次变得可判定。

## 关键设计: 事件而非状态

用真实分钟K 校准(688553, 2026-09-14~18, 1116 根 bar)发现两类问题:

1. **"状态"会被高频重复计数**。例如"距当日最高≤0.3%"全天 20.9% 的 bar 成立,
   直接用状态判定会让「量价背离」在 5 天内报 58 次(单日最多 33 次)。
   → 四个形态全部改为**边沿事件**(由 False→True 的那一分钟才算), 58 → 28。

2. **`vol_ratio_5m`(近5分钟/前5分钟)是"加速度", 不适用于判"放量"**。
   实测 10 次上穿开盘30分钟高点中 9 次该比值 <0.8: 因为突破前刚经历放量拉升,
   分母(前5分钟)偏大。用它会把放量突破误判为缩量。
   → 新增 `vol_rel_5m` = 近5分钟量 / **当日每分钟均量**(水平比, 更贴近"放量"语义),
      形态判定改用它; `vol_ratio_*` 仍照常上报。

校准后频率(688553, 5 个交易日): v_shape 0.8/日, no_vol_push 0.6/日,
vol_divergence 约 2.8/日; **ignite 为 0** —— 该样本内所有突破都是缩量的,
按方法论本就不该点亮火(那属于「无量拉升」)。样本仅 5 天, 阈值属初始校准。

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

# ── 派生阈值（经真实分钟K 校准的初值; 集中在此便于调参） ──────────────
PUMP_PCT = 0.03            # 拉升/涨幅阈值: 相对开盘 ≥3% (第2/3讲)
IGNITE_VOL = 1.2           # 点火/突破需放量: vol_rel_5m ≥1.2 (相对当日均量)
SHRINK_VOL = 0.7           # 量能萎缩: vol_rel_5m <0.7
V_SHAPE_LOOKBACK_MIN = 10  # V型: 最近 10 分钟内曾跌破均价线
OPEN_30M_MINUTES = 30      # 开盘 30 分钟(上午前30根)
# 形态对外报「最近 N 分钟内发生过」——LLM 每 llm_interval_seconds(默认300s)才看一次盘,
# 若只报"本分钟恰好发生", 5 分钟间隔会大概率整段错过事件。
RECENT_WINDOW_MIN = 5

_MORNING_END = 11 * 60 + 30


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
def _per_minute(minutes: dict[str, dict[str, Any]], order: list[str],
                day_open: float | None) -> list[dict[str, Any]]:
    """逐分钟算出判定所需的量（用于取"最后一分钟"与"上一分钟"做边沿比较）。

    每分钟产出: price / vwap / cum_vol / delta / rel5 / pct_from_open /
                day_high(截至该分钟的运行最高) / day_low / o30h / o30l
    """
    rows: list[dict[str, Any]] = []
    # 上午每根 bar 的累计高/低, 用于开盘30分钟高低 (第 30 根定版)
    am = [hh for hh in order if _session_of(hh) == 0]
    o30h = o30l = None
    if len(am) >= OPEN_30M_MINUTES:
        hs = [_fnum((minutes[hh] or {}).get("h")) for hh in am[:OPEN_30M_MINUTES]]
        ls = [_fnum((minutes[hh] or {}).get("l")) for hh in am[:OPEN_30M_MINUTES]]
        hs = [h for h in hs if h]
        ls = [l for l in ls if l]
        o30h = max(hs) if hs else None
        o30l = min(ls) if ls else None

    prev_cum: float | None = None
    run_high: float | None = None
    run_low: float | None = None
    for i, hh in enumerate(order, start=1):
        c = minutes[hh] or {}
        price = _fnum(c.get("p"))
        cum_v, cum_a = _fnum(c.get("v")), _fnum(c.get("a"))
        delta = (cum_v - prev_cum) if (cum_v is not None and prev_cum is not None) else cum_v
        if cum_v is not None:
            prev_cum = cum_v
        h = _fnum(c.get("h"))
        l = _fnum(c.get("l"))
        if h is not None:
            run_high = h if run_high is None else max(run_high, h)
        if l is not None:
            run_low = l if run_low is None else min(run_low, l)
        vwap = (cum_a / (cum_v * 100.0)) if (cum_v and cum_a and cum_v > 0) else None
        rows.append({
            "hh": hh, "price": price, "vwap": vwap, "cum_vol": cum_v, "delta": delta,
            "day_high": run_high, "day_low": run_low, "o30h": o30h, "o30l": o30l,
            "pct_from_open": (price / day_open - 1.0) if (price and day_open) else None,
            "idx": i,
        })
    # rel5 = 近5分钟量 / 当日每分钟均量（水平比, 比"加速度"更贴合"放量"语义）
    for i, r in enumerate(rows):
        if r["cum_vol"] and r["idx"] > 0:
            avg = r["cum_vol"] / r["idx"]
            win = [x["delta"] for x in rows[max(0, i - 4): i + 1] if x["delta"] is not None]
            r["rel5"] = (sum(win) / (5.0 * avg)) if (avg > 0 and win) else None
        else:
            r["rel5"] = None
    return rows


def derive(data_dir: Path, symbols: list[str], day_open: dict[str, float] | None = None,
           now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """由快照历史派生量能与形态指标（与 intraday_features 同口径 + 边沿事件）。

    ``day_open``: {symbol: 当日开盘价}；算 `pct_from_open` 与形态判定需要。

    返回 {symbol: {...}}，字段:
      vol_ratio_1m/3m/5m_today   放量比（加速度; 分上下午、不跨午休）
      vol_rel_5m                 近5分钟量 / 当日每分钟均量（**放量水平比**）
      open_30m_high_dist / low_dist / day_high_dist / day_low_dist
      vwap / price / minutes / last_minute
      pct_from_open
      v_shape / ignite / no_vol_push / vol_divergence  四个形态（**边沿事件**）
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

        rows = _per_minute(minutes, order, _fnum(days.get(sym)))
        cur = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None

        res: dict[str, Any] = {
            "minutes": len(order), "last_minute": cur["hh"],
            "price": cur["price"], "vwap": cur["vwap"], "vol_rel_5m": cur["rel5"],
            "pct_from_open": cur["pct_from_open"],
        }

        # 1) 放量比(加速度; 与 intraday_features 完全同口径, 分上下午不跨午休)
        deltas: dict[int, list[float]] = {0: [], 1: []}
        for sess, hhs in by_session.items():
            dcum: float | None = None
            for hh in hhs:
                cv = _fnum((minutes[hh] or {}).get("v"))
                if cv is None:
                    deltas[sess].append(0.0)
                    continue
                deltas[sess].append(cv - dcum if dcum is not None else cv)
                dcum = cv
        for n, key in ((1, "vol_ratio_1m_today"), (3, "vol_ratio_3m_today"), (5, "vol_ratio_5m_today")):
            val = None
            for sess in (1, 0):
                d = deltas[sess]
                if len(d) >= 2 * n:
                    pv = sum(d[-2 * n:-n])
                    if pv > 0:
                        val = round(sum(d[-n:]) / pv, 6)
                    break
            res[key] = val

        # 2) 高度/涨幅类
        o30h, o30l = cur["o30h"], cur["o30l"]
        res["open_30m_high"], res["open_30m_low"] = o30h, o30l
        px = cur["price"]
        res["open_30m_high_dist"] = (round(px / o30h - 1.0, 6) if (px and o30h) else None)
        res["open_30m_low_dist"] = (round(px / o30l - 1.0, 6) if (px and o30l) else None)
        res["day_high_dist"] = (round(px / cur["day_high"] - 1.0, 6) if (px and cur["day_high"]) else None)
        res["day_low_dist"] = (round(px / cur["day_low"] - 1.0, 6) if (px and cur["day_low"]) else None)

        # 3) 形态 —— 内部按**边沿事件**判定, 对外报「**最近 N 分钟内发生过**」。
        #    原因: LLM 每 llm_interval_seconds(默认 300s)才看一次盘, 若只报"本分钟
        #    恰好发生", 五分钟的间隔会大概率整段错过事件。故对外给一个时间窗,
        #    并附 `*_age_min` 说明是多久前发生的（0 = 本分钟）。
        ages = _pattern_ages(rows)
        for name, age in ages.items():
            res[name] = bool(age is not None and age <= RECENT_WINDOW_MIN)
            res[f"{name}_age_min"] = age

        out[sym] = res
    return out


def _pattern_ages(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    """逐分钟判定四个形态的**边沿**, 返回各自"最近一次发生距今多少分钟"(None=从未)。"""
    last: dict[str, int | None] = {"v_shape": None, "ignite": None,
                                   "no_vol_push": None, "vol_divergence": None}
    pumped_ever = False
    n = len(rows)
    for i, r in enumerate(rows):
        p = rows[i - 1] if i >= 1 else None
        px, vwap, rel5 = r["price"], r["vwap"], r["rel5"]
        pct = r["pct_from_open"]
        if (pct or -9) >= PUMP_PCT:
            pumped_ever = True

        # 点火突破: 上穿开盘30分钟高点 (用 <=, 标记价定版时价格常正好等于该高点)
        if px and r["o30h"] and px >= r["o30h"]:
            if p is None:
                crossed = True
            elif p["o30h"] and p["price"]:
                crossed = p["price"] <= p["o30h"]
            else:
                crossed = False
            if crossed and rel5 is not None and rel5 >= IGNITE_VOL \
                    and pct is not None and pct >= PUMP_PCT:
                last["ignite"] = i

        # V型确认: 当日曾拉升≥3% + 近 lookback 分钟内曾跌破均价线 + 本分钟刚收回
        if pumped_ever and px and vwap and px > vwap:
            lo = max(0, i - V_SHAPE_LOOKBACK_MIN + 1)
            below = any((x["price"] and x["vwap"] and x["price"] < x["vwap"])
                        for x in rows[lo:i + 1])
            was_below = bool(p and p["price"] and p["vwap"] and p["price"] <= p["vwap"])
            if below and was_below:
                last["v_shape"] = i

        # 无量拉升诱多: 涨幅≥3% + 价>均价线 + 缩量（组合边沿）
        nvp = bool(pct is not None and pct >= PUMP_PCT and px and vwap and px > vwap
                   and rel5 is not None and rel5 < SHRINK_VOL)
        p_nvp = bool(p and p["pct_from_open"] is not None and p["pct_from_open"] >= PUMP_PCT
                     and p["price"] and p["vwap"] and p["price"] > p["vwap"]
                     and p["rel5"] is not None and p["rel5"] < SHRINK_VOL)
        if nvp and not p_nvp:
            last["no_vol_push"] = i

        # 量价背离: 运行最高价抬升(真创新高) + 缩量（组合边沿）
        vd = bool(px and r["day_high"] and rel5 is not None and rel5 < SHRINK_VOL
                  and p and p["day_high"] and r["day_high"] > p["day_high"])
        p_vd = bool(p and p["price"] and p["day_high"] and p["rel5"] is not None
                    and p["rel5"] < SHRINK_VOL
                    and i >= 2 and rows[i - 2]["day_high"]
                    and p["day_high"] > rows[i - 2]["day_high"])
        if vd and not p_vd:
            last["vol_divergence"] = i

    return {k: (None if v is None else (n - 1 - v)) for k, v in last.items()}
