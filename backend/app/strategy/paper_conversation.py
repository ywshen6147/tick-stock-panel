# -*- coding: utf-8 -*-
"""模拟盘智能体的「对话记录」与「用户提问」。

两件事:

1. **对话记录（transcript）** —— 把智能体与 LLM 的每一次往返都留痕，让用户能看见
   "它到底给模型发了什么、模型回了什么":
     - ``channel="decision"``: 盘中 LLM 跟单的每次决策（请求 = 当时的上下文 JSON +
       记忆块；回复 = 模型原文 + 解析出的动作）
     - ``channel="review"``:   每日收盘复盘（请求 = 当日事实汇总；回复 = 结构化复盘）
     - ``channel="chat"``:     用户提问（请求 = 用户问题；回复 = 智能体回答）
   落盘 ``data/paper/accounts/<id>/conversation.jsonl``，有界保留（见 ``MAX_TURNS``）。

2. **用户提问（chat）** —— 让用户直接问这个智能体（支持流式）:
   "今天为什么没买？" / "688553 现在这个位置怎么看？"
   回答基于**同一套**《海平分时擒牛》方法论 + 该账户的记忆/知识库/持仓现状。

安全边界:
  - 提问通道**只做分析问答，不下单**。要交易请走「跟单模式 → LLM跟单」或手动下单。
  - 记录失败绝不影响交易路径（全部 try/except 吞掉并 debug 日志）。
  - 请求体可能很大（决策上下文含全标的特征），落盘前做截断（``_MAX_REQUEST_CHARS``）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.strategy import paper

logger = logging.getLogger(__name__)

# 对话记录保留条数（有界；超出丢弃最旧）
MAX_TURNS = 1500
# 单条请求/回复的落盘上限（字符）——防上下文过大把日志撑爆
_MAX_REQUEST_CHARS = 200_000
_MAX_RESPONSE_CHARS = 40_000
# 提问时回带的最近对话轮数（保持上下文连贯，又不至于过长）
_CHAT_HISTORY_TURNS = 6

_LOCK = threading.Lock()

CHANNEL_LABELS = {
    "decision": "盘中决策",
    "review": "收盘复盘",
    "chat": "我的提问",
    # 代码兜底风控（硬止损/跌破均价线清仓）——**不调用模型**，但必须留痕:
    # 否则盘里有卖出、对话里查不到出处(实测 2026-10-08 硬止损清仓 688553.SH 即如此)
    "risk": "风控兜底",
    # 五派辩论（蔡森/缠论/CAN SLIM/K线战法 四派顾问, 每派每轮一条留痕）——
    # 结论交海平分时擒牛终判；每派都是独立的模型调用, 面板可逐条看它们怎么吵
    "debate": "五派辩论",
}


def _conv_path(data_dir: Path, account_id: str) -> Path:
    return paper._root(data_dir, account_id) / "conversation.jsonl"


def _new_id() -> str:
    return f"turn_{int(time.time() * 1000)}_{os.urandom(2).hex()}"


def _clip(s: Any, n: int) -> str:
    text = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return text if len(text) <= n else text[: n - 30] + "\n…[已截断]…"


def _read_all(data_dir: Path, account_id: str) -> list[dict]:
    rows: list[dict] = []
    p = _conv_path(data_dir, account_id)
    try:
        if not p.exists():
            return rows
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception:
        logger.debug("conversation read failed", exc_info=True)
    return rows


def _write_all(data_dir: Path, account_id: str, rows: list[dict]) -> None:
    try:
        p = _conv_path(data_dir, account_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        atomic_write_text(p, text)
    except Exception:
        logger.warning("conversation write failed", exc_info=True)


def record_turn(
    data_dir: Path,
    account_id: str,
    *,
    channel: str,
    request: Any,
    response: str = "",
    parsed: Any = None,
    meta: dict | None = None,
    elapsed_ms: float | None = None,
    system_chars: int = 0,
    error: str | None = None,
) -> dict:
    """追加一条对话往返记录（失败静默，绝不影响交易）。"""
    rec = {
        "id": _new_id(),
        "ts": cn_now().isoformat(timespec="seconds"),
        "account": account_id,
        "channel": channel if channel in CHANNEL_LABELS else "decision",
        "request": _clip(request, _MAX_REQUEST_CHARS),
        "response": _clip(response or "", _MAX_RESPONSE_CHARS),
        "parsed": parsed if isinstance(parsed, (dict, list)) else None,
        "meta": meta or {},
        "elapsed_ms": round(float(elapsed_ms), 0) if elapsed_ms is not None else None,
        "system_chars": int(system_chars or 0),
        "error": (error or None),
    }
    try:
        with _LOCK:
            rows = _read_all(data_dir, account_id)
            rows.append(rec)
            if len(rows) > MAX_TURNS:
                rows = rows[-MAX_TURNS:]
            _write_all(data_dir, account_id, rows)
    except Exception:
        logger.debug("record_turn failed", exc_info=True)
    return rec


def list_turns(
    data_dir: Path,
    account_id: str,
    *,
    channel: str | None = None,
    limit: int = 50,
    with_request: bool = False,
) -> list[dict]:
    """对话记录（新→旧）。``with_request=False`` 时省略大请求体（列表视图用）。"""
    rows = _read_all(data_dir, account_id)
    if channel:
        rows = [r for r in rows if r.get("channel") == channel]
    rows = list(reversed(rows))[: max(1, min(int(limit or 50), 500))]
    if not with_request:
        rows = [{**r, "request": None, "request_chars": len(str(r.get("request") or ""))} for r in rows]
    return rows


def get_turn(data_dir: Path, account_id: str, turn_id: str) -> dict | None:
    for r in reversed(_read_all(data_dir, account_id)):
        if r.get("id") == turn_id:
            return r
    return None


def clear_turns(data_dir: Path, account_id: str) -> int:
    with _LOCK:
        rows = _read_all(data_dir, account_id)
        _write_all(data_dir, account_id, [])
    return len(rows)


def stats(data_dir: Path, account_id: str) -> dict:
    rows = _read_all(data_dir, account_id)
    by_channel: dict[str, int] = {}
    for r in rows:
        ch = str(r.get("channel") or "?")
        by_channel[ch] = by_channel.get(ch, 0) + 1
    return {
        "total": len(rows),
        "by_channel": by_channel,
        "last_ts": rows[-1].get("ts") if rows else None,
    }


# ══════════════════════════════════════════════════════════════════
# 用户提问（chat）
# ══════════════════════════════════════════════════════════════════
CHAT_SYSTEM_PROMPT = """你是「海平分时擒牛」分时交易分析师，正在和你的**账户主人**对话。

