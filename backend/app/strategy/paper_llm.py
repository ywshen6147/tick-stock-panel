# -*- coding: utf-8 -*-
"""LLM 跟单 — 把「海平分时擒牛」分析师能力**内置**到 TSP 模拟盘。

账户级开关 ``auto_mode``：
  - ``"condition"``（默认）：只走 ``paper_auto`` 的条件跟单（监控事件 → 规则 → 下单），
    行为与本模块引入前完全一致。
  - ``"llm"``：走本模块 —— 按 ``llm_interval_seconds`` 节流叫一次 LLM，由它按
    《海平分时擒牛》方法论判断并下单。方法论提示词内置在本文件，**不依赖 EvoFlow**。

触发者是 TSP 自己的行情循环（``quote_service._evaluate_monitors`` → ``maybe_run``），
所以 LLM 跟单在 TSP 进程内自驱，不需要任何外部定时任务。

安全闸门（全部 fail-closed，任一不满足即不下单）：
  1. 账户 ``status == "active"`` 且 ``auto_mode == "llm"``
  2. TSP 侧 AI 已配置（``ai_provider.ai_configured()``）
  3. 当前处于连续竞价时段（``market_time.in_continuous_session()``）
  4. 距上次 LLM 调用 ≥ ``llm_interval_seconds``（默认 300s）
  5. 无同账户在飞请求（模块内 in-flight 去重）
  6. 模型返回**严格 JSON**，且动作/标的/金额均通过校验
  7. 单笔 ≤ ``llm_max_order_amount``（默认 10 万）；当日累计买入 ≤ ``llm_max_daily_amount``
     （默认 20 万）；卖出仍受 ``paper.create_order`` 的 T+1 与超卖防护约束
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.market_time import cn_now, cn_today, in_continuous_session
from app.services.fs_utils import atomic_write_text
from app.strategy import paper

logger = logging.getLogger(__name__)

AUTO_MODE_CONDITION = "condition"
AUTO_MODE_LLM = "llm"
AUTO_MODES = (AUTO_MODE_CONDITION, AUTO_MODE_LLM)

# 默认参数（可被账户设置覆盖）
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MAX_ORDER_AMOUNT = 100_000.0
DEFAULT_MAX_DAILY_AMOUNT = 200_000.0
# 卖出日上限: 只约束"常规减仓"; 破位/止损清仓与价格档位清仓**豁免**(否则止损被卡住)
DEFAULT_MAX_DAILY_SELL_AMOUNT = 200_000.0
# 硬止损: 相对当日开盘跌幅达到该比例 → **由代码直接清仓**（不依赖模型自报 stop）。
# 对应方法论「止损 2-4 个点」的中间值。
STOP_LOSS_PCT = 0.03
# 档位键 → 中文标签（用于决策记录与告警文案）
PRICE_LEVEL_LABELS = {
    "exit_all": "清仓价",
    "reduce_half": "减半仓价",
    "full": "满仓价",
    "add_half": "加半仓价",
}

_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()


# ── 内置方法论提示词（源自《海平分时擒牛》全7讲；不依赖 EvoFlow 员工） ─────────
HAIPING_SYSTEM_PROMPT = """你是「海平分时擒牛」分时交易分析师，以《海平分时擒牛》全7讲为唯一权威方法论。
你只做一件事：判断当前该不该对指定标的买/卖，并按规则给出下单指令。

## 交易框架（核心是分时均价线 vwap）
买点（确定性买点，必须量价配合）：
  1. 回踩均价线不破：现价上穿分时均价线并站稳（第2讲·图pg005）
  2. V型确认：开盘放量拉升≥3% 后跌破均价线、又在短时间内收回（第2讲·图pg007/pg009）
     → 系统判据: `v_shape == true`
  3. 点火/横盘突破：放量突破前高、相对开盘涨幅≥3%（第3讲·图pg012/pg013、第2讲·图pg006）
     → 系统判据: `ignite == true`（刚上穿开盘30分钟最高 + `vol_rel_5m ≥1.2` + 涨幅 ≥3%）
     ⚠️ 这是**罕见**信号：真实样本里多数突破是缩量的（那属于下面第 5 条无量诱多）。
        `ignite` 长期为 false 是正常的，不要为凑信号而放宽标准。
卖点（动能衰竭即走，纪律优先）：
  4. 跌破分时均价线（标准 S 点，也是日内减仓纪律）（第2讲/第4讲）
  5. 无量拉升诱多：涨≥3% 但量能萎缩（第4讲·图pg021）
     → 系统判据: `no_vol_push == true`（涨幅 ≥3% + 价 > 均价 + `vol_rel_5m <0.7`）
  6. 量价背离：创当日新高但量能萎缩（第4讲·图pg019）
     → 系统判据: `vol_divergence == true`（刚创当日新高 + `vol_rel_5m <0.7`）
  7. 硬止损：跌破当日开盘价 3%（第2讲风控：止损2-4个点）
  8. 平低开直接跳水：平/低开且相对开盘再跌 ≥1%（第4讲·图pg017）
     → 系统判据: `flat_low_dive == true`（状态型）
风控纪律：纪律 > 情绪；止损 > 扛单；胜率 > 单次暴利；没有符合条件的机会就**不交易**。

## 输入字段口径（features 里每个标的最新一根已完成分钟bar）
- `vwap`       分时均价线（黄色均价线）—— 本体系的中心基准
- `price`      最新价（与 quotes 一致）
- `pct_from_open`     相对**当日开盘**涨跌幅（小数：0.03 = +3%）
- `pct_vs_prev_close` 相对**昨收**涨跌幅（小数）
- `vol_ratio_1m_today` / `vol_ratio_3m_today` / `vol_ratio_5m_today`
  放量比 = 最近N根量之和 / 此前N根量之和（1.0=平量，>2 明显放量，<0.5 极度缩量）
- `day_high_dist`  距当日最高价（0 = 正处当日最高；负值 = 低于最高）
- `day_low_dist`   距当日最低价（0 = 正处当日最低；正值 = 高于最低）
- `open_30m_high_dist` 距开盘30分钟最高价（上穿 0 = 突破前30分钟高点）
- `bar_time`       该特征所属分钟bar时间

注意：`pct_*` 是**小数**不是百分数；`null` 表示数据不足（窗口不满/缺昨收），
**任何字段为 null 时不要脑补数值，按"数据不足"处理**。

`features[symbol].source` 表示该标的分时数据的来源：
- `"minute"`        = 来自真实分钟K（字段最全）
- `"snapshot+hist"` = 快照 + **系统自建的分钟量能序列**（用快照自带的当日累计
  成交额/成交量按分钟重建，与真实分钟K**同口径**）→ `vol_ratio_*` / `open_30m_*`
  可用，四个形态布尔可用；**这是当前部署的常态**
- `"snapshot"`      = 仅快照基础（`vol_ratio_*` 为 null，量能类条件不可判）
- `"none"`          = 无有效分时数据（必须 hold）

### 系统已替你算好的形态布尔（`v_shape` / `ignite` / `no_vol_push` / `vol_divergence`）

**不要自己重算这四个**，直接采信；它们为 `false` 就代表条件不成立。

