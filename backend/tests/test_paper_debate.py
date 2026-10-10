# -*- coding: utf-8 -*-
"""五派辩论测试 —— 四派三轮讨论、汇总、风控闸门、留痕与触发条件。

覆盖:
- 四派并行 × 3 轮 → 12 条 channel=debate 留痕（含 meta 的立场/倾向/反对票）
- 汇总与闸门: ≥3 派明确反对开新仓 → allow_open=False（减仓/止损不受限）
- 单派失败只算弃权（degraded），不影响其余派与最终结论
- 全部弃权 → ok=False
- JSON 容错解析（```json 围栏 / 前后废话）
- 触发条件: 海平 hold 时不召集（零额外成本）; 有出手意图才召集并终判
- 账户设置校验（轮数 1~3、每日上限 0~100）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.strategy import paper
from app.strategy import paper_conversation as pc
from app.strategy import paper_debate as pd
from app.strategy import paper_personas as pp

ACCOUNT = "hp_debate"


def _acct(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    paper.create_account(data_dir, 1_000_000.0, account_id=ACCOUNT, name="五派辩论盘")
    return data_dir, ACCOUNT


def _ctx() -> dict:
    return {
        "features": {
            "688553.SH": {
                "price": 11.51, "day_open": 12.97, "vwap": 12.60,
                "pct_from_open": -0.1126, "vol_ratio_5m_today": 2.4, "amount": 2.3e8,
            }
        },
        "positions": {"688553.SH": {"qty": 51900, "available_qty": 51900, "avg_cost": 12.98}},
        "orders_today": [],
        "cash": 100000.0, "nav": 100000.0,
    }


def _statement(stance: str, bias: str, oppose: bool, conf: float = 0.8) -> str:
    return json.dumps({
        "stance": stance, "action_bias": bias, "oppose_new_position": oppose,
        "confidence": conf, "key_levels": {"支撑": "11.09", "止损": "11.99"},
        "points": ["放量长黑跌破颈线", "均价线压制反抽"],
        "changed_mind": "", "invalidation": "收复 12.60 均价线",
    }, ensure_ascii=False)


@pytest.fixture
def stub_ai(monkeypatch):
    """按"派 id + 轮次"返回预设发言；可用 overrides 定制某派某轮的行为。"""
    calls: list[dict] = []
    # caishen/chanlun/canslim/kxian 四派: 默认前三派反对开新仓, K线派中立
    default = {
        "caishen": _statement("空", "sell", True),
        "chanlun": _statement("空", "stop", True),
        "canslim": _statement("空", "reduce", True),
        "kxian": _statement("中性", "hold", False, 0.6),
    }

    async def fake_generate(messages, temperature=None, max_tokens=None, timeout=None, model=None):
        system = messages[0]["content"]
        # 精确识别是哪一派: analyst_system_prompt = SOUL 原文 + 输出契约,
        # 所以 SOUL 必是系统提示的子串。**不能**用学校名做子串匹配 ——
        # K线战法的 SOUL 里就写着「识别多空转折」, 会被误判成蔡森派。
        matched = next((a["id"] for a in pp.ANALYSTS if a["soul"].strip() in system), None)
        round_no = 1
        user = messages[1]["content"]
        for r in (1, 2, 3):
            if f"第 {r} 轮" in user:
                round_no = r
        calls.append({"analyst": matched, "round": round_no})

        override = getattr(fake_generate, "overrides", {}).get((matched, round_no))
        if override is not None:
            if isinstance(override, Exception):
                raise override
            return override
        if getattr(fake_generate, "fail_all", False):
            raise RuntimeError("模拟上游故障")
        return default.get(matched, _statement("中性", "hold", False))

    fake_generate.overrides = {}
    fake_generate.fail_all = False
    monkeypatch.setattr("app.services.ai_provider.generate_ai_text", fake_generate)
    return fake_generate, calls


# ── 引擎 ─────────────────────────────────────────────────────────────
def test_run_debate_records_twelve_turns_and_gate(tmp_path, stub_ai):
    data_dir, acc = _acct(tmp_path)
    result = asyncio.run(pd.run_debate(data_dir, acc, _ctx(), market={"688553.SH": {}}))

    assert result["ok"] is True
    assert result["rounds"] == 3
    rows = pc.list_turns(data_dir, acc, channel="debate", limit=50, with_request=True)
    assert len(rows) == 12, "四派 × 三轮 = 12 条留痕"

    # meta 完整（面板靠这些字段渲染）
    meta = rows[0]["meta"]
    assert meta["analyst_id"] in {a["id"] for a in pp.ANALYSTS}
    assert meta["round"] in (1, 2, 3)
    assert isinstance(meta["oppose_new_position"], bool)

    debate = result["debate"]
    assert debate["responded"] == 4
    assert debate["oppose_new_position"] == 3          # 蔡森/缠论/CAN SLIM 反对
    assert "空" in debate["consensus"]
    # 闸门: 3 派反对 → 禁止开仓
    assert result["gate"]["allow_open"] is False
    assert "禁止开仓" in result["gate"]["reason"]
    # 给海平的纪要包含闸门提示
    assert "风控闸门已触发" in result["summary"]


def test_debate_gate_opens_when_opposition_below_threshold(tmp_path, stub_ai):
    """反对票 < 阈值(3) → 闸门放行。只覆盖**最后一轮**的表态（闸门看末轮）。"""
    fake, _ = stub_ai
    fake.overrides = {
        ("caishen", 3): _statement("多", "buy", False),
        ("chanlun", 3): _statement("多", "buy", False),
        ("canslim", 3): _statement("中性", "hold", False),
        ("kxian", 3): _statement("空", "reduce", True),   # 仅 1 派反对
    }
    data_dir, acc = _acct(tmp_path)
    result = asyncio.run(pd.run_debate(data_dir, acc, _ctx()))
    assert result["debate"]["oppose_new_position"] == 1
    assert result["gate"]["allow_open"] is True
    assert result["gate"]["reason"] == ""


def test_single_analyst_failure_is_degraded_not_fatal(tmp_path, stub_ai):
    fake, _ = stub_ai
    # 缠论派第 2/3 轮报错 → 只算弃权
    fake.overrides = {
        ("chanlun", 2): RuntimeError("boom"),
        ("chanlun", 3): RuntimeError("boom"),
    }
    data_dir, acc = _acct(tmp_path)
    result = asyncio.run(pd.run_debate(data_dir, acc, _ctx()))
    assert result["ok"] is True
    assert result["degraded"] == 2
    # 最后一轮缠论弃权 → 只有 3 派表态
    assert result["debate"]["responded"] == 3
    rows = pc.list_turns(data_dir, acc, channel="debate", limit=50)
    degraded = [r for r in rows if r["meta"].get("degraded")]
    assert len(degraded) == 2 and degraded[0]["error"]


def test_all_analysts_failing_marks_not_ok(tmp_path, stub_ai):
    fake, _ = stub_ai
    fake.fail_all = True
    data_dir, acc = _acct(tmp_path)
    result = asyncio.run(pd.run_debate(data_dir, acc, _ctx(), rounds=1))
    assert result["ok"] is False
    assert result["debate"]["responded"] == 0
    # 四派全弃权时闸门不误拦（没有反对票）
    assert result["gate"]["allow_open"] is True
    assert "四派均未给出有效结论" in result["summary"]


def test_rounds_are_clamped_and_prompt_contains_prior_statements(tmp_path, stub_ai):
    _, calls = stub_ai
    data_dir, acc = _acct(tmp_path)
    asyncio.run(pd.run_debate(data_dir, acc, _ctx(), rounds=99))
    assert {c["round"] for c in calls} == {1, 2, 3}, "轮数上限 3"
    assert len(calls) == 12


def test_parse_statement_tolerates_fences_and_noise():
    assert pd._parse_statement('```json\n{"stance":"空"}\n```') == {"stance": "空"}
    assert pd._parse_statement('前言{"stance":"多","points":[]}后语') == {
        "stance": "多", "points": [],
    }
    assert pd._parse_statement("完全不是 JSON") is None
    assert pd._parse_statement("") is None


def test_analyst_system_prompt_has_contract_and_is_distinct():
    prompts = {a["id"]: pp.analyst_system_prompt(a) for a in pp.ANALYSTS}
    assert len(set(prompts.values())) == 4, "四派提示词必须各不相同"
    for pid, text in prompts.items():
        assert "输出契约" in text, pid
        assert "oppose_new_position" in text, pid
    assert "缠论" in prompts["chanlun"] and "CAN SLIM" in prompts["canslim"]
    assert "K线战法" in prompts["kxian"] and "蔡森" in prompts["caishen"]


# ── 与决策链的集成 ───────────────────────────────────────────────────
def test_hold_does_not_convene_debate(tmp_path, monkeypatch):
    """海平判 hold → 不召集辩论（零额外成本）。"""
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["688553.SH"], auto_mode="llm")
    called = {"debate": 0}

    async def fake_decide(_ctx, **_kw):
        return ({"action": "hold", "reason": "无信号"}, '{"action":"hold"}')

    def fake_debate(*a, **k):
        called["debate"] += 1
        raise AssertionError("hold 不应召集辩论")

    monkeypatch.setattr(paper_llm, "_decide", fake_decide)
    monkeypatch.setattr("app.strategy.paper_debate.run_debate", fake_debate)
    monkeypatch.setattr(paper_llm, "_stop_backstop", lambda *a, **k: None)
    monkeypatch.setattr(paper_llm, "_price_level_action", lambda *a, **k: None)

    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))
    assert called["debate"] == 0


def test_buy_intent_convenes_debate_and_gate_blocks_order(tmp_path, monkeypatch):
    """海平想买 → 召集辩论 → 闸门触发（≥3 派反对）→ 代码层拦截开仓。"""
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["688553.SH"], auto_mode="llm")
    calls = {"decide": 0, "execute": 0, "debate": 0}

    async def fake_decide(ctx, **_kw):
        calls["decide"] += 1
        if "debate" in ctx:
            return ({"action": "buy", "symbol": "688553.SH", "qty": 1000, "reason": "辩论后仍想买"},
                    '{"action":"buy"}')
        return ({"action": "buy", "symbol": "688553.SH", "qty": 1000, "reason": "初判想买"},
                '{"action":"buy"}')

    async def fake_run_debate(data_dir_, account_id, ctx, **kwargs):
        calls["debate"] += 1
        assert kwargs.get("intent", {}).get("action") == "buy", "初判意图要带给四派"
        return {
            "ok": True, "rounds": 3, "degraded": 0,
            "debate": {"responded": 4, "oppose_new_position": 3, "stance_count": {"空": 3},
                       "consensus": "4 派表态: 空 3 派 → 多数偏空", "statements": []},
            "gate": {"allow_open": False, "oppose_count": 3, "threshold": 3,
                     "reason": "3 派明确反对开新仓（阈值 3）→ 本轮禁止开仓, 只允许减仓/止损"},
            "summary": "## 五派辩论纪要",
        }

    def fake_execute(*a, **k):
        calls["execute"] += 1
        return {"action": "buy", "result": "ordered", "qty": 1000}

    monkeypatch.setattr(paper_llm, "_decide", fake_decide)
    monkeypatch.setattr("app.strategy.paper_debate.run_debate", fake_run_debate)
    monkeypatch.setattr(paper_llm, "_execute", fake_execute)
    monkeypatch.setattr(paper_llm, "_stop_backstop", lambda *a, **k: None)
    monkeypatch.setattr(paper_llm, "_price_level_action", lambda *a, **k: None)

    rec = asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))

    assert calls["debate"] == 1
    assert calls["decide"] == 2, "初判 + 辩论后终判"
    assert calls["execute"] == 0, "闸门触发后不得下单"
    assert rec["result"] == "rejected" and rec["action"] == "hold"
    assert rec["trigger"] == "debate_risk_gate"
    # 闸门留痕（风控兜底渠道），提供"为什么没买"的出处
    gate_turns = pc.list_turns(data_dir, acc, channel="risk", with_request=True)
    assert any(t["meta"].get("trigger") == "debate_risk_gate" for t in gate_turns)
    # 终判留痕标记为 after_debate
    finals = [t for t in pc.list_turns(data_dir, acc, channel="decision")
              if t["meta"].get("after_debate")]
    assert finals and finals[0]["meta"]["phase"] == "final"


def test_debate_disabled_skips(tmp_path, monkeypatch):
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["688553.SH"], auto_mode="llm",
                          llm_debate_enabled=False)
    called = {"debate": 0}

    async def fake_decide(ctx, **_kw):
        return ({"action": "buy", "symbol": "688553.SH", "qty": 100, "reason": "想买"}, "{}")

    def fake_debate(*a, **k):
        called["debate"] += 1
        raise AssertionError("开关关闭时不应召集")

    monkeypatch.setattr(paper_llm, "_decide", fake_decide)
    monkeypatch.setattr("app.strategy.paper_debate.run_debate", fake_debate)
    monkeypatch.setattr(paper_llm, "_execute", lambda *a, **k: {"action": "buy", "result": "rejected"})
    monkeypatch.setattr(paper_llm, "_stop_backstop", lambda *a, **k: None)
    monkeypatch.setattr(paper_llm, "_price_level_action", lambda *a, **k: None)

    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))
    assert called["debate"] == 0


def test_debate_daily_cap(tmp_path, monkeypatch):
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["688553.SH"], auto_mode="llm",
                          llm_debate_daily_max=1)
    called = {"debate": 0}

    async def fake_decide(ctx, **_kw):
        return ({"action": "sell", "symbol": "688553.SH", "qty": 100, "reason": "想卖"}, "{}")

    async def fake_debate(*a, **k):
        called["debate"] += 1
        return {"ok": True, "rounds": 1, "degraded": 0,
                "debate": {"responded": 4, "oppose_new_position": 0, "stance_count": {}},
                "gate": {"allow_open": True, "oppose_count": 0, "threshold": 3, "reason": ""},
                "summary": ""}

    monkeypatch.setattr(paper_llm, "_decide", fake_decide)
    monkeypatch.setattr("app.strategy.paper_debate.run_debate", fake_debate)
    monkeypatch.setattr(paper_llm, "_execute", lambda *a, **k: {"action": "sell", "result": "rejected"})
    monkeypatch.setattr(paper_llm, "_stop_backstop", lambda *a, **k: None)
    monkeypatch.setattr(paper_llm, "_price_level_action", lambda *a, **k: None)

    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))
    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))
    assert called["debate"] == 1, "当日上限 1 → 第二次跳过"


def test_account_settings_validate_debate_fields(tmp_path):
    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_debate_enabled=False,
                          llm_debate_rounds=2, llm_debate_daily_max=4)
    stored = paper.get_account(data_dir, acc)
    assert stored["llm_debate_enabled"] is False
    assert stored["llm_debate_rounds"] == 2
    assert stored["llm_debate_daily_max"] == 4

    with pytest.raises(ValueError):
        paper.update_settings(data_dir, acc, llm_debate_rounds=9)
    with pytest.raises(ValueError):
        paper.update_settings(data_dir, acc, llm_debate_daily_max=999)


def test_debate_channel_has_label():
    assert pc.CHANNEL_LABELS["debate"] == "五派辩论"


# ── 分组模型（海平 / 四派顾问 各用不同模型）──────────────────────────
def test_run_debate_forwards_model_to_analysts(tmp_path, monkeypatch):
    """四派辩论的每次调用都要带上指定模型（空串 = 跟随全局）。"""
    seen: list = []

    async def fake_generate(messages, temperature=None, max_tokens=None, timeout=None, model=None):
        seen.append(model)
        return json.dumps({"stance": "中性", "action_bias": "hold",
                           "oppose_new_position": False, "confidence": 0.5,
                           "key_levels": {}, "points": [], "changed_mind": "",
                           "invalidation": ""}, ensure_ascii=False)

    monkeypatch.setattr("app.services.ai_provider.generate_ai_text", fake_generate)
    data_dir, acc = _acct(tmp_path)

    asyncio.run(pd.run_debate(data_dir, acc, _ctx(), rounds=1, model="fast-model"))
    assert seen and all(m == "fast-model" for m in seen), seen
    assert len(seen) == 4, "四派各一次"

    seen.clear()
    asyncio.run(pd.run_debate(data_dir, acc, _ctx(), rounds=1, model=""))
    assert all(m == "" for m in seen), "空串表示跟随全局, 不由本层补默认值"


def test_group_models_reach_decide_and_debate(tmp_path, monkeypatch):
    """海平用 llm_model、四派用 llm_debate_model: 两条路都要拿到各自的模型名。"""
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["688553.SH"], auto_mode="llm",
                          llm_model="haiping-strong", llm_debate_model="debate-fast")
    seen: dict = {"decide": [], "debate": []}

    async def fake_decide(ctx, **kw):
        seen["decide"].append(kw.get("model"))
        if "debate" in ctx:
            return ({"action": "hold", "reason": "辩论后观望"}, "{}")
        return ({"action": "buy", "symbol": "688553.SH", "qty": 100, "reason": "想买"}, "{}")

    async def fake_run_debate(data_dir_, account_id, ctx, **kwargs):
        seen["debate"].append(kwargs.get("model"))
        return {"ok": True, "rounds": 1, "degraded": 0,
                "debate": {"responded": 4, "oppose_new_position": 0, "stance_count": {},
                           "consensus": "x"},
                "gate": {"allow_open": True, "oppose_count": 0, "threshold": 3, "reason": ""},
                "summary": ""}

    monkeypatch.setattr(paper_llm, "_decide", fake_decide)
    monkeypatch.setattr("app.strategy.paper_debate.run_debate", fake_run_debate)
    monkeypatch.setattr(paper_llm, "_execute", lambda *a, **k: {"action": "buy", "result": "rejected"})
    monkeypatch.setattr(paper_llm, "_stop_backstop", lambda *a, **k: None)
    monkeypatch.setattr(paper_llm, "_price_level_action", lambda *a, **k: None)

    asyncio.run(paper_llm._run_once(data_dir, acc, {}, quote_frame=None))

    assert seen["debate"] == ["debate-fast"], "四派用 llm_debate_model"
    assert seen["decide"] == ["haiping-strong", "haiping-strong"], "海平初判+终判都用 llm_model"

    # 空串 = 跟随全局（交给 provider 用全局配置模型）
    paper.update_settings(data_dir, acc, llm_model="", llm_debate_model="")
    stored = paper.get_account(data_dir, acc)
    assert paper_llm._haiping_model(stored) == "" and paper_llm._debate_model(stored) == ""


def test_debate_records_per_analyst_model_in_turn_meta(tmp_path, monkeypatch):
    """留痕必须记**该派实际调用的**模型。

    回归: 2026-10-10 实测 —— 逐派模型只传给了调用, 留痕仍写四派统一值,
    导致"某派到底用了哪个模型"查不出来（且与真实调用不一致）。
    """
    async def fake_generate(messages, temperature=None, max_tokens=None, timeout=None, model=None):
        return json.dumps({"stance": "中性", "action_bias": "hold",
                           "oppose_new_position": False, "confidence": 0.5,
                           "key_levels": {}, "points": [], "changed_mind": "",
                           "invalidation": ""}, ensure_ascii=False)

    monkeypatch.setattr("app.services.ai_provider.generate_ai_text", fake_generate)
    data_dir, acc = _acct(tmp_path)
    asyncio.run(pd.run_debate(data_dir, acc, _ctx(), rounds=1, model="unified-model",
                              model_by_id={"kxian": "kxian-fast"}))

    rows = pc.list_turns(data_dir, acc, channel="debate", limit=20, with_request=True)
    by_analyst = {r["meta"]["analyst_id"]: r["meta"]["model"] for r in rows}
    assert by_analyst["kxian"] == "kxian-fast", "单独指定的派, 留痕要是它自己的模型"
    assert by_analyst["caishen"] == "unified-model"
    assert len(by_analyst) == 4


def test_normalize_stance_maps_english_and_chinese():
    """模型可能回英文立场; 归一后才能正确汇总共识（豆包系实测回 'neutral'）。"""
    assert pd.normalize_stance("neutral") == "中性"
    assert pd.normalize_stance("Bullish") == "多"
    assert pd.normalize_stance("bearish") == "空"
    assert pd.normalize_stance("中性") == "中性"
    assert pd.normalize_stance("") == "未知"
    assert pd.normalize_stance("新奇说法") == "新奇说法"

    def _row(stance):
        return {"name": "x", "stmt": {"stance": stance, "action_bias": "hold",
                                      "oppose_new_position": False,
                                      "key_levels": {}, "points": []}}

    counts = pd.synthesize([[_row("neutral"), _row("中性"), _row("bearish"), _row("空")]])
    assert counts["stance_count"] == {"中性": 2, "空": 2}, "英文与中文立场必须合并计数"


def test_run_debate_uses_per_analyst_models(tmp_path, monkeypatch):
    """逐派模型: 每派带上自己那一把; 未单独指定的回落到四派统一 model。"""
    seen: dict = {}

    async def fake_generate(messages, temperature=None, max_tokens=None, timeout=None, model=None):
        system = messages[0]["content"]
        aid = next((a["id"] for a in pp.ANALYSTS if a["soul"].strip() in system), "?")
        seen[aid] = model
        return json.dumps({"stance": "中性", "action_bias": "hold",
                           "oppose_new_position": False, "confidence": 0.5,
                           "key_levels": {}, "points": [], "changed_mind": "",
                           "invalidation": ""}, ensure_ascii=False)

    monkeypatch.setattr("app.services.ai_provider.generate_ai_text", fake_generate)
    data_dir, acc = _acct(tmp_path)
    asyncio.run(pd.run_debate(data_dir, acc, _ctx(), rounds=1,
                              model="unified-model",
                              model_by_id={"kxian": "kxian-fast", "chanlun": ""}))

    assert seen["kxian"] == "kxian-fast", "单独指定的派用自己那把"
    assert seen["caishen"] == "unified-model"
    assert seen["canslim"] == "unified-model"
    assert seen["chanlun"] == "unified-model", "空串回落统一值, 不是回落全局"


def test_debate_models_of_priority():
    """优先级: 逐派 > 四派统一 > 全局(空串)。"""
    from app.strategy import paper_llm

    ids = ("caishen", "chanlun", "canslim", "kxian")
    assert paper_llm.debate_models_of({}) == {i: "" for i in ids}
    assert paper_llm.debate_models_of({"llm_debate_model": "u"}) == {i: "u" for i in ids}
    mixed = paper_llm.debate_models_of({"llm_debate_model": "u",
                                       "llm_debate_model_kxian": "fast"})
    assert mixed == {"caishen": "u", "chanlun": "u", "canslim": "u", "kxian": "fast"}
    only_per = paper_llm.debate_models_of({"llm_debate_model_chanlun": "cl"})
    assert only_per["chanlun"] == "cl" and only_per["caishen"] == ""


def test_account_settings_accept_per_analyst_models(tmp_path):
    data_dir, acc = _acct(tmp_path)
    row = paper.update_settings(
        data_dir, acc,
        llm_debate_model="unified",
        llm_debate_model_caishen="c1", llm_debate_model_chanlun="c2",
        llm_debate_model_canslim="c3", llm_debate_model_kxian="c4",
    )
    assert row["llm_debate_model"] == "unified"
    for key, val in (("caishen", "c1"), ("chanlun", "c2"), ("canslim", "c3"), ("kxian", "c4")):
        assert row[f"llm_debate_model_{key}"] == val

    # 清空逐派设置 = 回落四派统一值
    from app.strategy import paper_llm

    row2 = paper.update_settings(data_dir, acc, llm_debate_model_kxian="")
    assert row2["llm_debate_model_kxian"] == ""
    assert paper_llm.debate_models_of(row2)["kxian"] == "unified"


def test_account_settings_validate_group_models(tmp_path):
    data_dir, acc = _acct(tmp_path)
    acc_row = paper.update_settings(data_dir, acc, llm_model="deepseek-v4-pro",
                                    llm_debate_model="doubao-seed-2.0-lite")
    assert acc_row["llm_model"] == "deepseek-v4-pro"
    assert acc_row["llm_debate_model"] == "doubao-seed-2.0-lite"
    with pytest.raises(ValueError):
        paper.update_settings(data_dir, acc, llm_model="x" * 201)


def test_build_manual_context_falls_back_to_daily_bar(tmp_path):
    """盘后/周末手动召集: 没分钟数据时用最新日线兜底, 且前收按**该日线分区自己的日期**取。

    回归: 2026-10-10（周六）实测 —— 最新日线分区是 10-09, 而"日历今天"已是 10-10,
    若相对日历今天取前收, 会取到 10-09 自己的收盘 → `pct_vs_prev_close` 恒为 0。
    """
    import polars as pl

    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    paper.update_settings(data_dir, acc, llm_symbols=["688553.SH"])
    daily = data_dir / "kline_daily"
    for day, row in (
        ("2026-10-08", {"open": 12.97, "high": 13.03, "low": 11.40, "close": 11.55, "amount": 3.39e8}),
        ("2026-10-09", {"open": 11.65, "high": 11.95, "low": 11.11, "close": 11.85, "amount": 2.05e8}),
    ):
        d = daily / f"date={day}"
        d.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["688553.SH"], "open": [row["open"]], "high": [row["high"]],
            "low": [row["low"]], "close": [row["close"]], "volume": [1000.0],
            "amount": [row["amount"]],
        }).write_parquet(d / "part.parquet")

    ctx, market = paper_llm.build_manual_context(data_dir, acc, paper.get_account(data_dir, acc))
    f = (ctx.get("features") or {})["688553.SH"]

    assert f["source"] == "daily_close"
    assert f["price"] == 11.85 and f["day_open"] == 11.65
    assert f["prev_close"] == 11.55, "前收应取 10-08, 而不是日线分区自己(10-09)"
    assert abs(f["pct_vs_prev_close"] - (11.85 / 11.55 - 1)) < 1e-9
    assert abs(f["pct_from_open"] - (11.85 / 11.65 - 1)) < 1e-9
    assert market["688553.SH"]["price"] == 11.85
    assert "日线" in (ctx.get("data_note") or "")

    # 有分钟数据时不得覆盖（此处用不到, 但兜底只应在缺失时生效）
    from app.strategy import paper_llm as pl2

    monkey = tmp_path / "has_minute"
    assert pl2._latest_daily_bar(data_dir, ["688553.SH"])["688553.SH"]["date"] == "2026-10-09"
    assert monkey  # 保留变量, 明确该用例只覆盖"无分钟数据"分支