## 你的能力与约束
- 你以《海平分时擒牛》为唯一权威方法论：核心是**分时均价线（VWAP）**。
  买点：回踩均价线不破 / V型确认 / 点火·横盘突破（须量价配合）；
  卖点：跌破均价线 / 无量拉升诱多 / 量价背离 / 平低开跳水；
  风控：硬止损 2-4 个点，纪律 > 情绪、止损 > 扛单、胜率 > 单次暴利，没有机会就不交易。
- 你会拿到 `当前状态`（账户、持仓、当日委托、你的记忆与知识库、标的池分时特征）。
  **只依据这些真实数据回答**；数据里没有的（未记录的消息面、别人的观点、未来走势）
  不要编造。字段为 null 表示数据缺失，明确说"数据不足"。
- 你**没有下单权限**：不要输出"我已买入/卖出"，也不要假装已经执行。
  若主人让你交易，告诉他：切到「跟单模式 → LLM跟单」由你按纪律自动执行，或他自己手动下单。
- 你**不预测涨跌、不承诺收益**。给判断要给**触发条件与失效条件**（什么情况下这个判断就错了）。

## 回答风格
- 中文，先给结论再给依据；**引用具体数字**（价位、涨跌幅、量比、持仓成本）。
- 简洁：一般 3-6 句；主人要求展开时才详细。
- 主动指出风险与自己的失误（如果记忆里有相关教训，直接引用并说明这次是否适用）。
- 不确定就说不确定，宁可说"数据不足，需要看 XX"。

