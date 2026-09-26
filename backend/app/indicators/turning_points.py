"""蔡森·多空转折顾问 —— 确定性多形态深度分析(纯函数,无 IO)。

移植自 EvoFlow「蔡森·多空转折顾问」AGENT 的蔡森《多空转折一手抓》12型态方法论,
把原来只有「单主形态 + 三价」的简化版升级为**多形态同时识别 + 满足计算 +
波段结构 + 完整关键价位表**的深度分析,且全部为确定性算法(不需要大模型):

  1. 破底翻(招2/3): 前低 L1 → 破底 L2(L2<L1) → 快速收复站上颈线(两低间反弹高点)
  2. W底/双底(招1): 双底接近 + 颈线,突破后等幅上攻
  3. 向上假突破(招9/11): 前高 H1 → 放量突破 H2 → 爆量长黑/长上影 → 跌破颈线
  4. M头/双顶跌幅满足(招8/10): 双高 + 颈线,跌破后等幅下看
  5. 箱体震荡: 近60日窄幅区间
  6. 收敛三角形排除(招6/12): 高点降低 + 低点抬高,按纪律不硬套等幅
  7. 趋势: MA20/MA60 方向 + 现价位置

识别用**长回看窗口**(底部结构可达 200 根前、顶部结构 130 根前),
保证能覆盖「前高 → 破底 → 破底翻 → 第一波 → 回踩」的完整多空转折结构。

输出:
  {
    form: 主形态名, verdict: bull|bear|neutral, confidence, desc,
    summary, patterns: [多形态明细], waves: {波段结构},
    neckline/target/stop: 主形态三价(兼容旧接口),
    marks: [划线标注], points: [完整关键价位表, PriceLevel 兼容]
  }

NaN/Inf 全部过滤,空数据返回默认结构,不抛异常。
"""
from __future__ import annotations

import logging
import math
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

# 输出组 key(与 LEVEL_TYPES 对齐,前端按 key 渲染开关)
TURNING_TYPE = "turning"

# ---- 蔡森体系 v3.1 参数(2026-09-24 原书对齐,与 caisen-patterns skill 同源) ----
# 破底翻翻箱法: 第一目标 = 颈线C + (C − 破底低B); 第二目标 = 颈线C + 2×(C − B)(强量能才看)
_TARGET_MULT = 2.0          # 第二目标倍数(两倍翻箱)
_VOL_CONFIRM_RATIO = 1.5    # 放量定义: 突破量 ≥ 前N日均量 × 1.5
_VOL_LOOKBACK = 5           # 放量对比基准: 前5日均量
_BREAK_LOW_BUF = 0.015      # 止损缓冲: 破底低点B下方 1.5%
# 时间波(陈文图/台系): 前波下跌K棒数 × {0.618, 1.0, 1.618} 推变盘窗口
_TIMEWAVE_RATIOS = (0.618, 1.0, 1.618)


# ================================================================
# 工具
# ================================================================

