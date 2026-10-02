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

_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()


# ── 内置方法论提示词（源自《海平分时擒牛》全7讲；不依赖 EvoFlow 员工） ─────────
HAIPING_SYSTEM_PROMPT = """你是「海平分时擒牛」分时交易分析师，以《海平分时擒牛》全7讲为唯一权威方法论。
你只做一件事：判断当前该不该对指定标的买/卖，并按规则给出下单指令。

## 交易框架（核心是分时均价线 vwap）
买点（确定性买点，必须量价配合）：
  1. 回踩均价线不破：现价上穿分时均价线并站稳（第2讲·图pg005）
  2. V型确认：开盘放量拉升≥3% 后跌破均价线、又在短时间内收回（第2讲·图pg007/pg009）
  3. 点火/横盘突破：放量突破前高、相对开盘涨幅≥3%（第3讲·图pg012/pg013、第2讲·图pg006）
卖点（动能衰竭即走，纪律优先）：
  4. 跌破分时均价线（标准 S 点，也是日内减仓纪律）（第2讲/第4讲）
  5. 无量拉升诱多：涨≥3% 但量能萎缩（第4讲·图pg021）
  6. 量价背离：创当日新高但量能萎缩（第4讲·图pg019）
  7. 硬止损：跌破当日开盘价 3%（第2讲风控：止损2-4个点）
风控纪律：纪律 > 情绪；止损 > 扛单；胜率 > 单次暴利；没有符合条件的机会就**不交易**。

## 硬性要求
- 只依据给出的数据判断，**绝不编造**行情；数据不足就输出 hold。
- 输出必须是**严格 JSON**，不要任何解释性文字、不要 markdown 代码块。
- 只能在给定标的列表内交易。
- 不能替用户承诺收益，不能预测必然涨跌。

## 输出格式（严格 JSON）
{"action":"buy"|"sell"|"hold","symbol":"<代码或空>","amount":<正整数或0>,
 "reason":"<30字以内中文理由>","basis":"<方法论依据，如 第2讲·图pg005>"}
- hold 时 symbol 可为空字符串、amount 为 0。
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


# ── 上下文构建 ──────────────────────────────────────────────────
def _build_context(data_dir: Path, account_id: str, acc: dict[str, Any],
                   snapshot: dict[str, float], symbols: list[str]) -> dict[str, Any]:
    positions = {}
    try:
        for sym, pos in (paper.load_positions(data_dir, account_id) or {}).items():
            positions[sym] = {"qty": pos.get("qty"), "available_qty": pos.get("available_qty"),
                              "avg_cost": pos.get("avg_cost")}
    except Exception:
        pass

    today = cn_today().isoformat()
    orders_today = []
    buy_amount_today = 0.0
    try:
        for o in paper.load_orders(data_dir, account_id):
            if str(o.get("created_at") or "")[:10] != today:
                continue
            orders_today.append({"symbol": o.get("symbol"), "side": o.get("side"),
                                 "qty": o.get("qty"), "status": o.get("status"),
                                 "source": o.get("source")})
            if o.get("side") == "buy" and o.get("status") in ("pending", "filled"):
                est = float(o.get("qty") or 0) * float(o.get("ref_price") or o.get("price") or 0)
                buy_amount_today += est
    except Exception:
        pass

    quotes = {}
    for s in symbols:
        p = snapshot.get(s)
        if p:
            quotes[s] = round(float(p), 4)

    return {
        "time": cn_now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": {"id": account_id, "cash": acc.get("cash"), "status": acc.get("status")},
        "positions": positions,
        "orders_today": orders_today,
        "buy_amount_today_est": round(buy_amount_today, 2),
        "max_order_amount": _fnum(acc, "llm_max_order_amount", DEFAULT_MAX_ORDER_AMOUNT),
        "max_daily_amount": _fnum(acc, "llm_max_daily_amount", DEFAULT_MAX_DAILY_AMOUNT),
        "quotes": quotes,
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
        text = await ai_provider.generate_ai_text(
            messages, temperature=0.2, max_tokens=800, timeout=120.0,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"LLM 调用失败: {e}"
    return _extract_json(text), text


def _execute(data_dir: Path, account_id: str, acc: dict[str, Any], decision: dict[str, Any],
             snapshot: dict[str, float], symbols: list[str],
             buy_amount_today: float) -> dict[str, Any]:
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

    price = float(snapshot.get(sym) or 0)
    if price <= 0:
        rec["result"] = "rejected"; rec["error"] = "无有效现价"
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


async def _run_once(data_dir: Path, account_id: str, snapshot: dict[str, float]) -> dict[str, Any]:
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

    ctx = _build_context(data_dir, account_id, acc, snapshot, symbols)
    decision, raw = await _decide(ctx)
    if decision is None:
        rec = {"ts": cn_now().isoformat(timespec="seconds"), "account": account_id,
               "action": "hold", "result": "rejected", "error": f"未得到有效 JSON: {str(raw)[:200]}"}
        _append_decision(data_dir, account_id, rec)
        return rec

    rec = _execute(data_dir, account_id, acc, decision, snapshot, symbols,
                   ctx.get("buy_amount_today_est") or 0.0)
    _append_decision(data_dir, account_id, rec)
    logger.info("llm auto %s: action=%s result=%s %s", account_id, rec.get("action"),
                rec.get("result"), rec.get("error") or rec.get("order_id") or "")
    return rec


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
    st["runs"] = int(st.get("runs") or 0) + 1
    save_state(data_dir, account_id, st)


# ── 行情循环入口（同步，立即返回；真正工作在线程里） ────────────────
def maybe_run(data_dir: Path, account_id: str, snapshot: dict[str, float],
              *, now: datetime | None = None) -> bool:
    """行情循环每 tick 调用。命中全部闸门才起线程跑一次 LLM 决策。

    返回是否**启动**了一次运行（不代表决策成功）。
    """
    try:
        acc = paper.get_account(data_dir, account_id)
        if acc is None or auto_mode(acc) != AUTO_MODE_LLM:
            return False
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
                rec = asyncio.run(_run_once(data_dir, account_id, dict(snapshot or {})))
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
        "symbols": resolve_symbols(data_dir, account_id, acc),
        "state": load_state(data_dir, account_id),
    }