它们都是**事件型**判定：内部在「条件由不成立变为成立的那一分钟」记一次，
对外报告的是「**最近 `pattern_window_min` 分钟内发生过**」——
该窗口由系统按你的决策间隔自动设定（`max(5, 间隔分钟 + 1)`），所以调大间隔也不会漏判。
每个字段还配一个 `*_age_min`（距今多少分钟，`0` = 本分钟刚发生，`null` = 当日从未发生）。

所以 `false` 代表**近 5 分钟内没有发生**，不代表形态不存在。判断时优先看
`*_age_min`：越小越"新鲜"，越值得按该信号处理。

| 字段 | 成立条件（系统判定）|
|---|---|
| `v_shape` **V型确认** | 当日曾拉升 ≥3% + 近 10 分钟内**曾跌破**均价线 + **本分钟刚收回**到均价线上方 |
| `ignite` **点火突破** | **本分钟刚上穿**开盘30分钟最高价 + 放量（`vol_rel_5m ≥ 1.2`）+ `pct_from_open ≥ 0.03` |
| `no_vol_push` **无量拉升诱多** | `pct_from_open ≥ 0.03` + 现价 > 均价线 + 缩量（`vol_rel_5m < 0.7`），且**本分钟刚成立** |
| `vol_divergence` **量价背离** | **本分钟刚创出**当日新高（距最高 ≤0.1%）+ 缩量（`vol_rel_5m < 0.7`）|
| `flat_low_dive` **平低开跳水** | `pct_vs_prev_close ≤ 0`（平/低开）+ `pct_from_open ≤ -0.01`（相对开盘再跌 ≥1%）**（状态型，非事件；无 `_age_min`）**|

**量能口径**：`vol_rel_5m` = 近 5 分钟成交量 / **当日每分钟均量**（水平比，
>1.2 放量、<0.7 缩量）。它比 `vol_ratio_5m_today`（近5分钟/前5分钟的**加速度**）
更适合判"放量"：突破前常刚经历放量拉升，加速度比反而 <1，会把放量突破误判为缩量。
两个字段都给，量能判断**优先用 `vol_rel_5m`**。

量能类字段（`vol_ratio_*`、`open_30m_*`）**只在 `source` 为 `minute` 或
`snapshot+hist` 时可用**；为 `snapshot` 时它们恒为 null，此时上述四个条件
一律视为不成立（不要凭感觉补值）。

## 硬性要求
- 只依据给出的数据判断，**绝不编造**行情；数据不足就输出 hold。
- **`features[symbol].vwap` 为 null 时，禁止输出 buy/sell，只能 hold** ——
  均价线缺失时本方法论的所有买卖点都不成立。
- 输出必须是**严格 JSON**，不要任何解释性文字、不要 markdown 代码块。
- 只能在给定标的列表内交易。
- 不能替用户承诺收益，不能预测必然涨跌。

## 输出格式（严格 JSON）
{"action":"buy"|"sell"|"hold","symbol":"<代码或空>","amount":<正整数或0>,
 "stop":<true|false>,"reason":"<30字以内中文理由>","basis":"<方法论依据，如 第2讲·图pg005>"}

- hold 时 symbol 可为空字符串、amount 为 0。
- **`stop` 只在「卖出且属于破位/止损」时置 `true`**，即满足以下之一：
  ① 跌破分时均价线（标准 S 点）；② 硬止损（跌破当日开盘价 3%）。
  置 true 时系统会**按可卖持仓一次性清仓**（不受单笔 10 万限制）—— 因为
  「破位就走、不扛单」必须一次执行完，分批在急跌里卖不干净。
- 其他卖点（无量拉升诱多、量价背离、高开震荡缩量、平低开跳水）属**常规减仓**：
  `stop` 填 `false`，按 `amount`（≤单笔上限）分批卖出。
- 买入时 `stop` 一律 `false`。

## 额度与档位（系统层面，不在你的输出里设置）
- 单笔 ≤ `max_order_amount`；当日累计买入 ≤ `max_daily_amount`；
  **当日累计卖出 ≤ `max_daily_sell_amount`（仅约束常规减仓）**。
- 破位/止损（`stop=true`）的清仓**豁免**卖出日额度 —— 但**不要滥用**：只对
  ①跌破分时均价线 ②跌破开盘价3% 这两种情形置 true，其余卖点必须按分批处理。
- `price_levels` 是账户预先设好的**硬价格档位**（清仓价/减半仓价/满仓价/加半仓价），
  由系统在每次判断前**先行检查并直接执行**。你在输出里**不要**试图与之对抗；
  若当前价已越过某档位，系统会在你被调用之前就处理掉了。
