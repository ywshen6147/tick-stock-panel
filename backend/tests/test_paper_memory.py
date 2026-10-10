# -*- coding: utf-8 -*-
"""模拟盘智能体「记忆 + 知识库 + 每日收盘复盘」测试。

覆盖:
- 知识库播种幂等 / CRUD / 检索打分
- 经验教训写入与相似合并（confidence/hits 递增）
- 提示词记忆块的结构与上限
- 复盘事实汇总（build_review_payload 读当日成交/委托/平仓）
- run_daily_review: 持久化 + 幂等 + force 重跑 + LLM 失败落 state.last_error
- review_all_accounts: 无活动账户跳过
- 交易决策提示词确实注入了 memory（paper_llm._build_context）

不发起任何网络请求: LLM 调用被 monkeypatch 替换。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.strategy import paper
from app.strategy import paper_memory as pm

ACCOUNT = "hp_test"


# ── 夹具 ────────────────────────────────────────────────────────────
def _acct(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    paper.create_account(data_dir, 1_000_000.0, account_id=ACCOUNT, name="海平测试盘")
    return data_dir, ACCOUNT


def _write_fills(data_dir: Path, account_id: str, fills: list[dict]) -> None:
    p = paper._fills_path(data_dir, account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in fills), encoding="utf-8")


def _fill(seq: int, day: str, side: str, qty: int, price: float, *, symbol: str = "600519.SH",
          fee: float = 0.0) -> dict:
    return {
        "seq": seq, "ts": f"{day}T10:00:0{seq % 10}+08:00", "date": day, "order_id": f"o{seq}",
        "symbol": symbol, "asset_type": "stock", "side": side, "kind": "fill",
        "qty": qty, "price": price, "fee": fee,
    }


def _fake_reflect(review: dict | None = None):
    """返回一个可替换 pm._reflect 的协程函数。"""
    payload = review or {
        "summary": "当日测试复盘：冲高回落，纪律执行不足",
        "what_worked": ["回踩均价线买入后浮盈"],
        "what_failed": ["破位未及时止损，回吐利润"],
        "mistakes": ["追高买入", "破均价线未减仓"],
        "lessons": [
            {"text": "价格跌破分时均价线且3分钟内不收回必须减仓", "tags": ["卖点", "纪律"], "confidence": 0.85},
            {"text": "开盘30分钟内的首次突破多为缩量诱多，不可满仓", "tags": ["买点"], "confidence": 0.6},
        ],
        "knowledge_updates": [
            {"title": "破线3分钟未收回即减仓", "body": "跌破 VWAP 后 3 分钟未收回 → 减半仓；5 分钟未收回 → 清仓。",
             "tags": ["卖点", "纪律"]},
        ],
        "next_focus": ["盯住均价线破位", "控制单笔仓位不超过三成"],
        "rule_adjustments": [{"rule": "硬止损", "change": "3% → 2.5%", "reason": "当日两笔均亏在3%附近"}],
    }

    async def _reflect(_payload, *, model=None):
        return payload, "{}"

    return _reflect


# ── 知识库 ──────────────────────────────────────────────────────────
def test_seed_knowledge_idempotent(tmp_path):
    data_dir, acc = _acct(tmp_path)
    first = pm.seed_knowledge(data_dir, acc)
    assert first == len(pm._SEED_KNOWLEDGE)
    assert pm.seed_knowledge(data_dir, acc) == 0  # 幂等
    kb = pm.load_knowledge(data_dir, acc)
    assert len(kb) == len(pm._SEED_KNOWLEDGE)
    assert all("methodology" in (r.get("tags") or []) for r in kb)
    assert all(r.get("id", "").startswith("kb_") for r in kb)


def test_knowledge_crud_and_search(tmp_path):
    data_dir, acc = _acct(tmp_path)

    e1 = pm.add_knowledge(data_dir, acc, title="跌破均价线减仓", body="跌破 VWAP 减半仓", tags=["卖点"])
    assert e1 and e1["title"] == "跌破均价线减仓"

    # 同标题 → 更新而非新增
    e2 = pm.add_knowledge(data_dir, acc, title="跌破均价线减仓", body="跌破 VWAP 直接清仓", tags=["纪律"])
    assert e2 and e2["id"] == e1["id"]
    assert len(pm.load_knowledge(data_dir, acc)) == 1
    assert "清仓" in e2["body"]
    assert set(e2["tags"]) >= {"卖点", "纪律"}

    # 更新
    pm.update_knowledge(data_dir, acc, e1["id"], title="破线处理", body="改")
    assert pm.load_knowledge(data_dir, acc)[0]["title"] == "破线处理"

    # 检索：标题命中优于正文
    pm.add_knowledge(data_dir, acc, title="点火突破买点", body="放量破前30分钟高点", tags=["买点"])
    hits = pm.search_knowledge(pm.load_knowledge(data_dir, acc), "点火突破", limit=5)
    assert hits and hits[0]["title"] == "点火突破买点"

    # 按标签检索
    by_tag = pm.search_knowledge(pm.load_knowledge(data_dir, acc), "", tags=["买点"], limit=5)
    assert any(r["title"] == "点火突破买点" for r in by_tag)

    # 删除
    assert pm.delete_knowledge(data_dir, acc, e1["id"]) is True
    assert pm.delete_knowledge(data_dir, acc, e1["id"]) is False
    assert len(pm.load_knowledge(data_dir, acc)) == 1


# ── 经验教训 ────────────────────────────────────────────────────────
def test_append_lessons_dedupes_and_boosts_confidence(tmp_path):
    data_dir, acc = _acct(tmp_path)
    saved = pm.append_lessons(data_dir, acc, [
        {"text": "跌破分时均价线且3分钟不收回必须减仓", "tags": ["卖点"], "confidence": 0.6},
    ], day="2026-10-08")
    assert len(saved) == 1
    lid = saved[0]["id"]

    # 相似（仅标点/空格差异）→ 合并：hits+1, confidence 上调
    again = pm.append_lessons(data_dir, acc, [
        {"text": "跌破分时均价线，且 3 分钟不收回，必须减仓！", "tags": ["纪律"], "confidence": 0.7},
    ], day="2026-10-09")
    assert again[0]["id"] == lid
    assert again[0]["hits"] == 1
    assert again[0]["confidence"] > 0.6
    assert set(again[0]["tags"]) >= {"卖点", "纪律"}
    assert len(pm.load_lessons(data_dir, acc)) == 1

    # 差异明显 → 新增
    pm.append_lessons(data_dir, acc, [{"text": "开盘30分钟首次突破多为缩量诱多不可满仓"}], day="2026-10-09")
    assert len(pm.load_lessons(data_dir, acc)) == 2

    # 归档后不再出现在 active 列表
    assert pm.retire_lesson(data_dir, acc, lid) is True
    assert all(r["id"] != lid for r in pm.load_lessons(data_dir, acc))
    assert any(r["id"] == lid for r in pm.load_lessons(data_dir, acc, active_only=False))


# ── 提示词记忆块 ────────────────────────────────────────────────────
def test_build_memory_context_shapes_and_caps(tmp_path):
    data_dir, acc = _acct(tmp_path)
    pm.seed_knowledge(data_dir, acc)
    pm.append_lessons(data_dir, acc, [{"text": f"第{i}条经验：跌破均价线减仓", "confidence": 0.5 + i * 0.05}
                                     for i in range(20)], day="2026-10-08")

    ctx = pm.build_memory_context(data_dir, acc, symbols=["600519.SH"],
                                 max_reviews=3, max_lessons=5, max_knowledge=3)
    assert ctx["enabled"] is True
    assert len(ctx["lessons"]) == 5
    assert len(ctx["knowledge"]) == 3
    assert any("methodology" in (r.get("tags") or []) for r in pm.load_knowledge(data_dir, acc))
    assert ctx["note"]
    assert set(ctx["profile"].keys()) >= {"win_rate", "focus", "streak"}


# ── 复盘事实汇总 ────────────────────────────────────────────────────
def test_build_review_payload_reads_day_facts(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    day = "2026-10-08"
    _write_fills(data_dir, acc, [
        _fill(1, day, "buy", 100, 10.0),
        _fill(2, day, "sell", 100, 10.5, fee=5.0),
    ])
    paper.rebuild_positions(data_dir, acc)
    # 避免测试触碰真实自选/分时历史
    monkeypatch.setattr("app.strategy.paper_llm.resolve_symbols", lambda *a, **k: [])

    payload = pm.build_review_payload(data_dir, acc, day)
    assert payload["date"] == day
    assert payload["today"]["fills"] and payload["today"]["fills"][0]["symbol"] == "600519.SH"
    assert payload["today"]["realized_pnl"] > 0
    assert payload["today"]["round_trips"][0]["symbol"] == "600519.SH"
    assert payload["decisions"]["runs"] == 0
    assert payload["account"]["id"] == acc


# ── 收盘复盘 ────────────────────────────────────────────────────────
def test_run_daily_review_persists_and_is_idempotent(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr("app.strategy.paper_llm.resolve_symbols", lambda *a, **k: [])
    monkeypatch.setattr(pm, "_reflect", _fake_reflect())
    day = "2026-10-08"

    res = pm.run_daily_review(data_dir, acc, day=day)
    assert res["ok"] is True, res
    rec = res["review"]
    assert rec["date"] == day
    assert rec["review"]["summary"].startswith("当日测试复盘")
    assert res["lessons_saved"] == 2
    assert res["knowledge_saved"] == 1

    # 落盘可读
    got = pm.get_review(data_dir, acc, day)
    assert got is not None and got["review"]["next_focus"]
    assert len(pm.load_lessons(data_dir, acc)) == 2

    # 知识库：播种 + 复盘沉淀
    titles = [r["title"] for r in pm.load_knowledge(data_dir, acc)]
    assert "破线3分钟未收回即减仓" in titles

    # 画像：关注点进入 profile
    profile = pm.load_profile(data_dir, acc)
    assert profile["days_reviewed"] == 1
    assert "盯住均价线破位" in profile["focus"]
    assert profile["top_lessons"]

    # 幂等：同日重复默认跳过
    again = pm.run_daily_review(data_dir, acc, day=day)
    assert again["ok"] is True and again.get("skipped") == "already_reviewed"

    # force 重跑
    forced = pm.run_daily_review(data_dir, acc, day=day, force=True)
    assert forced["ok"] is True and not forced.get("skipped")
    # 相似经验合并 → 数量不翻倍
    assert len(pm.load_lessons(data_dir, acc)) == 2

    st = pm.status(data_dir, acc)
    assert st["available"] is True
    assert st["counts"]["reviews"] == 1
    assert st["counts"]["knowledge"] >= 6
    assert st["last_review_day"] == day
    assert st["last_error"] is None


def test_run_daily_review_llm_failure_records_error(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr("app.strategy.paper_llm.resolve_symbols", lambda *a, **k: [])

    async def _boom(_payload, *, model=None):
        return None, "AI 未配置（设置 → AI）"

    monkeypatch.setattr(pm, "_reflect", _boom)
    res = pm.run_daily_review(data_dir, acc, day="2026-10-08")
    assert res["ok"] is False
    assert "AI 未配置" in res["error"]
    # 失败不写复盘文件, 但状态里留痕
    assert pm.get_review(data_dir, acc, "2026-10-08") is None
    st = pm.status(data_dir, acc)
    assert "AI 未配置" in (st["last_error"] or "")


def test_review_all_accounts_skips_no_activity(tmp_path, monkeypatch):
    data_dir, acc = _acct(tmp_path)
    monkeypatch.setattr(pm, "_trading_day_ok", lambda day=None: True)
    monkeypatch.setattr("app.strategy.paper_llm.resolve_symbols", lambda *a, **k: [])

    called: list[str] = []

    def _fake_run(d, a, *, day=None, force=False):
        called.append(a)
        return {"ok": True, "day": day, "review": {}}

    monkeypatch.setattr(pm, "run_daily_review", _fake_run)

    res = pm.review_all_accounts(data_dir, day="2026-10-08")
    assert res["ok"] is True
    assert res["accounts"][0]["skipped"] == "no_activity"
    assert called == []  # 无成交/委托 → 不烧 token

    # 有当日成交 → 触发复盘
    _write_fills(data_dir, acc, [_fill(1, "2026-10-08", "buy", 100, 10.0)])
    res2 = pm.review_all_accounts(data_dir, day="2026-10-08")
    assert called == [acc]
    assert res2["accounts"][0]["ok"] is True


def test_review_all_accounts_holiday_skips(tmp_path, monkeypatch):
    data_dir, _ = _acct(tmp_path)
    monkeypatch.setattr(pm, "_trading_day_ok", lambda day=None: False)
    res = pm.review_all_accounts(data_dir, day="2026-10-08")
    assert res["skipped"] == "holiday"


# ── 决策提示词确实带上记忆 ──────────────────────────────────────────
def test_llm_context_includes_memory(tmp_path, monkeypatch):
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)
    pm.seed_knowledge(data_dir, acc)
    pm.append_lessons(data_dir, acc, [{"text": "跌破均价线必须减仓", "confidence": 0.9}], day="2026-10-08")

    accd = paper.get_account(data_dir, acc)
    ctx = paper_llm._build_context(data_dir, acc, accd, {}, [])
    assert "memory" in ctx
    mem = ctx["memory"]
    assert mem["enabled"] is True
    assert mem["lessons"] and mem["lessons"][0]["text"] == "跌破均价线必须减仓"
    assert mem["knowledge"]
    assert "记忆" in paper_llm.HAIPING_SYSTEM_PROMPT


def test_llm_context_memory_failure_is_non_fatal(tmp_path, monkeypatch):
    """记忆读取异常时降级为 disabled，不影响交易上下文其余部分。"""
    from app.strategy import paper_llm

    data_dir, acc = _acct(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("memory exploded")

    monkeypatch.setattr(pm, "build_memory_context", _boom)
    accd = paper.get_account(data_dir, acc)
    ctx = paper_llm._build_context(data_dir, acc, accd, {}, [])
    assert ctx["memory"]["enabled"] is False
    assert "time" in ctx and "positions" in ctx

