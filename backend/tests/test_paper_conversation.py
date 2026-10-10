# -*- coding: utf-8 -*-
"""模拟盘智能体「对话记录 + 用户提问」测试。

覆盖:
- record_turn / list_turns(渠道过滤、新→旧、请求体省略) / get_turn / clear_turns / stats
- 保留上限裁剪
- _chat_messages 组装（system + 历史问答 + 现状上下文 + 本次问题）
- chat_stream: 流式事件序列 + 落盘 chat 往返 + AI 未配置/异常的错误事件
- chat_once 包装
- 决策路径（paper_llm._run_once）与复盘路径（paper_memory.run_daily_review）
  确实写入了对话记录

不发起网络请求: LLM 全部 monkeypatch。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.strategy import paper
from app.strategy import paper_conversation as pc

ACCOUNT = "hp_chat"


def _acct(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    paper.create_account(data_dir, 1_000_000.0, account_id=ACCOUNT, name="海平对话盘")
    return data_dir, ACCOUNT


# ── 基础记录 ────────────────────────────────────────────────────────
def test_record_list_get_clear_stats(tmp_path):
    data_dir, acc = _acct(tmp_path)
    r1 = pc.record_turn(data_dir, acc, channel="decision", request={"a": 1}, response="hold",
                        parsed={"action": "hold"}, meta={"action": "hold"}, elapsed_ms=123.4,
                        system_chars=100)
    r2 = pc.record_turn(data_dir, acc, channel="review", request={"date": "2026-10-02"},
                        response='{"summary":"x"}', parsed={"summary": "x"})
    r3 = pc.record_turn(data_dir, acc, channel="chat", request="为什么没买？", response="因为不满足买点")

    rows = pc.list_turns(data_dir, acc)
    assert [r["channel"] for r in rows] == ["chat", "review", "decision"]  # 新→旧
    assert rows[0]["request"] is None and rows[0]["request_chars"] == len("为什么没买？")

    full = pc.list_turns(data_dir, acc, channel="decision", with_request=True)
    assert len(full) == 1 and full[0]["id"] == r1["id"]
    assert full[0]["request"] == {"a": 1} or full[0]["request"] == '{"a": 1}'
    assert full[0]["elapsed_ms"] == 123

    assert pc.get_turn(data_dir, acc, r2["id"])["channel"] == "review"
    assert pc.get_turn(data_dir, acc, "nope") is None

    st = pc.stats(data_dir, acc)
    assert st["total"] == 3
    assert st["by_channel"] == {"decision": 1, "review": 1, "chat": 1}

    assert pc.clear_turns(data_dir, acc) == 3
    assert pc.list_turns(data_dir, acc) == []


def test_trim_to_max_turns(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr(pc, "MAX_TURNS", 5)
    for i in range(8):
        pc.record_turn(data_dir, acc, channel="chat", request=f"q{i}", response=f"a{i}")
    rows = pc.list_turns(data_dir, acc, limit=50, with_request=True)
    assert len(rows) == 5
    # 保留最新的 5 条
    assert [r["request"] for r in rows] == ["q7", "q6", "q5", "q4", "q3"]


def test_long_request_is_clipped(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr(pc, "_MAX_REQUEST_CHARS", 200)
    big = "x" * 5000
    rec = pc.record_turn(data_dir, acc, channel="decision", request=big, response="ok")
    assert len(rec["request"]) < 5000
    assert "已截断" in rec["request"]


# ── 提问上下文组装 ──────────────────────────────────────────────────
def test_chat_messages_structure(tmp_path):
    data_dir, acc = _acct(tmp_path)
    # 先造两轮历史问答
    pc.record_turn(data_dir, acc, channel="chat", request="上一问", response="上一答")
    pc.record_turn(data_dir, acc, channel="decision", request={"x": 1}, response="d")  # 不应混入

    accd = paper.get_account(data_dir, acc)
    msgs = pc._chat_messages(data_dir, acc, "今天该买吗？", accd)

    assert msgs[0]["role"] == "system" and "海平分时擒牛" in msgs[0]["content"]
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "上一问" and msgs[2]["content"] == "上一答"
    last = msgs[-1]["content"]
    assert "当前状态" in last and "账户主人的问题" in last and "今天该买吗？" in last
    assert "market" in last and "memory" in last


# ── 流式提问 ────────────────────────────────────────────────────────
def _patch_stream(monkeypatch, chunks=("你", "好")):
    async def _fake_stream(messages, **kwargs):
        for c in chunks:
            yield c

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _fake_stream)
    monkeypatch.setattr("app.services.ai_provider.ai_configured", lambda *a, **k: True)
    monkeypatch.setattr("app.services.ai_provider.current_ai_model", lambda: "fake-model")


def test_chat_stream_events_and_persistence(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    _patch_stream(monkeypatch)

    events: list[dict] = []

    async def _run():
        async for line in pc.chat_stream(data_dir, acc, "帮我看看现在的持仓"):
            events.append(json.loads(line))

    asyncio.run(_run())
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert "".join(e.get("content", "") for e in events if e["type"] == "delta") == "你好"

    rows = [r for r in pc.list_turns(data_dir, acc, with_request=True) if r["channel"] == "chat"]
    assert len(rows) == 1
    assert rows[0]["request"] == "帮我看看现在的持仓"
    assert rows[0]["response"] == "你好"
    assert rows[0]["meta"]["model"] == "fake-model"
    assert rows[0]["error"] is None


def test_chat_stream_requires_ai_config(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr("app.services.ai_provider.ai_configured", lambda *a, **k: False)
    events = []

    async def _run():
        async for line in pc.chat_stream(data_dir, acc, "在吗"):
            events.append(json.loads(line))

    asyncio.run(_run())
    assert events and events[0]["type"] == "error"
    assert "AI 未配置" in events[0]["message"]
    assert pc.list_turns(data_dir, acc) == []  # 未调用模型 → 不留记录


def test_chat_stream_empty_question(tmp_path):
    data_dir, acc = _acct(tmp_path)
    events = []

    async def _run():
        async for line in pc.chat_stream(data_dir, acc, "   "):
            events.append(json.loads(line))

    asyncio.run(_run())
    assert events[0]["type"] == "error" and "不能为空" in events[0]["message"]


def test_chat_stream_records_error_on_llm_failure(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)

    async def _boom(messages, **kwargs):
        raise RuntimeError("llm down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _boom)
    monkeypatch.setattr("app.services.ai_provider.ai_configured", lambda *a, **k: True)
    monkeypatch.setattr("app.services.ai_provider.current_ai_model", lambda: "fake")

    events = []

    async def _run():
        async for line in pc.chat_stream(data_dir, acc, "在吗"):
            events.append(json.loads(line))

    asyncio.run(_run())
    assert any(e["type"] == "error" for e in events)
    rows = pc.list_turns(data_dir, acc)
    assert len(rows) == 1 and rows[0]["error"]  # 失败也留痕，便于排障


def test_chat_once_wraps_stream(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    _patch_stream(monkeypatch, chunks=("答", "案"))
    res = asyncio.run(pc.chat_once(data_dir, acc, "问"))
    assert res["ok"] is True and res["answer"] == "答案" and res["turn_id"]


# ── 与交易/复盘路径的集成 ───────────────────────────────────────────
def test_run_once_records_decision_turn(tmp_path, monkeypatch):
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    # 显式标的池，避免读真实自选
    paper.update_settings(data_dir, acc, llm_symbols=["600519.SH"])

    async def _fake_decide(_ctx, **_kw):
        return ({"action": "hold", "reason": "不满足买点", "symbol": "600519.SH"},
                '{"action":"hold","reason":"不满足买点"}')

    def _fake_execute(*a, **k):
        return {"ts": "2026-10-03T10:00:00+08:00", "account": acc, "action": "hold",
                "result": "hold", "reason": "不满足买点"}

    monkeypatch.setattr(paper_llm, "_decide", _fake_decide)
    monkeypatch.setattr(paper_llm, "_execute", _fake_execute)

    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))

    rows = [r for r in pc.list_turns(data_dir, acc, with_request=True) if r["channel"] == "decision"]
    assert len(rows) == 1
    assert rows[0]["parsed"]["action"] == "hold"
    assert rows[0]["meta"]["symbol"] == "600519.SH"
    assert rows[0]["system_chars"] > 0
    # 请求体里应含当日上下文（含 memory 块）
    req = rows[0]["request"]
    req_obj = json.loads(req) if isinstance(req, str) else req
    assert "memory" in req_obj and "features" in req_obj


def test_daily_review_records_review_turn(tmp_path, monkeypatch):
    from app.strategy import paper_memory as pm

    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr("app.strategy.paper_llm.resolve_symbols", lambda *a, **k: [])

    async def _reflect(_payload, *, model=None):
        return ({"summary": "测试复盘", "lessons": [], "knowledge_updates": [],
                 "what_worked": [], "what_failed": [], "mistakes": [],
                 "next_focus": [], "rule_adjustments": []}, '{"summary":"测试复盘"}')

    monkeypatch.setattr(pm, "_reflect", _reflect)
    res = pm.run_daily_review(data_dir, acc, day="2026-10-02")
    assert res["ok"] is True

    rows = [r for r in pc.list_turns(data_dir, acc, with_request=True) if r["channel"] == "review"]
    assert len(rows) == 1
    assert rows[0]["parsed"]["summary"] == "测试复盘"
    assert rows[0]["meta"]["date"] == "2026-10-02"


# ── 风控兜底留痕（channel=risk, 不调用模型）──────────────────────────
def test_risk_channel_has_own_label_and_stats(tmp_path):
    """风控兜底是独立渠道: 有自己的中文标签, 不会被 record_turn 回落到 decision。"""
    data_dir, acc = _acct(tmp_path)
    assert pc.CHANNEL_LABELS["risk"] == "风控兜底"

    rec = pc.record_turn(data_dir, acc, channel="risk",
                         request={"trigger": "stop_backstop"}, response="硬止损(代码兜底)",
                         meta={"trigger": "stop_backstop", "model_called": False}, elapsed_ms=0.0)
    assert rec["channel"] == "risk"

    rows = pc.list_turns(data_dir, acc, channel="risk", with_request=True)
    assert len(rows) == 1 and rows[0]["channel"] == "risk"
    assert pc.stats(data_dir, acc)["by_channel"]["risk"] == 1


def test_risk_turn_payload_snapshot():
    """留痕的 request 快照: 盘口 + 持仓 + 阈值, 并明确标注未调用模型。"""
    from app.strategy.paper_llm import _risk_turn_payload

    ctx = {
        "features": {"600519.SH": {"pct_from_open": -0.0517, "price": 12.21,
                                   "vwap": 12.55, "day_open": 12.87}},
        "positions": {"600519.SH": {"qty": 51900, "available_qty": 51900, "avg_cost": 12.98}},
    }
    stop_act = {"action": "sell", "symbol": "600519.SH", "target": "exit", "stop": True,
                "reason": "硬止损(代码兜底): 相对开盘 -5.17% ≤ -3%"}
    payload = _risk_turn_payload(ctx, stop_act, {"date": "2026-10-08"}, 0.03)

    assert payload["trigger"] == "stop_backstop" and payload["model_called"] is False
    assert payload["symbol"] == "600519.SH"
    assert payload["threshold_pct"] == -0.03
    assert payload["features"]["pct_from_open"] == -0.0517
    assert payload["position"]["qty"] == 51900


def test_run_once_records_risk_turn_when_backstop_fires(tmp_path, monkeypatch):
    """代码兜底止损必须写「风控兜底」留痕, 且**不调用模型**。

    回归: 2026-10-08 default 账户硬止损清仓 688553.SH（相对开盘 -5.17%），
    盘里有卖出、智能体对话里却查不到 —— 因为兜底分支在 record_turn 之前 return。
    """
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["600519.SH"], auto_mode="llm")

    stop_act = {"action": "sell", "symbol": "600519.SH", "target": "exit", "stop": True,
                "reason": "硬止损(代码兜底): 相对开盘 -5.17% ≤ -3%",
                "basis": "代码兜底·硬止损/跌破均价线S点"}
    monkeypatch.setattr(paper_llm, "_stop_backstop", lambda *a, **k: stop_act)
    monkeypatch.setattr(paper_llm, "_execute_price_level",
                        lambda *a, **k: {"ts": "2026-10-08T09:35:20+08:00", "account": acc,
                                         "action": "sell", "result": "ordered", "qty": 51900,
                                         "stop": True})
    monkeypatch.setattr(paper_llm, "_emit_order_alert", lambda *a, **k: None)
    monkeypatch.setattr("app.strategy.intraday_history.crossed_below_vwap", lambda *a, **k: {})

    async def _boom(_ctx, **_kw):  # 兜底路径一旦调用模型即视为回归
        raise AssertionError("兜底路径不应调用模型")

    monkeypatch.setattr(paper_llm, "_decide", _boom)

    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))

    risk = pc.list_turns(data_dir, acc, channel="risk", with_request=True)
    assert len(risk) == 1, "兜底止损必须留下风控兜底记录"
    turn = risk[0]
    assert turn["meta"]["trigger"] == "stop_backstop"
    assert turn["meta"]["model_called"] is False
    assert turn["meta"]["symbol"] == "600519.SH" and turn["meta"]["qty"] == 51900
    assert "硬止损" in turn["response"]
    assert turn["system_chars"] == 0          # 没发给模型 → 无系统提示词
    assert turn["elapsed_ms"] == 0
    req = json.loads(turn["request"]) if isinstance(turn["request"], str) else turn["request"]
    assert req["model_called"] is False and req["threshold_pct"] == -0.03
    # 兜底只在模型之前拦截 → 不应同时产生 decision 留痕
    assert pc.list_turns(data_dir, acc, channel="decision") == []