## 你还会看到
- `记忆与知识库`：这是你自己每日复盘沉淀的纪律，**回答时要遵守并优先引用**；
  若你的判断与某条经验冲突，必须说明这次为何例外。"""


def _compact_market(data_dir: Path, account_id: str, acc: dict) -> dict:
    """账户与市场现状（提问时喂给模型的最小充分集）。"""
    today = cn_now().strftime("%Y-%m-%d")
    positions: list[dict] = []
    try:
        for sym, pos in (paper.load_positions(data_dir, account_id) or {}).items():
            qty = int(pos.get("qty") or 0)
            if qty <= 0:
                continue
            positions.append({
                "symbol": sym, "qty": qty, "avg_cost": pos.get("avg_cost"),
                "available_qty": int(paper._available_of(pos, today) or 0),
            })
    except Exception:
        logger.debug("chat positions failed", exc_info=True)

    orders_today: list[dict] = []
    try:
        for o in paper.load_orders(data_dir, account_id):
            if str(o.get("created_at") or "")[:10] != today:
                continue
            orders_today.append({
                "ts": o.get("created_at"), "symbol": o.get("symbol"), "side": o.get("side"),
                "qty": o.get("qty"), "status": o.get("status"), "source": o.get("source"),
                "reason": o.get("reason"),
            })
    except Exception:
        logger.debug("chat orders failed", exc_info=True)

    symbols: list[str] = []
    try:
        from app.strategy.paper_llm import resolve_symbols

        symbols = resolve_symbols(data_dir, account_id, acc)
    except Exception:
        logger.debug("chat symbols failed", exc_info=True)

    # 标的池即时特征（复用交易决策同一套取数：分钟K > 快照历史 > 快照基础）
    features: dict[str, Any] = {}
    try:
        from app.strategy import paper_llm

        ctx = paper_llm._build_context(data_dir, account_id, acc, {}, symbols)
        feats = ctx.get("features") or {}
        keep = ("price", "vwap", "pct_from_open", "pct_vs_prev_close",
                "vol_ratio_5m_today", "vol_rel_5m", "day_high_dist", "day_low_dist",
                "v_shape", "ignite", "no_vol_push", "vol_divergence", "source", "bar_time")
        for sym, f in feats.items():
            features[sym] = {k: f.get(k) for k in keep if f.get(k) is not None}
    except Exception:
        logger.debug("chat features failed", exc_info=True)

    return {
        "time": cn_now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": {
            "id": account_id, "name": acc.get("name"),
            "cash": acc.get("cash"), "initial_cash": acc.get("initial_cash"),
            "auto_mode": acc.get("auto_mode"),
        },
        "positions": positions,
        "orders_today": orders_today,
        "symbol_pool": symbols,
        "features": features,
    }


def _chat_messages(data_dir: Path, account_id: str, question: str, acc: dict) -> list[dict]:
    """组装提问消息：system + 最近问答历史 + 本次（现状 + 问题）。"""
    messages: list[dict] = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]

    # 最近若干轮「我的提问」（保持连贯；不含决策/复盘的机器日志，避免串味）
    history = [r for r in _read_all(data_dir, account_id) if r.get("channel") == "chat"]
    for r in history[-_CHAT_HISTORY_TURNS:]:
        q = str(r.get("request") or "").strip()
        a = str(r.get("response") or "").strip()
        if q:
            messages.append({"role": "user", "content": q})
        if a:
            messages.append({"role": "assistant", "content": a})

    market = _compact_market(data_dir, account_id, acc)
    memory: dict[str, Any] = {}
    try:
        from app.strategy import paper_memory

        memory = paper_memory.build_memory_context(
            data_dir, account_id, symbols=market.get("symbol_pool") or [],
            max_reviews=3, max_lessons=8, max_knowledge=4,
        )
    except Exception:
        logger.debug("chat memory failed", exc_info=True)

    user = (
        "【当前状态】（真实数据，JSON）\n"
        + json.dumps({"market": market, "memory": memory}, ensure_ascii=False)
        + f"\n\n【账户主人的问题】\n{question.strip()}"
    )
    messages.append({"role": "user", "content": user})
    return messages


async def chat_stream(
    data_dir: Path,
    account_id: str,
    question: str,
) -> AsyncIterator[str]:
    """流式回答用户提问（NDJSON 事件），并把往返写入对话记录。

    事件协议（每行一个 JSON）::
      {"type":"meta","channel":"chat","model":...}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done","turn_id":"..."}
    """
    question = (question or "").strip()
    if not question:
        yield json.dumps({"type": "error", "message": "问题不能为空"}, ensure_ascii=False)
        return

    from app.services import ai_provider

    if not ai_provider.ai_configured():
        yield json.dumps({"type": "error", "message": "AI 未配置（设置 → AI 填写 API Key）"}, ensure_ascii=False)
        return

    acc = paper.get_account(data_dir, account_id) or {}
    try:
        messages = _chat_messages(data_dir, account_id, question, acc)
    except Exception as e:  # noqa: BLE001
        yield json.dumps({"type": "error", "message": f"装配上下文失败: {e}"}, ensure_ascii=False)
        return

    # 记录**实际使用的**模型：配了「对话」分组模型就用它，否则回落到全局模型。
    # （之前只报全局模型，配了分组模型后留痕与真实调用不一致。）
    model = ""
    try:
        model = ai_provider.role_model("chat") or ai_provider.current_ai_model()
    except Exception:
        pass
    yield json.dumps({"type": "meta", "channel": "chat", "model": model}, ensure_ascii=False)

    parts: list[str] = []
    error: str | None = None
    t0 = time.perf_counter()
    try:
        async for delta in ai_provider.stream_ai_text(
            messages, temperature=0.4, max_tokens=None, timeout=240.0, prefer_final_answer=True,
            # 分组模型：「对话」组，可在 设置→AI 配置
            model=ai_provider.role_model("chat"),
        ):
            parts.append(delta)
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        error = f"{e}"
        logger.warning("paper chat stream failed account=%s: %s", account_id, e)
        yield json.dumps({"type": "error", "message": f"AI 调用失败: {e}"}, ensure_ascii=False)

    answer = "".join(parts)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    system_chars = len(CHAT_SYSTEM_PROMPT)
    rec = record_turn(
        data_dir, account_id, channel="chat",
        request=question, response=answer,
        meta={"model": model, "history_turns": _CHAT_HISTORY_TURNS},
        elapsed_ms=elapsed_ms, system_chars=system_chars, error=error,
    )
    if not error:
        yield json.dumps({"type": "done", "turn_id": rec["id"]}, ensure_ascii=False)


async def chat_once(data_dir: Path, account_id: str, question: str) -> dict:
    """非流式提问（测试/脚本用）。"""
    parts: list[str] = []
    turn_id = None
    err = None
    async for line in chat_stream(data_dir, account_id, question):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "delta":
            parts.append(ev.get("content") or "")
        elif ev.get("type") == "done":
            turn_id = ev.get("turn_id")
        elif ev.get("type") == "error":
            err = ev.get("message")
    return {"ok": err is None, "answer": "".join(parts), "turn_id": turn_id, "error": err}
