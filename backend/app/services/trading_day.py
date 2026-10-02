"""交易日探针 (oracle) — 回答「今天是否 A 股交易日」。

消费方 (实时行情轮询 / 盘中分钟增量) 在周几+时段门控之后调用, 用于把
「工作日但休市」的节假日从轮询窗口里剔除; 返回 None (未知) 时调用方
维持现状行为 (周几近似 + 快照新鲜度判据兜底), 不引入新依赖。

探测链 (按确定性排序, 先到先得):
  1. fuyao 交易日历 (已配置 fuyao 时): GET /api/a-share/calendar/trading-days,
     今天在近一年交易日列表内 ⇔ 交易日。权威日历, 无时段依赖, 无开盘缓冲问题。
  2. tickflow 实时行情时间戳: 拉一篮流动性票快照 (单请求), max(timestamp)
     日期 == 今天 ⇔ 交易日。非交易日全市场戳停在上一交易日 (2026-08-29 周六
     实测 5551/5551, 含停牌股 — 戳是快照定版时刻, 非最后成交时刻);
     交易日集合竞价阶段 (9:15-9:30) 戳是否已翻新未实测 → 开盘缓冲窗内
     戳过期不作数, 保守视为未知。
  3. 均不可用 → None: 调用方按周几近似继续。

安全约束:
  - 周末直接返回 False (周几判断零成本, 不打任何请求)。
  - 探针是纯读: 只产出一个布尔判定, 不落盘、不进行情管道、不碰归属链路。
  - 只用于「降档」(休市不轮询); 休市结论 TTL 较短 (30 分钟) 定期复探,
    探针误判最坏损失一段快照且可自愈; 未知结论短 TTL (5 分钟) 防止
    轮询循环每拍重打失败的探测。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time

from app.market_time import CN_TZ, cn_now

# tickflow 戳探针的开盘缓冲窗: 此时刻之前戳仍是上一交易日属正常 (集合竞价),
# 不据此判休市。周一实测竞价戳翻新时机后可收紧。仅上午首个窗口需要。
_STALE_BUFFER_UNTIL = dt_time(9, 40)

# 一篮流动性票: 探 max(timestamp), 任一戳为今日即交易日 (OR 语义)。
# 大盘蓝筹同日全部停牌 = 市场性事件, 与休市同处理无碍。
_BASKET = ("000001.SZ", "600519.SH", "600036.SH", "601318.SH", "000651.SZ")

_TTL_TRADING_S = 3600.0   # 交易日结论每小时复探 (跨日天然失效)
_TTL_HOLIDAY_S = 1800.0   # 休市结论 30 分钟复探, 误判自愈上限
_TTL_UNKNOWN_S = 300.0    # 未知结论 5 分钟后重试探测

_CACHE_LOCK = threading.Lock()


@dataclass
class _Cache:
    day: object | None = None
    verdict: bool | None = None
    probed_at: float = 0.0


_CACHE = _Cache()

# ── 人工覆盖清单 ────────────────────────────────────────────────
# 为什么需要: fuyao 交易日历只覆盖滚动一年窗口, 窗口外的日期探针无法确定判定
# (例如国庆后首个交易日 2026-10-08 不在日历内)。这张清单让人工把**已确定**的
# 休市/交易日写进运行时配置, 优先级高于日历与行情戳。
# 文件: <data_dir>/user_data/market_calendar_overrides.json
#   {"closed": ["2026-10-01", ...], "trading": ["2026-10-08", ...]}
_OVERRIDE_TTL_S = 120.0
_OVERRIDE_LOCK = threading.Lock()
_OVERRIDE_CACHE: tuple[float, set[object], set[object]] = (0.0, set(), set())


def _override_path():
    from app.config import settings

    return settings.data_dir / "user_data" / "market_calendar_overrides.json"


def _load_overrides() -> tuple[set[object], set[object]]:
    """读覆盖清单 (按文件 mtime + TTL 缓存)。文件缺失/损坏 → 两个空集合。"""
    global _OVERRIDE_CACHE
    import os
    import time as _time

    try:
        p = _override_path()
        if not p.exists():
            return set(), set()
        mtime = os.path.getmtime(p)
        with _OVERRIDE_LOCK:
            ts, closed, trading = _OVERRIDE_CACHE
            if ts and (_time.monotonic() - ts) < _OVERRIDE_TTL_S and mtime <= ts:
                return closed, trading
        from datetime import date as _date

        raw = json.loads(p.read_text(encoding="utf-8"))

        def _parse(key: str) -> set[object]:
            out: set[object] = set()
            for s in raw.get(key) or []:
                try:
                    out.add(_date.fromisoformat(str(s)[:10]))
                except ValueError:
                    continue
            return out

        closed, trading = _parse("closed"), _parse("trading")
        with _OVERRIDE_LOCK:
            _OVERRIDE_CACHE = (_time.monotonic(), closed, trading)
        return closed, trading
    except Exception:  # noqa: BLE001 — 覆盖清单不可用不应影响探针
        return set(), set()


def reset_cache() -> None:
    """清空探针缓存 (测试用)。"""
    global _OVERRIDE_CACHE
    with _CACHE_LOCK:
        _CACHE.day = None
        _CACHE.verdict = None
        _CACHE.probed_at = 0.0
    with _OVERRIDE_LOCK:
        _OVERRIDE_CACHE = (0.0, set(), set())


def _probe_overrides(now: datetime) -> bool | None:
    """人工覆盖清单: 命中休市 → False, 命中交易日 → True, 未列出 → None。"""
    closed, trading = _load_overrides()
    d = now.date()
    if d in closed:
        return False
    if d in trading:
        return True
    return None


def _probe_fuyao(now: datetime) -> bool | None:
    """fuyao 交易日历: 今天在列表内 ⇔ 交易日。未配置 fuyao / 失败 → None。

    注意: 日历经实测只覆盖一段窗口 (2025-10-09 ~ 2026-09-30)。**超出窗口不等于休市** ——
    直接 `in days` 会把"日历没覆盖到"误判成节假日, 令次日(如国庆后 10-08)被当作休市:
    实时行情轮询会据此降档停摆、盘后分钟K补齐也会被跳过。故超出 [min, max] 时
    返回 None(未知), 交由探测链下一环(tickflow 戳)判定。
    """
    try:
        from app.data_providers import custom as custom_sources

        if not custom_sources.is_custom_provider("fuyao"):
            return None
        provider = custom_sources.get_provider("fuyao")
        days = provider.trading_days()
        if not days:
            return None
        today = now.date()
        if today < min(days) or today > max(days):
            return None
        return today in days
    except Exception:  # noqa: BLE001 — 探针失败按未知处理, 不上抛
        return None


def _probe_tickflow(now: datetime) -> bool | None:
    """tickflow 行情时间戳: max(timestamp) 日期 == 今天 ⇔ 交易日。

    戳停在上一交易日: 开盘缓冲窗内 → None (可能是竞价未翻新), 窗后 → False。
    无实时权限 / 网络失败 / 无有效戳 → None。
    """
    try:
        from app.tickflow.client import get_client

        rows = get_client().quotes.get(symbols=list(_BASKET)) or []
        stamps = [r.get("timestamp") for r in rows if isinstance(r, dict)]
        valid = [int(t) for t in stamps if isinstance(t, (int, float)) and t]
        if not valid:
            return None
        latest_day = datetime.fromtimestamp(max(valid) / 1000, tz=CN_TZ).date()
        if latest_day == now.date():
            return True
        if now.time() < _STALE_BUFFER_UNTIL:
            return None
        return False
    except Exception:  # noqa: BLE001 — 无权限/网络失败按未知处理
        return None


def is_trading_day(now: datetime | None = None) -> bool | None:
    """今天是否 A 股交易日。True=交易日, False=确定休市, None=未知 (维持周几近似)。

    周末零成本直判; 工作日走探测链 (fuyao 日历 → tickflow 时间戳),
    结论按 TTL 缓存。线程安全: 实时行情与分钟增量两个线程共用。
    """
    now = now or cn_now()
    # 0) 人工覆盖清单优先级最高 (含"把周末标为交易日"这类极端情形)。
    override = _probe_overrides(now)
    if override is not None:
        return override
    if now.weekday() >= 5:
        return False

    with _CACHE_LOCK:
        # 「未知」(None) 也是一个结论, 同样按 TTL 缓存 —— 它正是 _TTL_UNKNOWN_S 要
        # 挡住的场景 (未配 fuyao 且 tickflow 不可用时, 轮询每拍都会重打一次探测)。
        # _CACHE.day 只在探测写回时设置, 因此「当天已探过」用它判定即可。
        if (
            _CACHE.day == now.date()
            and (time.monotonic() - _CACHE.probed_at) < _ttl_of(_CACHE.verdict)
        ):
            return _CACHE.verdict

    verdict = _probe_fuyao(now)
    if verdict is None:
        verdict = _probe_tickflow(now)

    with _CACHE_LOCK:
        _CACHE.day = now.date()
        _CACHE.verdict = verdict
        _CACHE.probed_at = time.monotonic()
    return verdict


def _ttl_of(verdict: bool | None) -> float:
    if verdict is True:
        return _TTL_TRADING_S
    if verdict is False:
        return _TTL_HOLIDAY_S
    return _TTL_UNKNOWN_S