"""


# ── 账户设置读取 ───────────────────────────────────────────────
def auto_mode(acc: dict[str, Any] | None) -> str:
    """账户的跟单模式；非法/缺失一律回退 condition（fail-safe）。"""
    mode = str((acc or {}).get("auto_mode") or "").strip().lower()
    return mode if mode in AUTO_MODES else AUTO_MODE_CONDITION


def _fnum(acc: dict[str, Any], key: str, default: float) -> float:
    try:
        v = float(acc.get(key))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _inum(acc: dict[str, Any], key: str, default: int) -> int:
    try:
        v = int(acc.get(key))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _state_path(data_dir: Path, account_id: str) -> Path:
    return paper._root(data_dir, account_id) / "llm_auto_state.json"


def _log_path(data_dir: Path, account_id: str) -> Path:
    return paper._root(data_dir, account_id) / "llm_decisions.jsonl"


def load_state(data_dir: Path, account_id: str) -> dict[str, Any]:
    p = _state_path(data_dir, account_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(data_dir: Path, account_id: str, state: dict[str, Any]) -> None:
    atomic_write_text(_state_path(data_dir, account_id), json.dumps(state, ensure_ascii=False, indent=2))


def _append_decision(data_dir: Path, account_id: str, rec: dict[str, Any]) -> None:
    try:
        p = _log_path(data_dir, account_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("llm decision log write failed", exc_info=True)


# ── 标的池：显式配置 → 持仓 ∪ 自选 (都为空则 fail-closed) ────────────
def resolve_symbols(data_dir: Path, account_id: str, acc: dict[str, Any]) -> list[str]:
    llsyms = [str(s).strip() for s in (acc.get("llm_symbols") or []) if str(s).strip()]
    if llsyms:
        return llsyms
    out: list[str] = []
    try:
        out.extend(paper.load_positions(data_dir, account_id).keys())
    except Exception:
        pass
    try:
        from app.services import watchlist

        out.extend(str(e.get("symbol") or "").strip() for e in watchlist.list_symbols())
    except Exception:
        logger.debug("watchlist symbols unavailable for llm auto", exc_info=True)
    seen: list[str] = []
    for s in out:
        if s and s not in seen:
            seen.append(s)
    return seen


# ── 分时特征取数（LLM 判断的必需输入） ───────────────────────────
def _prev_close_map(data_dir: Path, symbols: list[str]) -> dict[str, float]:
    """取各标的"上一交易日"收盘价（用于 pct_vs_prev_close）。

    从 kline_daily/date=<d>/part.parquet 分区里，取日期 < 今日的最近一个分区。
    """
    import polars as pl

    out: dict[str, float] = {}
    try:
        root = data_dir / "kline_daily"
        today = cn_today().isoformat()
        parts = sorted(p.name.split("=", 1)[1] for p in root.glob("date=*") if p.name.split("=", 1)[1] < today)
        if not parts:
            return out
        f = root / f"date={parts[-1]}" / "part.parquet"
        if not f.exists():
            return out
        df = pl.read_parquet(f, columns=["symbol", "close"])
        df = df.filter(pl.col("symbol").cast(pl.Utf8).is_in(symbols))
        for r in df.to_dicts():
            try:
                out[str(r["symbol"])] = float(r["close"])
            except (TypeError, ValueError):
                continue
    except Exception:
        logger.debug("prev_close map failed", exc_info=True)
    return out


def _latest_intraday_features(data_dir: Path, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """用本地分钟K算出每个标的**最新一根已完成bar**的分时特征。

    复用 intraday_features.build_feature_frame（与分时信号/回测同口径），
    产出 vwap / pct_from_open / pct_vs_prev_close / vol_ratio_* / day_high_dist 等 ——
    这些正是《海平分时擒牛》判断买卖点的必需字段。

    数据不足（无分钟分区、标的缺失、窗口不满）时该字段为 None，调用方据此 fail-closed。
    """
    import polars as pl

    from app.strategy.intraday_features import INTRADAY_FEATURES, build_feature_frame

    out: dict[str, dict[str, Any]] = {}
    try:
        root = data_dir / "kline_minute"
        # 只认**当日**分区: 分时均价线跨日毫无意义, 用历史分区的 vwap 判断今天的交易
        # 比没有数据更危险(会把旧日形态当成当前)。缺失即返回空 → 上层 fail-closed。
        pdir = root / f"date={cn_today().isoformat()}"
        if not pdir.exists():
            return out
        f = pdir / "part.parquet"
        if not f.exists():
            return out

        df = pl.read_parquet(f)
        if "symbol" not in df.columns:
            return out
        df = df.filter(pl.col("symbol").cast(pl.Utf8).is_in(symbols))
        if df.is_empty():
            return out

        frame = build_feature_frame(df, prev_close=_prev_close_map(data_dir, symbols), cutoff=cn_now())
        if frame.is_empty():
            return out
        for sym in symbols:
            sub = frame.filter(pl.col("symbol") == sym)
            if sub.is_empty():
                continue
            row = sub.sort("datetime").tail(1).to_dicts()[0]
            vals: dict[str, Any] = {}
            for k in INTRADAY_FEATURES:
                v = row.get(k)
                vals[k] = None if v is None else round(float(v), 4)
            vals["bar_time"] = str(row.get("datetime"))
            out[sym] = vals
    except Exception:
        logger.debug("intraday features build failed", exc_info=True)
    return out


def _snapshot_metrics(frame: Any, symbols: list[str],
                      prev_close: dict[str, float]) -> dict[str, dict[str, Any]]:
    """从**实时快照帧**推导分时指标（不依赖分钟K）。

    A股实时快照自带**当日累计成交量/成交额**，因此分时均价线可直接算出：

        vwap = 当日累计成交额 / (当日累计成交量 × 100)      # volume 单位为「手」

    这让「合成分钟源(stocksdk) 不写主分区、主分区无当日分钟K」的部署下，
    依然有**可信的当日均价线**可用（数字来自真实累计成交额，不是合成的）。

    同时可得: 相对开盘涨跌幅 / 相对昨收涨跌幅 / 距当日最高最低。
    拿不到: 放量比(需日内量序列)、开盘30分钟高低(需分钟K) —— 保持 None，
    由提示词的 null 语义约束模型不得据此下单。
    """
    out: dict[str, dict[str, Any]] = {}
    if frame is None:
        return out
    try:
        import polars as pl

        cols = set(getattr(frame, "columns", []) or [])
        if "symbol" not in cols:
            return out
        price_col = "raw_close" if "raw_close" in cols else ("close" if "close" in cols else None)
        if price_col is None:
            return out
        need = [c for c in ("symbol", price_col, "open", "raw_high", "raw_low", "volume", "amount")
                if c in cols]
        sub = frame.select(need).filter(pl.col("symbol").cast(pl.Utf8).is_in(symbols))

        def _pct(num: Any, den: Any) -> float | None:
            try:
                n, d = float(num), float(den)
                return round(n / d - 1.0, 6) if d else None
            except (TypeError, ValueError):
                return None

        for r in sub.to_dicts():
            sym = str(r.get("symbol") or "").strip()
            if not sym:
                continue
            price = r.get(price_col)
            vol, amt = r.get("volume"), r.get("amount")
            vwap = None
            try:
                if vol is not None and amt is not None and float(vol) > 0 and float(amt) > 0:
                    vwap = round(float(amt) / (float(vol) * 100.0), 4)
            except (TypeError, ValueError):
                vwap = None
            out[sym] = {
                "price": float(price) if price is not None else None,
                "vwap": vwap,
                "pct_from_open": _pct(price, r.get("open")),
                "pct_vs_prev_close": _pct(price, prev_close.get(sym)),
                "day_high_dist": _pct(price, r.get("raw_high")),
                "day_low_dist": _pct(price, r.get("raw_low")),
                "volume": vol,
                "amount": amt,
            }
    except Exception:
        logger.debug("snapshot metrics failed", exc_info=True)
    return out


def _day_open_map(frame: Any, symbols: list[str]) -> dict[str, float]:
    """从行情帧取当日开盘价（形态判定需要 pct_from_open 时用）。"""
    out: dict[str, float] = {}
    if frame is None:
        return out
    try:
        import polars as pl

        cols = set(getattr(frame, "columns", []) or [])
        if "symbol" not in cols or "open" not in cols:
            return out
        sub = (frame.select(["symbol", "open"])
               .filter(pl.col("symbol").cast(pl.Utf8).is_in(symbols)))
        for r in sub.to_dicts():
            sym = str(r.get("symbol") or "").strip()
            try:
                f = float(r.get("open"))
            except (TypeError, ValueError):
                continue
            if sym and f > 0:
                out[sym] = f
    except Exception:
        logger.debug("day open extract failed", exc_info=True)
    return out


# ── 上下文构建 ──────────────────────────────────────────────────
def _build_context(data_dir: Path, account_id: str, acc: dict[str, Any],
                   snapshot: dict[str, float], symbols: list[str],
                   quote_frame: Any = None) -> dict[str, Any]:
    today = cn_today().isoformat()
    positions = {}
    try:
        for sym, pos in (paper.load_positions(data_dir, account_id) or {}).items():
            # 物化文件 positions.json 的 available_qty 是上次重建时的 T+1 口径, 跨日不更新
            # (次日仍为 0)。必须与 create_order 同口径**现算**, 否则模型会以为
            # "无可卖持仓"而拒绝卖出 —— 实测过这个坑。
            try:
                avail = int(paper._available_of(pos, today))
            except Exception:
                avail = int(pos.get("qty") or 0)
            positions[sym] = {"qty": pos.get("qty"), "available_qty": avail,
                              "avg_cost": pos.get("avg_cost")}
    except Exception:
        pass

    orders_today = []
    buy_amount_today = 0.0
    sell_amount_today = 0.0
    try:
        for o in paper.load_orders(data_dir, account_id):
            if str(o.get("created_at") or "")[:10] != today:
                continue
            orders_today.append({"symbol": o.get("symbol"), "side": o.get("side"),
                                 "qty": o.get("qty"), "status": o.get("status"),
                                 "source": o.get("source")})
            if o.get("status") in ("pending", "filled"):
                est = float(o.get("qty") or 0) * float(o.get("ref_price") or o.get("price") or 0)
                if o.get("side") == "buy":
                    buy_amount_today += est
                elif o.get("side") == "sell":
                    sell_amount_today += est
    except Exception:
        pass

    quotes = {}
    for s in symbols:
        p = snapshot.get(s)
        if p:
            quotes[s] = round(float(p), 4)

    # 分时特征三层取数，优先级 分钟K > 快照历史(自建量能序列) > 快照基础指标：
    #   1) 真实分钟K（若当日分区存在）字段最全
    #   2) 自建快照历史：用**快照自带的累计量额**按分钟重建量能序列，同口径算出
    #      vol_ratio_* / open_30m_*，并给出四个形态布尔（V型/点火/无量诱多/量价背离）
    #   3) 快照基础：vwap(=累计额/累计量)、涨跌幅、当日高低
    prev_close = _prev_close_map(data_dir, symbols)
    minute_feat = _latest_intraday_features(data_dir, symbols)
    snap_metrics = _snapshot_metrics(quote_frame, symbols, prev_close)
    hist: dict[str, dict[str, Any]] = {}
    # 形态报告窗口必须与决策间隔匹配：否则把 llm_interval_seconds 调大后，
    # 形态会在两次决策之间过期，LLM 看到 false 而静默丢信号。
    try:
        _interval_s = _inum(acc, "llm_interval_seconds", DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        _interval_s = DEFAULT_INTERVAL_SECONDS
    try:
        from app.strategy import intraday_history

        base_window = intraday_history.RECENT_WINDOW_MIN
        pattern_window_min = max(base_window, int(_interval_s / 60) + 1)
        hist = intraday_history.derive(data_dir, symbols,
                                      day_open=_day_open_map(quote_frame, symbols),
                                      recent_window_min=pattern_window_min)
    except Exception:
        pattern_window_min = 5          # 与 intraday_history.RECENT_WINDOW_MIN 同值
        logger.debug("intraday history derive failed", exc_info=True)

    feature_keys = (
        "price", "vwap", "pct_from_open", "pct_vs_prev_close",
        "vol_ratio_1m_today", "vol_ratio_3m_today", "vol_ratio_5m_today",
        "vol_rel_5m",
        "day_high_dist", "day_low_dist",
        "open_30m_high_dist", "open_30m_low_dist",
    )
    pattern_keys = ("v_shape", "ignite", "no_vol_push", "vol_divergence")
    features: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        mf = minute_feat.get(sym) or {}
        hs = hist.get(sym) or {}
        sm = snap_metrics.get(sym) or {}
        merged: dict[str, Any] = {}
        for k in feature_keys:
            v = mf.get(k)
            if v is None:
                v = hs.get(k)
            if v is None:
                v = sm.get(k)
            merged[k] = v
        # 四个形态的判定（由历史推出）+ 各自的"距今多少分钟"
        for pk in pattern_keys:
            if pk in hs:
                merged[pk] = hs[pk]
            age_key = f"{pk}_age_min"
            if age_key in hs:
                merged[age_key] = hs[age_key]
        merged["minutes_recorded"] = hs.get("minutes")
        merged["last_minute"] = hs.get("last_minute")
        merged["pattern_window_min"] = hs.get("pattern_window_min") or pattern_window_min
        # 平低开直接跳水（第4讲·图pg017）：平/低开 且 相对开盘再跌 ≥1%。
        # 与 condition 路径的 hp_sell_flat_low_open_dive 同口径（小数制）。
        _pfo, _pvc = merged.get("pct_from_open"), merged.get("pct_vs_prev_close")
        merged["flat_low_dive"] = bool(
            _pfo is not None and _pvc is not None
            and float(_pvc) <= 0 and float(_pfo) <= -0.01
        )
        if mf.get("vwap") is not None:
            merged["source"] = "minute"           # 真实分钟K
        elif hs.get("vwap") is not None:
            merged["source"] = "snapshot+hist"    # 快照 + 自建量能序列（字段接近完整）
        elif sm.get("vwap") is not None:
            merged["source"] = "snapshot"         # 仅快照基础（无量比）
        else:
            merged["source"] = "none"
        merged["bar_time"] = mf.get("bar_time")
        if merged.get("vwap") is not None or merged.get("price") is not None:
            features[sym] = merged

    return {
        "time": cn_now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": {"id": account_id, "cash": acc.get("cash"), "status": acc.get("status")},
        "positions": positions,
        "orders_today": orders_today,
        "buy_amount_today_est": round(buy_amount_today, 2),
        "sell_amount_today_est": round(sell_amount_today, 2),
        "max_order_amount": _fnum(acc, "llm_max_order_amount", DEFAULT_MAX_ORDER_AMOUNT),
        "max_daily_amount": _fnum(acc, "llm_max_daily_amount", DEFAULT_MAX_DAILY_AMOUNT),
        "max_daily_sell_amount": _fnum(acc, "llm_max_daily_sell_amount", DEFAULT_MAX_DAILY_SELL_AMOUNT),
        # 价格档位（硬规则，优先于 LLM 判断执行；0/未设=不启用）
        "price_levels": {
            "add_half": _fnum(acc, "llm_price_add_half", 0.0) or None,
            "reduce_half": _fnum(acc, "llm_price_reduce_half", 0.0) or None,
            "full": _fnum(acc, "llm_price_full", 0.0) or None,
            "exit_all": _fnum(acc, "llm_price_exit_all", 0.0) or None,
        },
        "quotes": quotes,
        "features": features,
        "symbols": symbols,
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里提取严格 JSON（容错：剥掉 ```json 围栏、取第一个平衡花括号块）。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except Exception:
                        pass
                    break
        start = s.find("{", start + 1)
    return None


# ── 单次 LLM 决策 ────────────────────────────────────────────────
async def _decide(ctx: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """调 LLM 拿决策。返回 (decision|None, raw_text)。"""
    from app.services import ai_provider

    if not ai_provider.ai_configured():
        return None, "AI 未配置（设置 → AI，或 .env 的 AI_API_KEY）"
    user = (
        "以下是当前账户与行情数据（JSON）。请按你的方法论判断，"
        "并只输出规定格式的严格 JSON：\n" + json.dumps(ctx, ensure_ascii=False)
    )
    messages = [
        {"role": "system", "content": HAIPING_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        # 不显式传 temperature / max_tokens:
        #   - 部分模型只接受 temperature=1 (传具体值会 400, 只能靠 provider 重试剥掉,
        #     白跑一轮请求), 故直接不传;
        #   - 推理型模型(如 kimi-k2.7-code / deepseek reasoner)的**思考 token 计入
        #     max_tokens 预算**, 显式限制(如 800)会把正文吃光 → 返回空串,
        #     表现为"模型没输出"。交给服务端默认上限最稳。
        text = await ai_provider.generate_ai_text(
            messages, temperature=None, max_tokens=None, timeout=180.0,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"LLM 调用失败: {e}"
    if not (text or "").strip():
        # 空响应常见于：推理预算被思考吃光、上游限流、内容被过滤。
        # 明确回传原因，避免在决策流水里只看到"未得到有效 JSON"而无从排查。
        return None, "LLM 返回空响应（可能是推理预算/限流/内容过滤）"
    return _extract_json(text), text


# ── 价格档位（硬规则：命中即确定性执行，优先于 LLM 判断） ──────────
def _price_level_action(acc: dict[str, Any], ctx: dict[str, Any],
                        symbols: list[str]) -> dict[str, Any] | None:
    """检查账户级价格档位，命中则返回确定性动作（否则 None，交给 LLM 判断）。

    档位语义（0/未设 = 不启用）:
      llm_price_exit_all     现价 ≤ 该价 → 清仓（卖光可卖持仓）
      llm_price_reduce_half  现价 ≤ 该价 → 减至半仓
      llm_price_full         现价 ≥ 该价 → 买至满仓（用尽可用现金）
      llm_price_add_half     现价 ≥ 该价 → 加至半仓

    优先级: 清仓 > 减半 > 满仓 > 加半（风险动作优先）。
    """

    def _lv(key: str) -> float | None:
        try:
            v = float(acc.get(key) or 0)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    quotes = ctx.get("quotes") or {}
    positions = ctx.get("positions") or {}
    for sym in symbols:
        try:
            px = float(quotes.get(sym) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            continue
        qty = int((positions.get(sym) or {}).get("qty") or 0)

        lv_exit = _lv("llm_price_exit_all")
        lv_reduce = _lv("llm_price_reduce_half")
        lv_full = _lv("llm_price_full")
        lv_add = _lv("llm_price_add_half")

        if lv_exit and px <= lv_exit and qty > 0:
            return {"action": "sell", "symbol": sym, "target": "exit", "stop": True,
                    "reason": f"价格档位·跌破清仓价 {lv_exit}",
                    "basis": f"价格档位 清仓价 {lv_exit}"}
        if lv_reduce and px <= lv_reduce and qty > 0:
            return {"action": "sell", "symbol": sym, "target": "half",
                    "reason": f"价格档位·跌破减半仓价 {lv_reduce}",
                    "basis": f"价格档位 减半仓价 {lv_reduce}"}
        if lv_full and px >= lv_full:
            return {"action": "buy", "symbol": sym, "target": "full",
                    "reason": f"价格档位·站上满仓价 {lv_full}",
                    "basis": f"价格档位 满仓价 {lv_full}"}
        if lv_add and px >= lv_add:
            return {"action": "buy", "symbol": sym, "target": "half",
                    "reason": f"价格档位·站上加半仓价 {lv_add}",
                    "basis": f"价格档位 加半仓价 {lv_add}"}
    return None


def _execute_price_level(data_dir: Path, account_id: str, acc: dict[str, Any],
                         act: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """执行价格档位动作：目标仓位 → 股数 → 下单。

    仓位以**账户权益**衡量（权益 = 现金 + 持仓市值，按快照价）:
      满仓 = 买至权益 100%（= 用尽可用现金）
      半仓 = 买/卖至权益 50%
      清仓 = 卖光可卖持仓

    价格档位是**用户的明确指令**, 因此豁免 LLM 自由裁量限额
    (单笔上限 / 当日买限额 / 当日卖限额) —— 否则"满仓价"永远买不满。
    平台级硬约束仍然生效: 资金预检、T+1 可卖、超卖防护、账户冻结、整手取整。
    """
    sym = act["symbol"]
    target = act.get("target")
    rec: dict[str, Any] = {
        "ts": cn_now().isoformat(timespec="seconds"), "account": account_id,
        "action": act.get("action"), "symbol": sym, "target": target,
        "reason": act.get("reason"), "basis": act.get("basis"),
        "trigger": "price_level", "cap_exempt": True,
    }
    try:
        price = float((ctx.get("quotes") or {}).get(sym) or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        rec["result"] = "rejected"; rec["error"] = "无有效现价"
        return rec

    pos = {}
    try:
        pos = (paper.load_positions(data_dir, account_id) or {}).get(sym) or {}
    except Exception:
        pos = {}
    qty_now = int(pos.get("qty") or 0)
    try:
        avail = int(paper._available_of(pos, cn_today().isoformat())) if pos else 0
    except Exception:
        avail = 0

    cash = 0.0
    try:
        cash = float(acc.get("cash") or 0)
    except (TypeError, ValueError):
        cash = 0.0
    hold_mv = qty_now * price
    equity = cash + hold_mv

    side = str(act.get("action") or "")
    qty = 0
    if target == "exit":
        qty = (avail // 100) * 100
    elif target == "half":
        # 方向由档位决定，不能反向操作：
        #   加半仓价 → 只允许买入到半仓（已在半仓以上则不动，绝不因此卖出）
        #   减半仓价 → 只允许卖出到半仓（已在半仓以下则不动，绝不因此买入）
        delta = equity * 0.5 - hold_mv
        want = str(act.get("action") or "")
        if want == "buy":
            side = "buy"
            qty = (min(max(delta, 0.0), cash) // (price * 100)) * 100
        else:
            side = "sell"
            qty = (min(max(-delta, 0.0) / price, float(avail)) // 100) * 100
    elif target == "full":
        side = "buy"
        qty = (cash // (price * 100)) * 100
    else:
        rec["result"] = "rejected"; rec["error"] = f"未知档位目标: {target!r}"
        return rec

    rec["action"] = side
    if qty <= 0:
        rec["result"] = "rejected"
        # T+1 受阻要**显性化**：持仓明明存在、可卖却为 0（当日买入），档位命中却执行不了。
        # 若只报"无需调仓"，用户会以为档位失效 —— 实际是次日就能执行。
        if target in ("exit", "half") and side == "sell" and qty_now > 0 and avail <= 0:
            rec["error"] = (f"档位命中但被 **T+1** 拦住: 持仓{qty_now}股、可卖0"
                            f"（当日买入），次日方可执行")
            rec["t1_blocked"] = True
        else:
            rec["error"] = (f"档位无需调仓 (目标{target}: 现价{price}, 持仓{qty_now}, "
                            f"可卖{avail}, 现金{cash:.0f})")
        return rec

    qty = int(qty)
    rec["qty"] = qty
    rec["amount"] = round(qty * price, 2)
    if target == "exit":
        rec["stop"] = True
    order, err = paper.create_order(
        data_dir, sym, side, account_id=account_id, qty=qty,
        order_type="market", ref_price=price,
        source=f"llm:{cn_today().isoformat()}",
    )
    if err:
        rec["result"] = "rejected"; rec["error"] = err
    else:
        rec["result"] = "ordered"; rec["order_id"] = order.get("id")
        rec["qty"] = order.get("qty"); rec["price"] = price
    return rec


def _stop_backstop(acc: dict[str, Any], ctx: dict[str, Any], symbols: list[str],
                   down_cross: dict[str, bool]) -> dict[str, Any] | None:
    """**确定性止损兜底**（优先级：价格档位 > 本兜底 > LLM 判断）。

    为什么需要: `stop` 原先只有一个来源 —— 模型自报（`decision.get("stop")`）。而
    「跌破分时均价线（S点）」与「硬止损（跌破开盘 3%）」恰好是方法论里**最不能失效**
    的两条纪律，却成了唯一没有确定性实现的条件：模型漏报 `stop` 时，"破位就走"
    会退化成 10 万/20 万**分批卖出**，急跌里根本卖不干净。这里用代码复算这两条。

      - **硬止损**：`pct_from_open ≤ -STOP_LOSS_PCT` —— 不需要 vwap，
        因此数据源故障（vwap=null）时**仍然可用**。
      - **破位**：本分钟刚下穿分时均价线（由自建分钟序列判定）。

    只在「有持仓 + 有可卖数量 + 当日无 pending 卖单」时返回动作：
    T+1 无可卖时交给正常路径去记录（避免这里每 5 分钟重复尝试）；
    已有 pending 卖单则跳过（平台的超卖防护也会拦，这里少一次噪声）。
    """
    feats = ctx.get("features") or {}
    positions = ctx.get("positions") or {}
    orders_today = ctx.get("orders_today") or []
    for sym in symbols:
        pos = positions.get(sym) or {}
        if int(pos.get("qty") or 0) <= 0:
            continue
        if int(pos.get("available_qty") or 0) <= 0:
            continue
        if any(str(o.get("symbol")) == sym and o.get("side") == "sell"
               and o.get("status") == "pending" for o in orders_today):
            continue
        f = feats.get(sym) or {}
        try:
            pct = float(f.get("pct_from_open")) if f.get("pct_from_open") is not None else None
        except (TypeError, ValueError):
            pct = None
        reason = None
        if pct is not None and pct <= -STOP_LOSS_PCT:
            reason = f"硬止损(代码兜底): 相对开盘 {pct:.2%} ≤ -{STOP_LOSS_PCT:.0%}"
        elif down_cross.get(sym):
            reason = "破位(代码兜底): 本分钟下穿分时均价线(S点)"
        if reason:
            return {"action": "sell", "symbol": sym, "target": "exit", "stop": True,
                    "reason": reason, "basis": "代码兜底·硬止损/跌破均价线S点"}
    return None


def _execute(data_dir: Path, account_id: str, acc: dict[str, Any], decision: dict[str, Any],
             snapshot: dict[str, float], symbols: list[str],
             buy_amount_today: float,
             features: dict[str, dict[str, Any]] | None = None,
             sell_amount_today: float = 0.0) -> dict[str, Any]:
    """校验决策并下单。返回记录（含 action/order_id/rejected 原因）。"""
    action = str(decision.get("action") or "").strip().lower()
    sym = str(decision.get("symbol") or "").strip()
    reason = str(decision.get("reason") or "")[:120]
    basis = str(decision.get("basis") or "")[:80]
    try:
        amount = float(decision.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0

    rec: dict[str, Any] = {
        "ts": cn_now().isoformat(timespec="seconds"), "account": account_id,
        "action": action, "symbol": sym, "amount": amount,
        "reason": reason, "basis": basis,
    }

    if action == "hold":
        rec["result"] = "hold"
        return rec
    if action not in ("buy", "sell"):
        rec["result"] = "rejected"; rec["error"] = f"非法 action: {action!r}"
        return rec
    if not sym or sym not in symbols:
        rec["result"] = "rejected"; rec["error"] = f"标的不在允许列表: {sym!r}"
        return rec

    # 硬护栏: 分时均价线缺失 → 方法论条件不成立, 拒绝下单。
    # **但风险退出例外**: stop=true 的清仓必须放行 —— 数据源故障时, 最该执行的
    # 减仓动作不该被"数据缺失"反过来拦住(U8)。买入与常规卖出仍一律拒绝。
    feat = (features or {}).get(sym) or {}
    is_risk_exit = action == "sell" and bool(decision.get("stop"))
    if feat.get("vwap") is None and not is_risk_exit:
        rec["result"] = "rejected"
        rec["error"] = "分时均价线缺失(vwap=null), 方法论条件不成立, 拒绝下单"
        return rec
    if feat.get("vwap") is None and is_risk_exit:
        rec["degraded"] = "vwap=null 仍执行风险退出"

    price = float(snapshot.get(sym) or 0)
    if price <= 0:
        rec["result"] = "rejected"; rec["error"] = "无有效现价"
        return rec

    # ── 止损/破位清仓通道 ─────────────────────────────────────
    # 「破位就走、不扛单」要求一次执行完；分批(每单10万)在急跌里卖不干净。
    # 当模型标记 stop=true（跌破分时均价线S点 / 硬止损跌破开盘3%）时，
    # 改为**按可卖持仓全额卖出**，不再走金额分批；仍受 T+1 可卖与超卖防护约束。
    is_stop = bool(decision.get("stop"))
    if action == "sell" and is_stop:
        pos = {}
        try:
            pos = (paper.load_positions(data_dir, account_id) or {}).get(sym) or {}
        except Exception:
            pos = {}
        # 注意: 物化文件 positions.json 里的 available_qty 是上次重建时的 T+1 口径,
        # 跨日不更新(次日仍为 0)。必须与 paper.create_order 同口径**现算**
        # (_available_of(pos, 今日) = buy_date < 今日 的批次之和), 否则清仓恒被拒。
        try:
            avail = int(paper._available_of(pos, cn_today().isoformat())) if pos else 0
        except Exception:
            avail = 0
        qty = (avail // 100) * 100          # 按手取整
        if qty <= 0:
            rec["result"] = "rejected"
            rec["error"] = f"止损清仓被拒: 无可用持仓 (available={avail}, T+1)"
            return rec
        rec["stop"] = True
        rec["qty"] = qty
        rec["amount"] = round(qty * price, 2)
        order, err = paper.create_order(
            data_dir, sym, "sell", account_id=account_id,
            qty=qty, order_type="market", ref_price=price,
            source=f"llm:{cn_today().isoformat()}",
        )
        if err:
            rec["result"] = "rejected"; rec["error"] = err
        else:
            rec["result"] = "ordered"; rec["order_id"] = order.get("id")
            rec["qty"] = order.get("qty"); rec["price"] = price
        return rec

    max_order = _fnum(acc, "llm_max_order_amount", DEFAULT_MAX_ORDER_AMOUNT)
    if amount <= 0:
        amount = max_order
    if amount > max_order:
        amount = max_order
        rec["capped"] = f"单笔钳制到 {max_order:.0f}"

    if action == "buy":
        max_daily = _fnum(acc, "llm_max_daily_amount", DEFAULT_MAX_DAILY_AMOUNT)
        remaining = max_daily - float(buy_amount_today or 0)
        if remaining <= 0:
            rec["result"] = "rejected"; rec["error"] = f"当日买入额度已用尽 (上限 {max_daily:.0f})"
            return rec
        if amount > remaining:
            amount = remaining
            rec["capped"] = (rec.get("capped", "") + f"; 当日剩余额度钳制到 {remaining:.0f}").strip("; ")

    if action == "sell":
        # (a) 卖出日上限: 只约束**常规减仓**(无量诱多/量价背离等)。
        # 破位/止损清仓在上面的 stop 分支已返回, 天然豁免 —— 否则止损会被额度卡住。
        max_sell = _fnum(acc, "llm_max_daily_sell_amount", DEFAULT_MAX_DAILY_SELL_AMOUNT)
        remaining = max_sell - float(sell_amount_today or 0)
        if remaining <= 0:
            rec["result"] = "rejected"
            rec["error"] = f"当日卖出额度已用尽 (上限 {max_sell:.0f})"
            return rec
        if amount > remaining:
            amount = remaining
            rec["capped"] = (rec.get("capped", "") + f"; 当日卖出额度钳制到 {remaining:.0f}").strip("; ")

    rec["amount"] = round(amount, 2)
    order, err = paper.create_order(
        data_dir, sym, action, account_id=account_id,
        amount=amount, order_type="market", ref_price=price,
        source=f"llm:{cn_today().isoformat()}",
    )
    if err:
        rec["result"] = "rejected"; rec["error"] = err
    else:
        rec["result"] = "ordered"; rec["order_id"] = order.get("id")
        rec["qty"] = order.get("qty"); rec["price"] = price
    return rec


async def _run_once(data_dir: Path, account_id: str, snapshot: dict[str, float],
                    quote_frame: Any = None) -> dict[str, Any]:
    acc = paper.get_account(data_dir, account_id)
    if acc is None:
        return {"result": "skipped", "error": "账户不存在"}
    symbols = resolve_symbols(data_dir, account_id, acc)
    if not symbols:
        rec = {"ts": cn_now().isoformat(timespec="seconds"), "account": account_id,
               "action": "hold", "result": "rejected",
               "error": "标的池为空（请设置 llm_symbols，或先加自选/持仓）"}
        _append_decision(data_dir, account_id, rec)
        return rec

    ctx = _build_context(data_dir, account_id, acc, snapshot, symbols, quote_frame=quote_frame)
    # 把本次判断所用的分时快照（尤其 vwap）带进记录与状态，供 UI 直观查看
    # 「当前分时均价线是多少、来自哪种数据源」。
    feats = ctx.get("features") or {}
    market = {
        s: {
            "price": (feats.get(s) or {}).get("price"),
            "vwap": (feats.get(s) or {}).get("vwap"),
            "source": (feats.get(s) or {}).get("source"),
        }
        for s in symbols
    }
    decision, raw = None, ""
    # ── 价格档位优先：命中即确定性执行，不再问 LLM ──────────────
    # (用户设的硬价格指令应立刻生效; 也省一次模型调用)
    lvl = _price_level_action(acc, ctx, symbols)
    if lvl is not None:
        rec = _execute_price_level(data_dir, account_id, acc, lvl, ctx)
        rec["market"] = market
        _append_decision(data_dir, account_id, rec)
        if rec.get("result") == "ordered":
            _emit_order_alert(data_dir, account_id, rec)
        logger.info("llm auto %s: price-level %s result=%s qty=%s", account_id,
                    lvl.get("target"), rec.get("result"), rec.get("qty"))
        return rec

    # ── 止损兜底（价格档位已处理则不会走到这里）─────────────────
    # 硬止损 / 下穿均价线 由代码判定并直接清仓, 不再只依赖模型自报 stop。
    try:
        from app.strategy import intraday_history

        down_cross = intraday_history.crossed_below_vwap(data_dir, symbols)
    except Exception:
        logger.debug("stop backstop: down-cross probe failed", exc_info=True)
        down_cross = {}
    stop_act = _stop_backstop(acc, ctx, symbols, down_cross)
    if stop_act is not None:
        rec = _execute_price_level(data_dir, account_id, acc, stop_act, ctx)
        rec["market"] = market
        rec["trigger"] = "stop_backstop"
        _append_decision(data_dir, account_id, rec)
        if rec.get("result") == "ordered":
            _emit_order_alert(data_dir, account_id, rec)
        logger.info("llm auto %s: stop-backstop %s result=%s qty=%s (%s)", account_id,
                    stop_act.get("symbol"), rec.get("result"), rec.get("qty"),
                    stop_act.get("reason"))
        return rec

    decision, raw = await _decide(ctx)
    if decision is None:
        rec = {"ts": cn_now().isoformat(timespec="seconds"), "account": account_id,
               "action": "hold", "result": "rejected",
               "error": f"未得到有效 JSON: {str(raw)[:200]}", "market": market}
        _append_decision(data_dir, account_id, rec)
        return rec

    rec = _execute(data_dir, account_id, acc, decision, snapshot, symbols,
                   ctx.get("buy_amount_today_est") or 0.0,
                   features=feats,
                   sell_amount_today=ctx.get("sell_amount_today_est") or 0.0)
    rec["market"] = market
    _append_decision(data_dir, account_id, rec)
    if rec.get("result") == "ordered":
        _emit_order_alert(data_dir, account_id, rec)
    logger.info("llm auto %s: action=%s result=%s %s", account_id, rec.get("action"),
                rec.get("result"), rec.get("error") or rec.get("order_id") or "")
    return rec


def _emit_order_alert(data_dir: Path, account_id: str, rec: dict[str, Any]) -> None:
    """把 LLM 下单推入告警流（落盘 alerts.jsonl + SSE 实时广播）。

    条件跟单的订单经 `paper_auto.auto_order_events` 由 quote_service 推送；
    LLM 跟单跑在独立线程里，若不自报就会「静默成交」——用户只能在订单列表里
    事后发现。这里补上同构事件，让它在监控中心触发记录与实时通知中可见，
    并把模型给的理由/依据一起带上（便于复盘为什么这么下单）。
    """
    try:
        from app.services import alert_store

        side_label = "买入" if rec.get("action") == "buy" else "卖出"
        msg = f"LLM跟单触发{side_label}下单 {rec.get('qty')}股 @ {rec.get('price')}"
        if rec.get("reason"):
            msg += f" · {rec['reason']}"
        alert_store.append_many(data_dir, [{
            "source": "paper",
            "type": "auto_order",
            "severity": "info",
            "ts": int(cn_now().timestamp() * 1000),
            "symbol": rec.get("symbol"),
            "account_id": account_id,
            "side": rec.get("action"),
            "qty": rec.get("qty"),
            "order_id": rec.get("order_id"),
            "mode": "llm",
            "reason": rec.get("reason"),
            "basis": rec.get("basis"),
            "message": msg,
        }])
    except Exception:
        logger.debug("llm order alert emit failed", exc_info=True)


def _mark_state_after(data_dir: Path, account_id: str, rec: dict[str, Any]) -> None:
    st = load_state(data_dir, account_id)
    st["last_run_ts"] = time.time()
    st["last_run_at"] = cn_now().isoformat(timespec="seconds")
    st["last_result"] = rec.get("result")
    st["last_action"] = rec.get("action")
    st["last_reason"] = rec.get("reason")
    st["last_basis"] = rec.get("basis")
    st["last_error"] = rec.get("error")
    st["last_order_id"] = rec.get("order_id")
    st["last_stop"] = rec.get("stop")
    # 档位/兜底命中但被 T+1 拦住 —— 让 UI 能直接显示"为什么没执行"
    st["last_t1_blocked"] = rec.get("t1_blocked")
    st["last_degraded"] = rec.get("degraded")
    # 本次判断所用的分时快照（价格/均价线/来源）——给 UI 看「当前均价线是多少」
    if rec.get("market") is not None:
        st["last_market"] = rec.get("market")
    st["runs"] = int(st.get("runs") or 0) + 1
    save_state(data_dir, account_id, st)


# ── 行情循环入口（同步，立即返回；真正工作在线程里） ────────────────
def maybe_run(data_dir: Path, account_id: str, snapshot: dict[str, float],
              *, quote_frame: Any = None, now: datetime | None = None) -> bool:
    """行情循环每 tick 调用。命中全部闸门才起线程跑一次 LLM 决策。

    ``quote_frame``: 本 tick 的增强行情帧（含当日累计 volume/amount、open、
    raw_high/raw_low）——用于推导分时均价线。只在真正决策时（节流后）才被读取，
    每 tick 传引用本身无开销。

    返回是否**启动**了一次运行（不代表决策成功）。
    """
    try:
        acc = paper.get_account(data_dir, account_id)
        if acc is None or auto_mode(acc) != AUTO_MODE_LLM:
            return False
        # 每 tick 记录快照历史（放在节流/时段闸门**之前**）：量能与形态派生依赖
        # 分钟序列，只在真正决策时才采样会在分钟桶里留下大片空洞。
        try:
            from app.strategy import intraday_history

            intraday_history.record(data_dir, quote_frame,
                                    resolve_symbols(data_dir, account_id, acc), now=now)
        except Exception:
            logger.debug("intraday history record failed", exc_info=True)
        if str(acc.get("status") or "active") != "active":
            return False
        if not in_continuous_session(now):
            return False
        interval = _inum(acc, "llm_interval_seconds", DEFAULT_INTERVAL_SECONDS)
        state = load_state(data_dir, account_id)
        last = state.get("last_run_ts")
        if last is not None:
            try:
                if (time.time() - float(last)) < interval:
                    return False
            except (TypeError, ValueError):
                pass
        with _LOCK:
            if account_id in _INFLIGHT:
                return False
            _INFLIGHT.add(account_id)

        def _worker() -> None:
            try:
                rec = asyncio.run(_run_once(data_dir, account_id, dict(snapshot or {}),
                                            quote_frame=quote_frame))
                _mark_state_after(data_dir, account_id, rec)
            except Exception as e:  # noqa: BLE001
                logger.warning("llm auto worker failed account=%s: %s", account_id, e)
                _mark_state_after(data_dir, account_id, {"result": "error", "error": str(e)[:200]})
            finally:
                with _LOCK:
                    _INFLIGHT.discard(account_id)

        threading.Thread(target=_worker, name=f"paper-llm-{account_id}", daemon=True).start()
        return True
    except Exception:
        logger.warning("llm auto gate failed account=%s", account_id, exc_info=True)
        with _LOCK:
            _INFLIGHT.discard(account_id)
        return False


def status(data_dir: Path, account_id: str) -> dict[str, Any]:
    """给 API/UI 的开关状态。"""
    acc = paper.get_account(data_dir, account_id) or {}
    from app.services import ai_provider

    try:
        ai_ok = ai_provider.ai_configured()
    except Exception:
        ai_ok = False
    return {
        "auto_mode": auto_mode(acc),
        "ai_configured": ai_ok,
        "ai_model": (ai_provider.current_ai_model() if ai_ok else ""),
        "in_session": in_continuous_session(),
        "interval_seconds": _inum(acc, "llm_interval_seconds", DEFAULT_INTERVAL_SECONDS),
        "max_order_amount": _fnum(acc, "llm_max_order_amount", DEFAULT_MAX_ORDER_AMOUNT),
        "max_daily_amount": _fnum(acc, "llm_max_daily_amount", DEFAULT_MAX_DAILY_AMOUNT),
        "max_daily_sell_amount": _fnum(acc, "llm_max_daily_sell_amount", DEFAULT_MAX_DAILY_SELL_AMOUNT),
        # 价格档位（0 = 未启用）；命中即确定性执行，优先于 LLM 判断
        "price_levels": {
            "add_half": _fnum(acc, "llm_price_add_half", 0.0),
            "reduce_half": _fnum(acc, "llm_price_reduce_half", 0.0),
            "full": _fnum(acc, "llm_price_full", 0.0),
            "exit_all": _fnum(acc, "llm_price_exit_all", 0.0),
        },
        "symbols": resolve_symbols(data_dir, account_id, acc),
        "state": load_state(data_dir, account_id),
    }


# ── 触发统计（形态频率 + 决策结果 + 未下单原因） ────────────────────
def stats(data_dir: Path, account_id: str, days: int = 7) -> dict[str, Any]:
    """按日汇总 LLM 跟单的形态触发与决策结果。

    回答两个问题:
      ① 四个形态各触发了几次、什么时刻（需当日有快照历史; 历史只在 LLM 账户
         运行期间累积, 故更早的日期可能没有数据）;
      ② **触发了但没下单**的原因 —— 决策流水里的 rejected 错误文案与 hold 记录。
    """
    try:
        n_days = max(1, min(int(days or 7), 60))
    except (TypeError, ValueError):
        n_days = 7
    try:
        acc = paper.get_account(data_dir, account_id) or {}
    except Exception:
        acc = {}
    symbols = resolve_symbols(data_dir, account_id, acc)

    # 1) 决策流水按日聚合
    recs: list[dict[str, Any]] = []
    try:
        p = _log_path(data_dir, account_id)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        logger.debug("llm stats: decision log read failed", exc_info=True)

    buckets: dict[str, dict[str, Any]] = {}
    for r in recs:
        d = str(r.get("ts") or "")[:10]
        if not d:
            continue
        b = buckets.setdefault(d, {"runs": 0, "ordered": 0, "rejected": 0, "hold": 0,
                                   "reasons": {}, "orders": []})
        b["runs"] += 1
        res = str(r.get("result") or "")
        if res in ("ordered", "rejected", "hold"):
            b[res] += 1
        if res == "rejected":
            key = str(r.get("error") or "未知原因")[:70]
            b["reasons"][key] = b["reasons"].get(key, 0) + 1
        if res == "ordered":
            b["orders"].append({
                "ts": r.get("ts"), "action": r.get("action"), "symbol": r.get("symbol"),
                "qty": r.get("qty"), "price": r.get("price"), "reason": r.get("reason"),
                "mode": "price_level" if r.get("trigger") == "price_level" else "llm",
            })

    # 2) 形态触发（仅"有快照历史"的日期可算）
    from app.strategy import intraday_history

    hist_days = set(intraday_history.available_days(data_dir))
    out_days: list[dict[str, Any]] = []
    p_totals = {"v_shape": 0, "ignite": 0, "no_vol_push": 0, "vol_divergence": 0}
    t_runs = t_ordered = t_rejected = t_hold = 0
    for d in sorted(set(buckets) | hist_days, reverse=True)[:n_days]:
        b = buckets.get(d) or {"runs": 0, "ordered": 0, "rejected": 0, "hold": 0,
                               "reasons": {}, "orders": []}
        try:
            pe = intraday_history.daily_pattern_events(data_dir, d, symbols)
        except Exception:
            pe = {}
        counts = {k: len(v) for k, v in pe.items()} if pe else {}
        has_hist = d in hist_days and any(pe.values())
        for k in p_totals:
            p_totals[k] += counts.get(k, 0)
        t_runs += b["runs"]; t_ordered += b["ordered"]
        t_rejected += b["rejected"]; t_hold += b["hold"]
        out_days.append({
            "date": d,
            "has_history": has_hist,
            "patterns": counts,
            "pattern_times": pe,
            "runs": b["runs"], "ordered": b["ordered"],
            "rejected": b["rejected"], "hold": b["hold"],
            "reject_reasons": [{"reason": k, "count": v}
                               for k, v in sorted(b["reasons"].items(), key=lambda x: -x[1])],
            "orders": b["orders"],
        })

    return {
        "account": account_id,
        "symbols": symbols,
        "days": out_days,
        "totals": {"patterns": p_totals, "runs": t_runs, "ordered": t_ordered,
                   "rejected": t_rejected, "hold": t_hold},
    }