def _ok(v: Any) -> bool:
    """数值有效(非空/非 NaN/非 Inf)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _f(v: Any) -> float | None:
    """安全转 float,无效返回 None。"""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _arr(df: pl.DataFrame, col: str) -> list[float]:
    """取一列数值(无效值过滤)。"""
    if col not in df.columns:
        return []
    return [x for x in (_f(v) for v in df[col].to_list()) if x is not None]


def _last(df: pl.DataFrame, col: str) -> float | None:
    """取列最后一个有效值。"""
    vals = _arr(df, col)
    return vals[-1] if vals else None


def _ma_value(df: pl.DataFrame, window: int) -> float | None:
    """均线现值:优先用预计算列,缺失则现场 rolling_mean。"""
    col = f"ma{window}"
    if col in df.columns:
        v = _last(df, col)
        if v is not None:
            return v
    if df.height >= window:
        v = df.select(pl.col("close").rolling_mean(window)).tail(1)["close"][0]
        return _f(v)
    return None


def _side(level: float, close: float) -> str:
    """价位相对当前价的方向。"""
    if level > close * 1.001:
        return "resistance"
    if level < close * 0.999:
        return "support"
    return "neutral"


def _round(v: float | None, n: int = 2) -> float | None:
    if v is None or not _ok(v):
        return None
    return round(v, n)


# ================================================================
# 蔡森体系 v3.1 工具(2026-09-24 原书对齐)
# ================================================================

def _vol_confirm(
    vols: list[float], idx: int,
    ratio: float = _VOL_CONFIRM_RATIO, lookback: int = _VOL_LOOKBACK,
) -> tuple[bool | None, str]:
    """放量突破验证: 突破那根的量能 ≥ 前 lookback 日均量 × ratio。

    返回 (状态, 说明): True=放量确认 / False=无量突破 / None=数据不足无法验证。
    """
    if idx < lookback or idx >= len(vols) or len(vols) != len(vols):
        return None, "量能数据不足,无法验证"
    prev = vols[idx - lookback:idx]
    cur = vols[idx]
    if not prev or max(prev) <= 0 or not _ok(cur) or cur <= 0:
        return None, "无成交量数据,无法验证"
    ma = sum(prev) / len(prev)
    r = cur / ma
    txt = f"突破量={cur:,.0f}, 前{lookback}日均量={ma:,.0f}, 倍数={r:.2f}x"
    if r >= ratio:
        return True, txt + f" ≥ {ratio}x → 放量确认"
    return False, txt + f" < {ratio}x → 无量突破"


def _time_wave(
    highs: list[float], lows: list[float], dates: list | None,
    high_idx: int, low_idx: int, base_idx: int | None = None,
    ratios: tuple = _TIMEWAVE_RATIOS,
) -> dict | None:
    """时间波(陈文图/台系): 数前一波下跌波段的K棒数, 用 0.618/1.0/1.618 倍推下一波变盘窗口。

    high_idx: 前波起跌高点索引; low_idx: 破底/转折低点索引(默认起算点)。
    base_idx: 可选二次起算点(如突破确认那根); 默认从低点起算。
    返回时间目标(窗口K棒数+对应日期)与当前进度; 概率性区间, 非精准某一天。
    """
    n = len(highs)
    if low_idx <= high_idx or high_idx < 0 or low_idx >= n:
        return None
    n_down = low_idx - high_idx + 1                  # 前波下跌K棒数(含两端)
    base = base_idx if (base_idx is not None and low_idx <= base_idx < n) else low_idx
    elapsed = n - 1 - base                           # 起算点以来已走K棒数
    wins: list[dict] = []
    for r in ratios:
        k = max(1, int(round(n_down * r)))
        idx = base + k
        d = str(dates[idx]) if dates and 0 <= idx < len(dates) else None
        wins.append({
            "ratio": r, "bars": k,
            "date": d if idx < n else f"(需再走{idx - (n - 1)}根)",
            "idx": idx, "reached": idx <= n - 1,
            "remain": max(0, idx - (n - 1)),
        })
    return {
        "down_bars": n_down,
        "down_from": (f"{dates[high_idx]}@{round(highs[high_idx], 2)}"
                      if dates and 0 <= high_idx < len(dates)
                      else f"idx{high_idx}@{round(highs[high_idx], 2)}"),
        "down_to": (f"{dates[low_idx]}@{round(lows[low_idx], 2)}"
                    if dates and 0 <= low_idx < len(dates)
                    else f"idx{low_idx}@{round(lows[low_idx], 2)}"),
        "base_date": (str(dates[base]) if dates and 0 <= base < len(dates) else f"idx{base}"),
        "elapsed": elapsed,
        "windows": wins,
        "note": "时间波为概率性时间窗口(区间非某日), 须搭配价格形态使用",
    }


def _timewave_resonance(
    price: float, tw: dict | None, target: float | None, target2: float | None = None,
) -> tuple[str, str]:
    """时价共振判定: 时间窗口到 + 价格抵达目标 = 时空共振(优先获利了结)。

    返回 (状态标签, 共振说明)。
    """
    if not tw:
        return "", ""
    elapsed = tw["elapsed"]
    near = None
    for w in tw["windows"]:
        if abs(elapsed - w["bars"]) <= max(1, int(round(w["bars"] * 0.1))):
            near = w
            break
    late = all(elapsed > w["bars"] + max(1, int(round(w["bars"] * 0.1))) for w in tw["windows"])
    price_hit = bool(target) and price >= target
    price_hit2 = bool(target2) and price >= target2
    tname = f"{near['ratio']}倍窗口({near['bars']}根)" if near else ""
    if price_hit and near:
        return "时空共振", (f"⚠️ 时间到({elapsed}根≈{tname}) + 价格已达目标({target}) → "
                            f"优先获利了结/减仓")
    if price_hit:
        return "价到时应未到", (f"价格已到目标({target})但时间未到窗口(已走{elapsed}根, "
                                f"下一窗口{tw['windows'][1]['bars']}根) → 易横盘震荡, 不一定马上反转")
    if near:
        return "时间到价未到", (f"时间窗口已到({elapsed}根≈{tname})但价格未达目标({target}) → "
                                f"行情偏强, 大概率继续延伸")
    if late:
        return "超时", (f"已走{elapsed}根, 超过最大窗口{tw['windows'][-1]['bars']}根仍未见共振 → "
                        f"时间波参考性下降, 以价格结构为准")
    return "未到窗口", (f"已走{elapsed}根, 距最近窗口({tw['windows'][0]['bars']}根)尚有"
                        f"{tw['windows'][0]['bars'] - elapsed}根")


def _pattern_status(
    price: float, direction: str, target: float | None, stop: float | None,
    break_idx: int, highs: list[float], lows: list[float],
    force_invalid: str | None = None,
) -> tuple[str, str]:
    """三态判定(原书: 到第一满足多会整理做了结; 破止损即失效)。

    返回 (group, label): group ∈ {'valid','done','invalid'}。
    """
    if force_invalid:
        return "invalid", f"已失效({force_invalid})"
    sig = "多头买进" if direction == "bull" else "空单进场"
    b = break_idx if (isinstance(break_idx, int) and 0 <= break_idx < len(highs)) else len(highs) - 1
    if sig == "多头买进":
        if stop and price < stop:
            return "invalid", "已失效(跌破止损)"
        if target and any(h >= target for h in highs[b:]):
            return "done", "已完成(已达第一满足)"
    else:
        if stop and price > stop:
            return "invalid", "已失效(涨破止损)"
        if target and any(l <= target for l in lows[b:]):
            return "done", "已完成(已达第一满足)"
    return "valid", "有效"


# ================================================================
# 摆动点提取
# ================================================================

def _swing_highs(arr: list[float], window: int = 3) -> list[tuple[int, float]]:
    """局部高点 (idx, value)。"""
    out: list[tuple[int, float]] = []
    for i in range(window, len(arr) - window):
        w = arr[i - window:i + window + 1]
        if arr[i] == max(w):
            out.append((i, arr[i]))
    return out


def _swing_lows(arr: list[float], window: int = 3) -> list[tuple[int, float]]:
    """局部低点 (idx, value)。"""
    out: list[tuple[int, float]] = []
    for i in range(window, len(arr) - window):
        w = arr[i - window:i + window + 1]
        if arr[i] == min(w):
            out.append((i, arr[i]))
    return out


def _merge(points: list[tuple[int, float]], tol: float = 0.03, gap: int = 10) -> list[tuple[int, float]]:
    """合并相近极值点(价差 < tol 且间隔 < gap 视为同一结构点,保留更极端/最新)。"""
    if not points:
        return []
    out: list[tuple[int, float]] = []
    for i, v in points:
        if out and abs(v - out[-1][1]) / max(out[-1][1], 1e-9) < tol and i - out[-1][0] < gap:
            out[-1] = (i, v)
        else:
            out.append((i, v))
    return out


# ================================================================
# 多形态识别(长窗口)
# ================================================================

def _detect_break_below_reverse(
    highs: list[float], lows: list[float], closes: list[float],
    price: float, n: int, vols: list[float] | None = None,
    dates: list | None = None,
) -> dict | None:
    """破底翻(招2/3, v3.1 原书对齐):前低 L1 → 破底 L2(L2 < L1) → 快速收复站上颈线。

    关键:破底低点 L2 是**历史真实底部**(最近 200 根内最低摆动低点),
    而非当前回踩低点——破底翻确认后的回踩(未破 L2)正是健康结构。
    颈线 = L1 与 L2 之间的反弹高点。

    v3.1 (2026-09-24) 更新:
      - 价格目标改用**翻箱法**: 第一 = C+(C−B)、第二 = C+2×(C−B)(强量能才看)
      - **放量前提**: 突破颈线必须放量(≥前5日均量×1.5); 无量突破 → 形态失效, target 置空
      - **时间波**: 数前波下跌(颈线→破底B)K棒数, 0.618/1.0/1.618 推变盘窗口 → 时价共振
      - 止损 = L2 × (1 − 1.5%)缓冲, 标注与 5~7% 纪律带关系
    确认:现价 > 颈线(或已收盘站上颈线)。
    """
    if n < 40:
        return None
    lows_ex = _merge(_swing_lows(lows, 3), tol=0.04, gap=12)
    if len(lows_ex) < 2:
        return None
    # 破底低点 L2 = 最近 200 根内最低摆动低点(真实底部)
    recent_lows = [(i, v) for i, v in lows_ex if i >= n - 200]
    if not recent_lows:
        return None
    l2 = min(recent_lows, key=lambda x: x[1])
    if l2[0] < n - 190:
        return None  # 底部太老,破底翻结构已不适用
    # 前低 L1 = L2 之前的摆动低点中,最近一个高于 L2 的(破底前的位置)
    l1 = None
    for i, v in reversed(lows_ex):
        if i < l2[0] and v > l2[1] * 1.01:
            l1 = (i, v)
            break
    if l1 is None:
        return None
    gap = l2[0] - l1[0]
    if not (3 <= gap <= 120):
        return None
    # 颈线 = L1 与 L2 之间的最高点
    seg = highs[l1[0]:l2[0] + 1]
    neck_idx = l1[0] + int(max(range(len(seg)), key=lambda i: seg[i]))
    neck = highs[neck_idx]
    if not (neck > l2[1] and neck > l1[1]):
        return None
    # 破底翻必须:颈线明显高于破底低点(>=3%)
    if (neck - l2[1]) / neck < 0.03:
        return None

    # ---- v3.1 翻箱法价格目标 ----
    height = neck - l2[1]                          # 翻箱高度 = 颈线 − 破底最低点
    t1 = neck + height                             # 第一目标(翻箱, 最常用)
    t2 = neck + _TARGET_MULT * height              # 第二目标(两倍翻箱, 需强量能)

    # 突破确认: L2 之后第一根收盘站上颈线 → 放量前提(无量突破 → 失效)
    brk_idx = next((i for i in range(l2[0] + 1, n) if closes[i] > neck), None)
    confirmed = price > neck
    vol_ok: bool | None = None
    vol_txt = ""
    if brk_idx is not None and vols and len(vols) == n:
        vol_ok, vol_txt = _vol_confirm(vols, brk_idx)

    # 放量前提: 无量突破 → 形态失效, 不看目标
    force_invalid: str | None = None
    if confirmed and brk_idx is not None and vol_ok is False:
        force_invalid = "突破颈线无量(破底翻失效)"
        t1 = t2 = None
    elif brk_idx is None:
        # 尚未突破: 保留结构但标注未确认, 不给目标(避免拿未突破的价格当锚)
        force_invalid = None
        t1 = t2 = None

    # v3.1 止损: 破底低点B下方 1.5% 缓冲(标注与 5~7% 纪律带关系)
    stop_b = l2[1] * (1 - _BREAK_LOW_BUF)
    stop_basis = f"破底低点B({l2[1]:.2f})下方{_BREAK_LOW_BUF:.1%}缓冲"
    if brk_idx is not None and _ok(closes[brk_idx]):
        dist = (closes[brk_idx] - stop_b) / closes[brk_idx]
        stop_basis += (f"; 距突破价{dist:.1%}" +
                       ("超出5~7%纪律带→应减仓位或按纪律提前离场" if dist > 0.07 else "在5~7%纪律带内"))

    # v3.1 时间波: 前波下跌 = 颈线高点 → 破底低点B, 从B起算
    tw = _time_wave(highs, lows, dates, neck_idx, l2[0])
    res_label, res_txt = _timewave_resonance(price, tw, t1, t2)

    # 诚实性字段: 突破后已走根数 / 现价是否仍守颈线
    signal_age = (n - 1 - brk_idx) if brk_idx is not None else 0
    neckline_hold = price >= neck

    desc_parts = [f"破底翻{'确认' if confirmed else '确认期'}:前低 {l1[1]:.2f} → 破底 {l2[1]:.2f} → "
                  f"站上颈线 {neck:.2f}"]
    if t1 is not None:
        desc_parts.append(f"翻箱第一目标 {t1:.2f}(第二 {t2:.2f})")
    if force_invalid:
        desc_parts.append("无量突破→失效")
    desc = "; ".join(desc_parts)

    out = {
        "key": "break_below_reverse", "name": "破底翻", "direction": "bull",
        "confirmed": confirmed, "neckline": neck, "bottom": l2[1],
        "target": t1, "target2": t2,
        "target_basis": "第一=C+(C−B) 翻箱; 第二=C+2×(C−B) 两倍翻箱(需强量能)",
        "box_height": round(height, 2),
        "break_price": round(closes[brk_idx], 2) if (brk_idx is not None and _ok(closes[brk_idx])) else None,
        "add_level": round(neck, 2),
        "stop_loss": round(stop_b, 2), "stop_structure": round(stop_b, 2), "stop_basis": stop_basis,
        "vol_confirm": vol_ok, "vol_note": vol_txt,
        "signal_age": signal_age, "neckline_hold": neckline_hold,
        "break_idx": brk_idx if brk_idx is not None else n - 1,
        "desc": desc,
        "marks": [
            {"yAxis": neck, "name": f"破底翻颈线 {neck:.2f}", "style": "orange"},
            {"yAxis": t1, "name": f"满足点 {t1:.2f}", "style": "purple"} if t1 else None,
            {"yAxis": l2[1], "name": f"破底低 {l2[1]:.2f}", "style": "green"},
            {"yAxis": l1[1], "name": f"前低 {l1[1]:.2f}", "style": "green"},
        ],
    }
    if not neckline_hold:
        out["weak_note"] = (f"现价 {price:.2f} 已回落至颈线C({neck:.2f})下方, "
                            f"突破回测加深 → 信号转弱(止损仍以破底低点B为准)")
    if tw:
        out["time_wave"] = tw
        out["time_wave_status"] = res_label
        out["time_wave_note"] = res_txt
    if brk_idx is not None and tw:
        max_win = tw["windows"][-1]["bars"]
        if signal_age > max_win:
            out["stale_note"] = (f"突破已过 {signal_age} 根, 超过最长时间波窗口, "
                                 f"信号新鲜度下降, 优先看更近期的形态")
    if force_invalid:
        out["force_invalid"] = force_invalid
        out["target"] = None
        out["target2"] = None
        out["confidence"] = 0.40
    else:
        out["confidence"] = 0.75
    out["marks"] = [m for m in out["marks"] if m is not None]
    return out


def _detect_double_bottom(
    highs: list[float], lows: list[float], closes: list[float], price: float, n: int,
) -> dict | None:
    """W底/双底(招1):两底接近 + 颈线(两底间高点),突破后等幅上攻。

    回看窗口:最近 200 根内的底部结构;要求右底在最近 100 根内。
    注意:两底不能用 merge 合并(真正的双底是两个独立摆动低点)。
    """
    if n < 40:
        return None
    lows_ex = _swing_lows(lows, 3)
    if len(lows_ex) < 2:
        return None
    # 取最近两个摆动低点作为双底候选
    l1, l2 = lows_ex[-2], lows_ex[-1]
    if not (l1[0] >= n - 200 and l2[0] >= n - 100):
        return None
    gap = l2[0] - l1[0]
    if not (3 <= gap <= 60):
        return None
    bot1, bot2 = l1[1], l2[1]
    if abs(bot2 - bot1) / max(bot1, 1e-9) >= 0.05:
        return None
    seg = highs[l1[0]:l2[0] + 1]
    neck_idx = l1[0] + int(max(range(len(seg)), key=lambda i: seg[i]))
    neck = highs[neck_idx]
    bottom = min(bot1, bot2)
    depth = neck - bottom
    if depth <= 0:
        return None
    target = neck + depth
    confirmed = price > neck
    return {
        "key": "double_bottom", "name": "W底/双底", "direction": "bull",
        "confirmed": confirmed, "neckline": neck, "bottom": bottom, "target": target,
        "desc": (
            f"W底/双底{'突破' if confirmed else '确认期'}:双底 {bottom:.2f},"
            f"颈线 {neck:.2f},等幅满足 {target:.2f}"
        ),
        "marks": [
            {"yAxis": neck, "name": f"W底颈线 {neck:.2f}", "style": "orange"},
            {"yAxis": target, "name": f"满足点 {target:.2f}", "style": "purple"},
            {"yAxis": bottom, "name": f"W底 {bottom:.2f}", "style": "green"},
        ],
    }


def _detect_fake_breakout(
    highs: list[float], lows: list[float], closes: list[float], vols: list[float],
    price: float, n: int,
) -> dict | None:
    """向上假突破(招9/11,诱多):前高 H1 → 放量突破 H2 → 见顶信号 → 跌回颈线。

    回看窗口 200 根;突破高必须在最近 40 根内(结构有现实意义)。
    """
    if n < 80 or len(vols) != n:
        return None
    look = min(200, n)
    seg_hi = highs[-look:]
    seg_lo = lows[-look:]
    seg_cl = closes[-look:]
    seg_vol = vols[-look:]
    base = n - look

    cand: list[tuple[int, float]] = []
    for i in range(3, len(seg_hi) - 3):
        w = seg_hi[i - 3:i + 4]
        if seg_hi[i] == max(w):
            cand.append((base + i, seg_hi[i]))
    if len(cand) < 2:
        return None
    prev_h, brk_h = cand[-2], cand[-1]
    if brk_h[0] < n - 60:  # 突破高点不能太老(否则是历史,不是当前假突破)
        return None
    if brk_h[1] < prev_h[1] * 1.02:
        return None
    gap = brk_h[0] - prev_h[0]
    if not (5 <= gap <= 150):
        return None
    # 突破日放量
    bi = brk_h[0] - base
    avg_vol = sum(seg_vol[max(0, bi - 20):bi]) / max(bi, 1)
    if avg_vol <= 0 or seg_vol[bi] < avg_vol * 1.5:
        return None
    # 突破后见顶信号(爆量长黑/长上影)
    top_signal = False
    for j in range(bi + 1, min(bi + 15, len(seg_hi))):
        c = seg_cl[j]; h = seg_hi[j]; l = seg_lo[j]
        o_approx = (h + l + c) / 3
        body = abs(c - o_approx)
        upper = h - max(c, o_approx)
        lower = min(c, o_approx) - l
        if (upper >= 2 * max(body, 1e-9) and upper >= 2 * lower) \
                or (c < h * 0.97 and seg_vol[j] >= avg_vol * 1.5):
            top_signal = True
            break
    if not top_signal:
        return None
    # 颈线 = 前高;跌破确认
    neck = prev_h[1]
    confirmed = price < neck
    target = neck - (brk_h[1] - neck)
    return {
        "key": "fake_breakout", "name": "向上假突破", "direction": "bear",
        "confirmed": confirmed, "top": brk_h[1], "neckline": neck, "target": target,
        "desc": (
            f"向上假突破{'确认' if confirmed else '嫌疑'}:前高 {neck:.2f} 放量突破至 "
            f"{brk_h[1]:.2f} 后见顶,跌破颈线则回补至 {target:.2f}"
        ),
        "marks": [
            {"yAxis": neck, "name": f"前高/颈线 {neck:.2f}", "style": "orange"},
            {"yAxis": brk_h[1], "name": f"假突破高 {brk_h[1]:.2f}", "style": "red"},
            {"yAxis": target, "name": f"回补目标 {target:.2f}", "style": "purple"},
        ],
    }


def _detect_double_top(
    highs: list[float], lows: list[float], closes: list[float], price: float, n: int,
) -> dict | None:
    """M头/双顶(招8/10):双高接近 + 颈线(两高间低点),跌破后等幅下看。

    回看窗口 130 根;第二顶在最近 80 根内。
    """
    if n < 40:
        return None
    look = min(130, n)
    seg_h = highs[-look:]
    seg_l = lows[-look:]
    base = n - look
    cand: list[tuple[int, float]] = []
    for i in range(3, len(seg_h) - 3):
        w = seg_h[i - 3:i + 4]
        if seg_h[i] == max(w):
            cand.append((base + i, seg_h[i]))
    merged: list[tuple[int, float]] = []
    for i, v in cand:
        if merged and (i - merged[-1][0] < 2 or abs(v - merged[-1][1]) / max(merged[-1][1], 1e-9) < 0.005):
            if v > merged[-1][1]:
                merged[-1] = (i, v)
            continue
        merged.append((i, v))
    if len(merged) < 2:
        return None
    h1, h2 = merged[-2], merged[-1]
    if h2[0] < n - 80:  # 第二顶太老 → 不是当前关注的双顶
        return None
    gap = h2[0] - h1[0]
    # 真 M头:两顶间隔 >=8 根(需有回撤换手),避免把相邻两根小高点误判
    if not (8 <= gap <= 60):
        return None
    if abs(h2[1] - h1[1]) / max(h1[1], 1e-9) >= 0.06:
        return None
    seg = lows[h1[0]:h2[0] + 1]
    neck_idx = h1[0] + int(min(range(len(seg)), key=lambda i: seg[i]))
    neck = lows[neck_idx]
    top = max(h1[1], h2[1])
    # 颈线必须有足够深度(>=5%),否则只是小回撤不是 M头
    if (top - neck) / top < 0.05:
        return None
    # 显著性过滤:双顶必须是「近期真实顶部」——较高峰接近近 40 根最高点,
    # 且较低峰也足够接近较高峰(避免把中段小高点误判成 M头)。
    recent_hi = max(highs[-40:]) if n >= 40 else max(highs)
    if top < recent_hi * 0.97:
        return None
    low_peak = min(h1[1], h2[1])
    if low_peak < top * 0.93:
        return None
    target = neck - (top - neck)
    confirmed = price < neck
    return {
        "key": "double_top", "name": "M头/双顶", "direction": "bear",
        "confirmed": confirmed, "neckline": neck, "top": top, "target": target,
        "desc": (
            f"M头/双顶{'破位' if confirmed else '嫌疑'}:双高 {top:.2f},"
            f"颈线 {neck:.2f},跌破后下看 {target:.2f}"
        ),
        "marks": [
            {"yAxis": neck, "name": f"M头颈线 {neck:.2f}", "style": "orange"},
            {"yAxis": top, "name": f"双高 {top:.2f}", "style": "red"},
            {"yAxis": target, "name": f"下看目标 {target:.2f}", "style": "purple"},
        ],
    }


def _detect_box(highs: list[float], lows: list[float], price: float) -> dict | None:
    """箱体震荡:近60日振幅 < 18%。"""
    recent_h = max(highs[-60:])
    recent_l = min(lows[-60:])
    if recent_l > 0 and (recent_h - recent_l) / recent_l < 0.18:
        return {
            "key": "box", "name": "箱体震荡", "direction": "neutral",
            "confirmed": True, "neckline": recent_h, "bottom": recent_l,
            "target": None,
            "desc": f"近60日箱体 {recent_l:.2f}-{recent_h:.2f},等待方向选择",
            "marks": [
                {"yAxis": recent_h, "name": f"箱顶 {recent_h:.2f}", "style": "red"},
                {"yAxis": recent_l, "name": f"箱底 {recent_l:.2f}", "style": "green"},
            ],
        }
    return None


def _detect_triangle(highs: list[float], lows: list[float], closes: list[float], n: int, dates: list | None = None) -> dict | None:
    """收敛三角形(招6/12):近 120 根内「高点序列逐级降低 + 低点整体不再创新低」。

    识别(务实口径,适配确定性算法):
      - 取最近 120 根的 swing 高点,合并相近后,最近 3~4 个高点**严格逐级下降**
        (688553 例:14.28→13.74→12.99→12.56),构成上缘;
      - 下缘取「相邻高点之间的最低点」(每段下跌的终点),要求整体**不创新低**
        (允许平台小幅波动,如 9.96→10.61),构成下缘;
      - 上下缘间距仍为正(未完全收敛)。

    输出:上缘/下缘斜线锚点(供 K 线画斜线)、上下缘斜率、当日动态上下缘价、
    收敛尖点、当日三角形高度。按 12 招纪律不硬套等幅,但提供突破位参考。
    """
    if n < 60:
        return None
    look = min(120, n)
    seg_hi = highs[-look:]
    seg_lo = lows[-look:]
    base = n - look

    # swing 高点(合并相近)
    sh = _merge(_swing_highs(seg_hi, 3), tol=0.02, gap=8)
    if len(sh) < 3:
        return None
    # 最近 3~4 个高点:严格逐级下降(允许 +0.5% 毛刺)
    h4 = sh[-4:] if len(sh) >= 4 else sh[-3:]
    h_use = h4[-3:]  # 用最近 3 个高点画上缘
    if not all(h_use[i][1] <= h_use[i - 1][1] * 1.005 for i in range(1, len(h_use))):
        return None
    hi_drop = (h_use[0][1] - h_use[-1][1]) / h_use[0][1]
    if not (0.015 <= hi_drop <= 0.20):
        return None

    # 下缘:相邻高点之间的最低点(每段下跌终点)
    lows_seg: list[tuple[int, float]] = []
    for k in range(len(h4) - 1):
        a, b = h4[k][0], h4[k + 1][0]
        seg = seg_lo[a:b + 1]
        min_i = int(min(range(len(seg)), key=lambda i: seg[i]))
        lows_seg.append((base + a + min_i, seg[min_i]))
    # 追加:最后一个高点之后到现在的最低点(当前段)
    last_h = h4[-1][0]
    seg_tail = seg_lo[last_h:]
    if seg_tail:
        min_i = int(min(range(len(seg_tail)), key=lambda i: seg_tail[i]))
        lows_seg.append((base + last_h + min_i, seg_tail[min_i]))
    if len(lows_seg) < 3:
        return None
    # 用最近 3 个下缘点:三角形下缘本质是「回踩低点整体抬高」,
    # 允许中段平台凸起(如 688553 的 8月平台),只要求「末点不低于首点」+「不逐级创新低」
    l_use = lows_seg[-3:]
    # 末点相对首点不能更低(整体不创新低)
    if l_use[-1][1] < l_use[0][1] * 0.99:
        return None
    # 允许中段凸起,但最后一个下缘点应接近或高于首点(收敛下缘抬升)
    lo_rise = (l_use[-1][1] - l_use[0][1]) / l_use[0][1]
    if lo_rise < -0.015:
        return None

    # 上下缘间距(最后一个高点到最后一个低点)
    top_gap = h_use[-1][1] - l_use[-1][1]
    if top_gap <= 0:
        return None

    # 斜线锚点(绝对索引 + 日期,供 K 线对齐)。
    # 注意:h_use 是相对 seg 的索引(需 +base),l_use 已是绝对索引(不加)。
    def _pt(abs_idx: int, value: float) -> dict:
        d = str(dates[abs_idx]) if dates and 0 <= abs_idx < len(dates) else str(abs_idx)
        return {"idx": abs_idx, "date": d, "value": round(value, 2)}

    upper_line = [_pt(base + i, v) for i, v in h_use]
    lower_line = [_pt(i, v) for i, v in l_use]

    # 斜线计算统一用绝对索引
    up_pts = [(base + i, v) for i, v in h_use]
    lo_pts = [(i, v) for i, v in l_use]

    def _slope(points):
        if len(points) < 2:
            return 0.0
        (x1, y1), (x2, y2) = points[0], points[-1]
        return (y2 - y1) / (x2 - x1) if x2 != x1 else 0.0

    up_slope = _slope(up_pts)
    lo_slope = _slope(lo_pts)
    last_idx = n - 1

    def _proj(points, slope):
        (x1, y1) = points[-1]
        return y1 + slope * (last_idx - x1)

    dyn_up = _proj(up_pts, up_slope)
    dyn_lo = _proj(lo_pts, lo_slope)
    # 收敛尖点(两线交点)
    (hx1, hy1), (hx2, hy2) = up_pts[0], up_pts[-1]
    (lx1, ly1), (lx2, ly2) = lo_pts[0], lo_pts[-1]
    s1 = (hy2 - hy1) / (hx2 - hx1) if hx2 != hx1 else 0
    s2 = (ly2 - ly1) / (lx2 - lx1) if lx2 != lx1 else 0
    apex_idx = apex_val = None
    if abs(s1 - s2) > 1e-9:
        apex_idx = (ly1 - hy1 - s2 * lx1 + s1 * hx1) / (s1 - s2)
        apex_val = hy1 + s1 * (apex_idx - hx1)
    height = dyn_up - dyn_lo
    price = closes[-1]
    broke_upper = price > dyn_up
    # v3.1 位置过滤(原书): 有效突破须在三角形 1/2~3/4 行程处;
    # 行程比 = (当前/突破根 − 起点) / (收敛尖点 − 起点); 尾端(>0.85)突破易失败反转。
    tri_start = min(up_pts[0][0], lo_pts[0][0])
    apex_i = float(apex_idx) if apex_idx is not None else None
    break_position: str | None = None
    if apex_i is not None and apex_i > tri_start + 2:
        ratio = (last_idx - tri_start) / (apex_i - tri_start)
        if broke_upper:
            if ratio < 0.30:
                break_position = f"行程{ratio * 100:.0f}%(不足1/2行程,形态未成熟,突破不可信)"
            elif ratio > 0.85:
                break_position = f"行程{ratio * 100:.0f}%(尾端突破易失败反转)"
            else:
                break_position = f"行程{ratio * 100:.0f}%(1/2~3/4有效区)"
        else:
            break_position = f"行程{ratio * 100:.0f}%(未突破)"
    # 双口径满足位(对齐 EvoFlow 报告的「动态 vs 静态」说明):
    #   动态口径 = 突破位(现价/动态上缘) + 当日三角形高度 → 突破初期第一目标(近)
    #   静态口径 = 突破颈线 + 全高(最后一个高点到下缘最低点的垂直距离) → 站上颈线后的主升目标(远)
    bounce_base = price if broke_upper else dyn_up
    dyn_target = bounce_base + height
    full_height = h_use[-1][1] - min(v for _, v in l_use)
    static_target = h_use[-1][1] + full_height if full_height > 0 else None

    return {
        "key": "triangle", "name": "收敛三角形", "direction": "neutral",
        "confirmed": broke_upper,  # 突破上缘视为向上表态(未确认,3日看; 且须过位置过滤)
        "neckline": round(dyn_up, 2), "bottom": round(dyn_lo, 2),
        "target": None,
        "upper_line": upper_line, "lower_line": lower_line,
        "up_slope": round(up_slope, 4), "lo_slope": round(lo_slope, 4),
        "dyn_up": round(dyn_up, 2), "dyn_lo": round(dyn_lo, 2),
        "apex_idx": round(apex_idx, 1) if apex_idx else None,
        "apex_val": round(apex_val, 2) if apex_val else None,
        "height": round(height, 2),
        "dyn_target": round(dyn_target, 2),
        "static_target": round(static_target, 2) if static_target else None,
        "break_position": break_position,
        "desc": (
            f"收敛三角形:上缘高连高 {h_use[0][1]:.2f}→{h_use[-1][1]:.2f}(斜率{up_slope:+.4f}/天),"
            f"下缘低连低 {l_use[0][1]:.2f}→{l_use[-1][1]:.2f}(斜率{lo_slope:+.4f}/天),"
            f"当日动态上缘 {dyn_up:.2f}{'(已突破)' if broke_upper else '(未破)'},"
            f"动态满足 {dyn_target:.2f} / 静态满足 {static_target:.2f},"
            f"收敛尖点约 idx{apex_idx:.0f}/价 {apex_val:.2f}"
            + (f"; {break_position}" if break_position else "")
        ),
        "marks": [
            {"yAxis": round(dyn_up, 2), "name": f"上缘 {dyn_up:.2f}", "style": "orange"},
            {"yAxis": round(dyn_lo, 2), "name": f"下缘 {dyn_lo:.2f}", "style": "green"},
        ],
    }


def _detect_trend(ma20: float | None, ma60: float | None, price: float) -> dict:
    """趋势:MA20/MA60 方向 + 现价位置。"""
    if ma20 and ma60:
        if ma20 > ma60 and price > ma20:
            return {
                "key": "trend", "name": "多头趋势", "direction": "bull",
                "confirmed": True, "neckline": None, "target": None,
                "desc": f"多头趋势:MA20({ma20:.2f})>MA60({ma60:.2f}),现价站上MA20",
                "marks": [],
            }
        if ma20 < ma60 and price < ma20:
            return {
                "key": "trend", "name": "空头趋势", "direction": "bear",
                "confirmed": True, "neckline": None, "target": None,
                "desc": f"空头趋势:MA20({ma20:.2f})<MA60({ma60:.2f}),现价在MA20下方",
                "marks": [],
            }
    return {
        "key": "trend", "name": "趋势整理", "direction": "neutral",
        "confirmed": False, "neckline": None, "target": None,
        "desc": "趋势整理:MA20/MA60 纠结,方向未明",
        "marks": [],
    }


# ================================================================
# 波段结构(第一波 / 回踩 / 第二波等幅)
# ================================================================

def _wave_structure(
    lows: list[float], highs: list[float],
    patterns: list[dict], price: float,
) -> dict:
    """从多形态中提取波段结构:底部 → 第一波高点 → 回踩低点 → 第二波等幅。"""
    out: dict = {"available": False}
    bottom_candidates = []
    for p in patterns:
        if p["key"] in ("double_bottom", "break_below_reverse") and p.get("bottom"):
            bottom_candidates.append(p["bottom"])
        if p["key"] == "double_bottom" and p.get("target"):
            out["w_bottom_target"] = _round(p["target"])
        if p["key"] == "break_below_reverse" and p.get("target"):
            out["break_reverse_target"] = _round(p["target"])
    if not bottom_candidates:
        return out
    bottom = min(bottom_candidates)
    # 底部之后的第一波高点 = 底部之后到现在的最高点
    b_idx = min((i for i, v in enumerate(lows) if abs(v - bottom) / max(bottom, 1e-9) < 0.02), default=None)
    if b_idx is None:
        return out
    after_highs = highs[b_idx:]
    if not after_highs:
        return out
    first_high = max(after_highs)
    hi_idx = b_idx + after_highs.index(first_high)
    height = first_high - bottom
    # 回踩低点 = 第一波高点之后的最低点(未破底部)
    pullback = min(lows[hi_idx:]) if hi_idx < len(lows) - 1 else bottom
    second_target = pullback + height
    # 保守目标:第一波只算到第一个明显高点(若第二高点更高则不适用)
    out.update({
        "available": True,
        "bottom": _round(bottom),
        "first_wave_high": _round(first_high),
        "first_wave_height": _round(height, 3),
        "pullback_low": _round(pullback),
        "second_wave_target": _round(second_target),
    })
    return out


# ================================================================
# 主入口
# ================================================================

def compute_turning_analysis(df: pl.DataFrame) -> dict:
    """蔡森·多空转折分析(多形态深度版,主入口)。

    输入: 含 OHLCV 的 polars 日 K DataFrame(建议 ≥250 根覆盖完整结构)。
    输出:
      {
        form, verdict, confidence, desc, summary,
        patterns: [多形态明细], waves: {波段结构},
        neckline/target/stop: 主形态三价(兼容旧接口),
        marks: [划线标注], points: [完整关键价位表]
      }
    空数据返回全默认结构。
    """
    empty = {
        "form": "无数据", "verdict": "neutral", "neckline": None, "target": None,
        "stop": None, "confidence": "低", "desc": "无 K 线数据", "note": "",
        "marks": [], "points": [], "patterns": [], "waves": {"available": False},
        "lines": [], "discipline": {"entry": [], "add": [], "reduce": [], "stop": [],
                                    "rules": []},
        "summary": "无 K 线数据",
    }
    if df.is_empty() or "close" not in df.columns:
        return empty

    try:
        highs = _arr(df, "high")
        lows = _arr(df, "low")
        closes = _arr(df, "close")
        vols = _arr(df, "volume") if "volume" in df.columns else []
        price = _last(df, "close")
        if price is None or not closes:
            return empty
        n = len(closes)

        ma20 = _ma_value(df, 20)
        ma60 = _ma_value(df, 60)

        # ---- 多形态同时识别(长窗口) ----
        patterns: list[dict] = []
        dates_arr = df["date"].to_list() if "date" in df.columns else None
        p_break_reverse = _detect_break_below_reverse(highs, lows, closes, price, n, vols, dates_arr)
        p_double_bottom = _detect_double_bottom(highs, lows, closes, price, n)
        p_fake_breakout = _detect_fake_breakout(highs, lows, closes, vols, price, n)
        p_double_top = _detect_double_top(highs, lows, closes, price, n)
        p_box = _detect_box(highs, lows, price)
        p_triangle = _detect_triangle(highs, lows, closes, n, dates_arr)
        p_trend = _detect_trend(ma20, ma60, price)

        for p in (p_break_reverse, p_double_bottom, p_fake_breakout, p_double_top, p_box, p_triangle, p_trend):
            if p:
                # v3.1 三态判定: 有效 / 已完成(已达第一满足) / 已失效(破止损)
                p["status"], p["status_label"] = _pattern_status(
                    price, p["direction"], p.get("target"), p.get("stop_loss") or p.get("stop"),
                    p.get("break_idx", n - 1), highs, lows, p.get("force_invalid"),
                )
                patterns.append(p)

        # ---- 波段结构 ----
        waves = _wave_structure(lows, highs, patterns, price)

        # ---- 主形态:优先选「已确认」的形态;未确认的结构形态只在
        # 没有其他已确认形态时才作为提示展示 ----
        primary = None
        # 第一优先:已确认的底部/顶部结构 + 已确认的箱体/趋势
        for p in (p_break_reverse, p_double_bottom, p_fake_breakout, p_double_top, p_box, p_trend):
            if p and p.get("confirmed"):
                primary = p
                break
        # 第二优先:未确认的结构形态(仍提示潜在结构,但排在已确认形态之后)
        if primary is None:
            for p in (p_break_reverse, p_double_bottom, p_fake_breakout, p_double_top):
                if p:
                    primary = p
                    break
        # 第三优先:未确认的箱体/趋势
        if primary is None:
            for p in (p_box, p_trend):
                if p:
                    primary = p
                    break
        form = primary["name"] if primary else "趋势"

        # ---- 多空结论:看「当前价格处于什么结构」,而非简单数形态 ----
        # 破底翻/W底确认 + 现价在颈线上方 → 多(核心多空转折,优先);
        # M头/假突破确认 + 现价在颈线下方 → 空(历史头部结构若仍压制则转空);
        # 底部结构确认后,历史头部跌幅满足已兑现 → 以底部结构方向为准。
        bull_confirmed = any(
            p["key"] in ("double_bottom", "break_below_reverse") and p["confirmed"] for p in patterns
        )
        bear_confirmed = any(
            p["key"] in ("double_top", "fake_breakout") and p["confirmed"] for p in patterns
        )
        # 破底翻/W底底部结构确认 = 空转多(核心);当前价站上颈线即多头阶段
        if bull_confirmed:
            verdict = "bull"
        elif bear_confirmed:
            verdict = "bear"
        else:
            # 都未确认:看趋势 + 波段
            trend_bull = any(p["key"] == "trend" and p["direction"] == "bull" for p in patterns)
            trend_bear = any(p["key"] == "trend" and p["direction"] == "bear" for p in patterns)
            if trend_bull:
                verdict = "bull"
            elif trend_bear:
                verdict = "bear"
            else:
                verdict = "neutral"

        # ---- 关键价位表(综合所有形态 + 波段 + 均线 + 60日高低) ----
        points = _collect_points(patterns, waves, df, price)
        # 主形态三价(兼容旧接口)
        neckline = primary.get("neckline") if primary else None
        target = primary.get("target") if primary else None
        stop = _compute_stop(patterns)

        desc = _build_desc(patterns, waves, price, verdict)
        summary = _build_summary(form, verdict, patterns, waves, price)
        confidence = _confidence(patterns, waves)
        discipline = _build_discipline(patterns, waves, form, verdict, price, ma20, ma60)

        # 合并所有划线标注(水平线:颈线/目标/止损等)
        marks: list[dict] = []
        seen_mark: set[float] = set()
        for p in patterns:
            for m in p.get("marks", []):
                v = round(m["yAxis"], 2)
                if v in seen_mark:
                    continue
                seen_mark.add(v)
                marks.append(m)

        # 斜线(收敛三角形上下缘连线),供 K 线画趋势斜线
        lines: list[dict] = []
        for p in patterns:
            if p["key"] == "triangle" and p.get("upper_line") and p.get("lower_line"):
                lines.append({
                    "id": "tri_upper", "name": "三角形上缘", "color": "#F97316",
                    "kind": "downtrend", "points": p["upper_line"],
                })
                lines.append({
                    "id": "tri_lower", "name": "三角形下缘", "color": "#22C55E",
                    "kind": "uptrend", "points": p["lower_line"],
                })

        return {
            "form": form, "verdict": verdict,
            "neckline": _round(neckline), "target": _round(target), "stop": _round(stop),
            "confidence": confidence, "desc": desc, "note": "",
            "marks": marks, "points": points, "patterns": patterns, "waves": waves,
            "lines": lines, "discipline": discipline, "summary": summary,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("compute_turning_analysis failed: %s", e)
        return empty


# ================================================================
# 关键价位表
# ================================================================

def _collect_points(
    patterns: list[dict], waves: dict, df: pl.DataFrame, price: float,
) -> list[dict]:
    """综合所有形态颈线/目标/止损/底部顶部 + 波段目标 + 均线 + 60日高低,
    输出 PriceLevel 兼容列表(去重,按距现价排序)。"""
    out: list[dict] = []
    seen: set[float] = set()

    def add(value: float | None, label: str, strength: str = "medium") -> None:
        if value is None or not _ok(value) or value <= 0:
            return
        v = round(value, 2)
        if v in seen:
            return
        seen.add(v)
        out.append({
            "value": v, "label": label, "type": TURNING_TYPE,
            "side": _side(v, price), "strength": strength,
        })

    # 波段目标(中期满足位优先展示)
    if waves.get("available"):
        add(waves.get("second_wave_target"), "中期满足位(第二波)", "strong")
        add(waves.get("w_bottom_target"), "W底满足位", "medium")
        add(waves.get("break_reverse_target"), "破底翻满足位", "medium")
        add(waves.get("first_wave_high"), "第一波高点", "medium")
        add(waves.get("pullback_low"), "回踩低点", "medium")
        add(waves.get("bottom"), "波段底部", "medium")

    # 各形态关键位
    for p in patterns:
        key = p["key"]
        if key in ("double_bottom", "break_below_reverse"):
            add(p.get("neckline"), f"{p['name']}颈线", "strong")
            add(p.get("target"), f"{p['name']}满足点", "medium")
            add(p.get("target2"), f"{p['name']}第二满足", "medium")
            add(p.get("add_level"), f"{p['name']}加码位", "medium")
            add(p.get("bottom"), f"{p['name']}底部", "medium")
        elif key in ("double_top", "fake_breakout"):
            add(p.get("neckline"), f"{p['name']}颈线", "strong")
            add(p.get("target"), f"{p['name']}跌幅满足", "medium")
            add(p.get("top"), f"{p['name']}高点", "medium")
        elif key == "box":
            add(p.get("neckline"), "箱顶", "medium")
            add(p.get("bottom"), "箱底", "medium")
        elif key == "triangle":
            add(p.get("dyn_up"), "三角上缘(突破位)", "strong")
            add(p.get("dyn_lo"), "三角下缘", "medium")
            add(p.get("dyn_target"), "三角动态满足", "medium")
            add(p.get("static_target"), "三角静态满足", "medium")

    # 止损位
    stop = _compute_stop(patterns)
    add(stop, "结构止损", "strong")

    # 近60日高低 + 均线
    highs = _arr(df, "high")
    lows = _arr(df, "low")
    if len(highs) >= 60:
        add(max(highs[-60:]), "近60日高点", "weak")
        add(min(lows[-60:]), "近60日低点", "weak")
    for win, label in ((20, "MA20"), (60, "MA60")):
        v = _ma_value(df, win)
        if v is not None:
            add(v, label, "weak")

    out.sort(key=lambda p: abs(p["value"] - price))
    return out[:16]


def _compute_stop(patterns: list[dict]) -> float | None:
    """结构止损:底部形态取底部×0.985,顶部形态取高点×1.015。"""
    for p in patterns:
        if p["key"] in ("double_bottom", "break_below_reverse") and p.get("bottom"):
            return p["bottom"] * 0.985
        if p["key"] in ("double_top", "fake_breakout") and p.get("top"):
            return p["top"] * 1.015
    return None


# ================================================================
# 操作纪律(确定性规则式,由形态/波段/价位自动推导)
# ================================================================

def _build_discipline(
    patterns: list[dict], waves: dict, form: str, verdict: str, price: float,
    ma20: float | None = None, ma60: float | None = None,
) -> dict:
    """基于形态与关键位自动生成「操作纪律」——结构化观察位 + 铁律。

    刻意区分:
      - 观察区(entry):回踩/站稳的可观察区域(客观结构位)
      - 确认区(add):带量突破的确认位
      - 减码区(reduce):满足位附近(满足点不追高)
      - 防守位(stop):跌破即结构走坏
      - 铁律(rules):蔡森式纪律条文

    定位为技术纪律参考,不构成投资建议。
    """
    entry: list[dict] = []
    add: list[dict] = []
    reduce_: list[dict] = []
    stop: list[dict] = []
    rules: list[str] = []

    by_key = {p["key"]: p for p in patterns}
    bbr = by_key.get("break_below_reverse")
    db = by_key.get("double_bottom")
    dt = by_key.get("double_top")
    fb = by_key.get("fake_breakout")
    box = by_key.get("box")
    tri = by_key.get("triangle")
    bull_struct = bbr or db
    stop_level = _compute_stop(patterns)

    def add_item(bucket: list[dict], label: str, value, note: str) -> None:
        if value is None or not _ok(value):
            return
        bucket.append({"label": label, "value": round(float(value), 2), "note": note})

    if verdict == "bull" and bull_struct:
        neck = bull_struct.get("neckline")
        # 观察区:回踩低点 + 颈线
        if waves.get("available"):
            add_item(entry, "回踩观察区", waves.get("pullback_low"),
                     "回踩不破此位 = 多方结构完好,可观察")
        add_item(entry, "颈线防守", neck, "站稳颈线 = 结构确认;失守则回踩加深")
        # 确认/加码:第一波高点(前高压力)带量站上
        if waves.get("available") and waves.get("first_wave_high"):
            add_item(add, "前高压力(加码确认)", waves.get("first_wave_high"),
                     "带量站上才确认第二波;缩量冲高不算")
        # 减码:满足位(动态/静态/第二波)
        if tri:
            add_item(reduce_, "动态满足(近)", tri.get("dyn_target"), "满足点附近不追高")
            add_item(reduce_, "静态满足(远)", tri.get("static_target"), "站上颈线后主升目标")
        if waves.get("available"):
            add_item(reduce_, "第二波等幅", waves.get("second_wave_target"), "中期满足位,逢高减码")
        # 防守
        add_item(stop, "结构止损", stop_level, "跌破即结构走坏,离场不扛单")
        if wave_bottom := (waves.get("bottom") if waves.get("available") else None):
            if stop_level is None or abs(wave_bottom - stop_level) > 0.01:
                add_item(stop, "波段底部", wave_bottom, "破底翻失败线:跌破则转弱")
        rules = [
            "满足点不追高:冲至满足位附近以减码为主,不追最后一棒",
            "带量突破才可靠:颈线/前高突破需放量确认,缩量冲高视为未确认",
            "破位就离场:跌破结构止损不给幻想,「要输得起才会赢」",
            "回踩不破再进:突破后等回踩确认,不追当日长红",
        ]
    elif verdict == "bear" and (dt or fb):
        top_pat = dt or fb
        neck = top_pat.get("neckline")
        add_item(stop, "颈线(破位线)", neck, "跌破颈线 = 头部确认,反抽颈线不过则弱势延续")
        add_item(entry, "压力观察", top_pat.get("top"), "未站回颈线前,上方均为压力")
        add_item(reduce_, "跌幅满足", top_pat.get("target"), "跌幅满足后不再追杀")
        if top_pat.get("top"):
            add_item(stop, "头部止损", float(top_pat["top"]) * 1.015, "站回头部上方则空头结构失效")
        rules = [
            "爆量长黑不追多:高位放量长黑是反转信号,不是买点",
            "破位不接刀:跌破颈线后反抽不过颈线,弱势延续",
            "跌幅满足不追杀:满足位附近不再追空,防反弹",
            "未站回颈线前不加多:结构未修复前只观察",
        ]
    elif box:
        add_item(entry, "箱底观察", box.get("bottom"), "箱底附近为区间下沿观察位")
        add_item(add, "箱顶突破", box.get("neckline"), "带量突破箱顶才转为趋势,否则仍在区间")
        add_item(stop, "箱底失守", box.get("bottom"), "跌破箱底则区间走坏")
        rules = [
            "箱体内不追单边:区间震荡中高抛低吸,不做方向赌注",
            "突破需带量:箱顶/箱底突破必须放量,否则假突破概率高",
            "区间走坏就离场:跌破箱底不再视为震荡",
        ]
    else:
        # 无明确结构形态:按趋势方向给纪律
        if verdict == "bull":
            add_item(entry, "回踩观察区",
                     (waves.get("pullback_low") if waves.get("available") else price * 0.97),
                     "回踩不破可观察")
            add_item(add, "前高确认位",
                     (waves.get("first_wave_high") if waves.get("available") else price * 1.03),
                     "带量站上才确认延续")
            if waves.get("available"):
                add_item(reduce_, "第二波等幅", waves.get("second_wave_target"), "满足位逢高减码")
            add_item(stop, "结构防守位", stop_level,
                     "跌破则结构转弱")
            rules = [
                "回踩不破再进:等回踩确认,不追当日长红",
                "带量突破才可靠:突破需放量,缩量冲高视为未确认",
                "破位离场:跌破防守位不扛单",
            ]
        elif verdict == "bear":
            # 纯空头趋势:压力位观察 + 站回均线才算修复(用真实均线,缺失则用现价外推)
            press = ma20 if (ma20 and ma20 > price) else price * 1.02
            repair = ma60 if (ma60 and ma60 > price) else press * 1.03
            add_item(entry, "压力观察(未站回前只观察)", press,
                     "下跌趋势中,反弹至均线/压力位为观察区")
            add_item(add, "趋势修复位", repair,
                     "带量站回中期均线上方,空头结构才算修复")
            add_item(stop, "空头失效位", max(repair, price * 1.06) * 1.02,
                     "站上此位则空头结构失效,需重新评估")
            rules = [
                "下跌趋势不接刀:未站回均线前不加多,顺势为主",
                "反弹不追:趋势中反弹视为压力测试,不追高",
                "结构修复才转多:带量站回均线上方再重新评估",
            ]
        else:
            add_item(entry, "区间观察", price * 0.98, "结构未明,区间内观察")
            add_item(stop, "结构防守位", stop_level or price * 0.95, "跌破则转弱")
            rules = [
                "结构未明以观察为主:形态未确认前不重仓",
                "带量突破才可靠:等待放量方向选择",
                "破位离场:跌破防守位不扛单",
            ]

    # 通用铁律补充
    if tri and tri.get("confirmed"):
        rules.append("突破初验未确认:收敛三角突破需 3 日站稳,防冲高回落")
    if not rules:
        rules = ["结构未明,以观察为主;带量突破才可靠;破位离场"]

    return {
        "entry": entry, "add": add, "reduce": reduce_, "stop": stop,
        "rules": rules[:5],
    }


# ================================================================
# 结论 / 置信度
# ================================================================

def _build_desc(patterns: list[dict], waves: dict, price: float, verdict: str) -> str:
    """一句话多空转折研判(供卡片展示)。"""
    vmap = {"bull": "空转多结构", "bear": "多头转空结构", "neutral": "结构未明"}
    parts: list[str] = [f"{vmap[verdict]}"]
    bull_struct = [p for p in patterns if p["key"] in ("double_bottom", "break_below_reverse") and p.get("confirmed")]
    bear_struct = [p for p in patterns if p["key"] in ("double_top", "fake_breakout") and p.get("confirmed")]
    if bull_struct:
        parts.append(f"已确认:{bull_struct[0]['name']}")
    elif bear_struct:
        parts.append(f"已确认:{bear_struct[0]['name']}")
    if waves.get("available"):
        if waves.get("second_wave_target"):
            parts.append(f"第一波已走完,回踩确认中,中期目标 {waves['second_wave_target']:.2f}")
        if waves.get("pullback_low"):
            parts.append(f"回踩低点 {waves['pullback_low']:.2f}")
    # v3.1: 破底翻时间波 / 放量状态
    bbr = next((p for p in patterns if p["key"] == "break_below_reverse"), None)
    if bbr and bbr.get("time_wave_status"):
        parts.append(f"时间波:{bbr['time_wave_status']}")
    if bbr and bbr.get("vol_note"):
        parts.append(bbr["vol_note"])
    parts.append(f"现价 {price:.2f}")
    return ";".join(parts)


def _build_summary(
    form: str, verdict: str, patterns: list[dict], waves: dict, price: float,
) -> str:
    """生成顾问卡片的一句话结论(紧凑)。"""
    vmap = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}
    parts: list[str] = [f"形态:{form}({vmap[verdict]})"]
    # 只列与结论方向一致的已确认形态(避免"破底翻/M头"自相矛盾)
    dir_of = {"bull": "bull", "bear": "bear", "neutral": None}
    want = dir_of[verdict]
    confirmed = [
        p["name"] for p in patterns
        if p.get("confirmed") and (want is None or p["direction"] == want)
    ]
    if confirmed:
        parts.append("已确认:" + "/".join(confirmed[:3]))
    if waves.get("available") and waves.get("second_wave_target"):
        parts.append(f"第二波目标 {waves['second_wave_target']:.2f}")
    # v3.1: 三态摘要 + 破底翻时间波
    done = [p["name"] for p in patterns if p.get("status") == "done"]
    invalid = [p["name"] for p in patterns if p.get("status") == "invalid"]
    if done:
        parts.append("已完成:" + "/".join(done[:2]))
    if invalid:
        parts.append("已失效:" + "/".join(invalid[:2]))
    bbr = next((p for p in patterns if p["key"] == "break_below_reverse"), None)
    if bbr and bbr.get("time_wave_status"):
        parts.append(f"时间波 {bbr['time_wave_status']}")
    parts.append(f"现价 {price:.2f}")
    return " · ".join(parts)


def _confidence(patterns: list[dict], waves: dict) -> str:
    """置信度:确认形态多 / 有波段结构 → 中高;否则中/低。"""
    confirmed = sum(1 for p in patterns if p.get("confirmed"))
    if confirmed >= 2 or (confirmed >= 1 and waves.get("available")):
        return "中高"
    if confirmed >= 1:
        return "中"
    return "低"
