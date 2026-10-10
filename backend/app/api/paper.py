"""虚拟账户(模拟盘) API — 薄胶水: 校验/映射在 paper 域模块, 这里只做 HTTP 壳。

多账户: 所有端点接受 ?account=<id> (缺省 "default"); 帐户 ID 校验在域模块。

路由挂载在 app/main.py; 域模块自带进程内写锁, API 层不再加第二把锁
(镜像 lots API 的跨请求互斥语义, 但锁下沉到域)。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.strategy import paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _acc(request: Request, account: str) -> str:
    try:
        return paper.validate_account_id(account)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class AccountModel(BaseModel):
    initial_cash: float
    account_id: str = paper.DEFAULT_ACCOUNT_ID
    name: str | None = None
    commission_pct: float = paper.DEFAULT_COMMISSION_PCT
    stamp_tax_pct: float = paper.DEFAULT_STAMP_TAX_PCT
    slippage_bps: float = paper.DEFAULT_SLIPPAGE_BPS
    queue_limit_orders: bool = False


class OrderModel(BaseModel):
    symbol: str
    side: str                       # buy / sell
    qty: int | None = None
    amount: float | None = None
    order_type: str = "market"      # market / next_open / close
    ref_price: float | None = None  # amount 模式折算 & 买入资金预检


class SettingsModel(BaseModel):
    queue_limit_orders: bool | None = None
    commission_pct: float | None = None   # 佣金率 (0.00025 = 万2.5)
    stamp_tax_pct: float | None = None    # 印花税 (0.001 = 千1, 仅卖出)
    slippage_bps: float | None = None     # 滑点 (5 = 5bps)
    # ── 跟单模式 (二选一) ──
    #   condition = 条件跟单 (监控事件 → 自动规则), 默认
    #   llm       = LLM 跟单   (TSP 内置海平方法论判断, paper_llm 自驱)
    auto_mode: str | None = None
    llm_symbols: list[str] | None = None          # 空 = 跟随持仓+自选
    llm_interval_seconds: int | None = None       # LLM 决策最小间隔
    llm_max_order_amount: float | None = None     # LLM 单笔上限 (元)
    llm_max_daily_amount: float | None = None     # LLM 当日买入上限 (元)
    llm_max_daily_sell_amount: float | None = None  # LLM 当日卖出上限 (元, 仅常规减仓)
    llm_stop_loss_pct: float | None = None        # 硬止损阈值 (小数: 0.03 = 3%)
    # ── 价格档位 (硬规则; 命中即确定性执行, 优先于 LLM 判断; 0/不传 = 不启用) ──
    llm_price_add_half: float | None = None       # 现价 ≥ 此价 → 加至半仓
    llm_price_reduce_half: float | None = None    # 现价 ≤ 此价 → 减至半仓
    llm_price_full: float | None = None           # 现价 ≥ 此价 → 买至满仓
    llm_price_exit_all: float | None = None       # 现价 ≤ 此价 → 清仓
    # ── 五派辩论 (蔡森/缠论/CAN SLIM/K线战法 四派讨论 → 海平终判) ──
    llm_debate_enabled: bool | None = None        # 账户级开关 (缺省开)
    llm_debate_rounds: int | None = None          # 轮数 1~3
    llm_debate_daily_max: int | None = None       # 当日最多召集次数 (安全网)
    # ── 分组模型 (空 = 跟随全局 AI 设置里的模型) ──
    llm_model: str | None = None                  # 海平 决策/终判 用的模型
    llm_debate_model: str | None = None           # 四派顾问**统一**用的模型 (逐派未指定时生效)
    llm_review_model: str | None = None           # 收盘复盘 用的模型
    # 四派顾问逐派模型 (空 = 回落到 llm_debate_model, 再回落全局)
    llm_debate_model_caishen: str | None = None   # 蔡森·多空转折顾问
    llm_debate_model_chanlun: str | None = None   # 缠论·走势实战顾问
    llm_debate_model_canslim: str | None = None   # 笑傲股市 CAN SLIM 分析师
    llm_debate_model_kxian: str | None = None     # K线战法分析师


def _resolve_asset_type(request: Request, symbol: str) -> str:
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


def _last_close(data_dir: Path, symbol: str, asset_type: str) -> float | None:
    """最近收盘价 (amount 折算参考价)。读最新分区日 raw close。"""
    base = paper._daily_path(data_dir, asset_type)
    if not base.exists():
        return None
    try:
        import polars as pl
        df = (
            pl.scan_parquet((base / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol") == symbol)
            .sort("date")
            .select(pl.col("close").last())
            .collect()
        )
    except Exception:
        return None
    if df.is_empty() or df["close"][0] is None:
        return None
    return float(df["close"][0])


@router.get("/accounts")
def list_accounts(request: Request):
    """账户列表 (带最新净值摘要, 供前端切换器)。"""
    return {"accounts": paper.list_accounts(_data_dir(request))}


@router.get("/account")
def get_account(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    acc = paper.get_account(_data_dir(request), _acc(request, account))
    return {"account": acc}


@router.post("/account")
def create_account(request: Request, body: AccountModel):
    try:
        acc = paper.create_account(
            _data_dir(request),
            body.initial_cash,
            account_id=_acc(request, body.account_id),
            name=body.name,
            commission_pct=body.commission_pct,
            stamp_tax_pct=body.stamp_tax_pct,
            slippage_bps=body.slippage_bps,
            queue_limit_orders=body.queue_limit_orders,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


@router.get("/overview")
def overview(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """总览: 现金 + 持仓估值 + 账户状态。

    盘中(交易时段且实时快照就绪)用当日实时 raw_close 估算 (estimating=true),
    其余时刻用最近日线收盘价 (与收盘定版口径一致)。
    """
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        return {"initialized": False, "account_id": acc_id}
    ov = paper.overview(data_dir, account_id=acc_id)
    ov["fees"] = {k: acc[k] for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")}

    # 盘中实时估算: 从行情服务的当日 enriched 快照取 raw_close (软失败, 降级日线)
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None and acc.get("status") == "active":
        try:
            enriched, enriched_date = qs.get_enriched_today()
            from app.market_time import cn_today
            if not enriched.is_empty() and enriched_date == cn_today() and ov.get("holdings"):
                snapshot = dict(zip(enriched["symbol"].to_list(), enriched["raw_close"].to_list(), strict=False))
                live = paper.overview(data_dir, snapshot, account_id=acc_id)
                live["fees"] = ov["fees"]
                return live
        except Exception as e:
            logger.warning("paper overview 实时估算失败, 降级日线口径: %s", e)
    return ov


@router.post("/orders")
def create_order(request: Request, body: OrderModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    asset_type = _resolve_asset_type(request, body.symbol)
    ref_price = body.ref_price
    if ref_price is None:
        ref_price = _last_close(data_dir, body.symbol, asset_type)
    order, err = paper.create_order(
        data_dir, body.symbol, body.side,
        account_id=acc_id,
        qty=body.qty, amount=body.amount,
        order_type=body.order_type, asset_type=asset_type, ref_price=ref_price,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"order": order}


@router.get("/orders")
def list_orders(request: Request, status: str | None = None, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    orders = paper.load_orders(_data_dir(request), _acc(request, account))
    if status:
        orders = [o for o in orders if o["status"] == status]
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    return {"orders": orders}


@router.delete("/orders/{order_id}")
def cancel_order(request: Request, order_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    order, err = paper.cancel_order(_data_dir(request), order_id, _acc(request, account))
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"order": order}


@router.get("/trades")
def list_trades(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    fills = paper.load_fills(_data_dir(request), _acc(request, account))
    fills.sort(key=lambda f: f.get("seq", 0), reverse=True)
    return {"fills": fills}


@router.get("/positions")
def list_positions(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    ov = paper.overview(data_dir, account_id=_acc(request, account))
    return {"holdings": ov.get("holdings", []), "initialized": ov.get("initialized", False)}


@router.get("/nav")
def list_nav(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return {"nav": paper.load_nav(_data_dir(request), _acc(request, account))}


@router.get("/stats")
def get_stats(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return paper.stats(_data_dir(request), _acc(request, account))


@router.get("/compare")
def compare_accounts(request: Request):
    """横向对比全部账户: 概览 + 回合统计 + 定版净值 (供对比表与净值叠加图)。

    净值给全量定版序列, 前端做归一化 (起点=1) 后多账户叠加。
    """
    data_dir = _data_dir(request)
    rows = []
    for acc_id in paper.list_account_ids(data_dir):
        acc = paper.get_account(data_dir, acc_id)
        if acc is None:
            continue
        ov = paper.overview(data_dir, account_id=acc_id)
        if not ov.get("initialized"):
            continue  # 空壳目录 (懒创建) 不进对比
        st = paper.stats(data_dir, acc_id)
        initial = float(acc.get("initial_cash") or 0)
        rows.append({
            "account": acc_id,
            "name": acc.get("name") or acc_id,
            "status": acc.get("status"),
            "initial_cash": initial,
            "fees": {k: acc.get(k) for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")},
            "total": ov.get("total"),
            "cash": ov.get("cash"),
            "market_value": ov.get("market_value"),
            "total_pnl": ov.get("total_pnl"),
            "pnl_pct": round((ov.get("total_pnl") or 0) / initial * 100, 2) if initial > 0 else None,
            "rounds": st.get("rounds"),
            "win_rate": st.get("win_rate"),
            "profit_loss_ratio": st.get("profit_loss_ratio"),
            "avg_holding_days": st.get("avg_holding_days"),
            "realized_pnl": st.get("realized_pnl"),
            "max_drawdown": st.get("max_drawdown"),
            "nav": [{"date": n["date"], "nav": n["nav"]} for n in paper.load_nav(data_dir, acc_id)],
        })
    return {"accounts": rows}


@router.post("/rebuild")
def rebuild(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """由成交台账重建物化持仓与现金 (修复兜底)。"""
    positions = paper.rebuild_positions(_data_dir(request), _acc(request, account))
    return {"symbols": len(positions)}


@router.post("/freeze")
def freeze(request: Request, frozen: bool = True, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """冻结/解冻账户: 冻结后拒绝新订单, 持仓只读。"""
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        raise HTTPException(status_code=400, detail="尚未创建模拟账户")
    acc["status"] = "frozen" if frozen else "active"
    paper.save_account(data_dir, acc, acc_id)
    return {"account": acc}


@router.post("/settings")
def update_settings(request: Request, body: SettingsModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """更新账户设置 (涨跌停排队等开关)。"""
    try:
        acc = paper.update_settings(_data_dir(request), _acc(request, account), **body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


@router.get("/llm_status")
def llm_status(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """LLM 跟单开关状态: 当前模式 / AI 是否就绪 / 标的池 / 上次决策。

    供 UI 的「条件跟单 / LLM跟单」开关展示与排障。
    """
    from app.strategy import paper_llm

    return paper_llm.status(_data_dir(request), _acc(request, account))


@router.get("/llm_stats")
def llm_stats(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID),
              days: int = Query(7, ge=1, le=60)):
    """LLM 跟单**触发统计**: 四个形态各触发几次/何时 + 决策结果 + 未下单原因。

    用于跑几天后回头校准阈值: 形态频繁触发但 `ordered=0` 时, 直接看
    `reject_reasons` 就知道卡在哪（额度/无持仓/均价线缺失/模型 hold 等）。
    """
    from app.strategy import paper_llm

    return paper_llm.stats(_data_dir(request), _acc(request, account), days=days)


class DebateRequest(BaseModel):
    account: str = paper.DEFAULT_ACCOUNT_ID
    rounds: int = 3
    # 手动召集只出结论、不下单（默认）；true 时才把结论落成一次决策留痕
    record_intent: bool = True


@router.post("/debate")
async def run_debate_now(request: Request, req: DebateRequest):
    """**手动召集五派辩论**（页面按钮）——只出结论, 不执行任何委托。

    与自动流程的区别: 自动流程只在海平有出手意图时才召集, 这里随点随跑;
    结论会写进 `channel=debate` 的对话留痕（面板「五派辩论」档可见）。
    """
    from app.strategy import paper_debate, paper_llm

    data_dir = _data_dir(request)
    account_id = _acc(request, req.account)
    acc = paper.get_account(data_dir, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="模拟账户不存在")

    # 复用决策上下文（同一套盘口/持仓/记忆），但**不**调用海平、不下单。
    ctx, market = paper_llm.build_manual_context(data_dir, account_id, acc)
    intent = None
    if req.record_intent:
        intent = {"action": "manual", "reason": "用户在模拟盘页面手动召集五派辩论"}
    result = await paper_debate.run_debate(
        data_dir, account_id, ctx, market=market, intent=intent,
        rounds=max(1, min(int(req.rounds or 3), 3)),
        model=paper_llm.debate_model_of(acc),
        model_by_id=paper_llm.debate_models_of(acc),
    )
    paper_llm.note_manual_debate(data_dir, account_id, result)
    return {
        "ok": result.get("ok"),
        "rounds": result.get("rounds"),
        "degraded": result.get("degraded"),
        "consensus": (result.get("debate") or {}).get("consensus"),
        "stance_count": (result.get("debate") or {}).get("stance_count"),
        "oppose_new_position": (result.get("debate") or {}).get("oppose_new_position"),
        "gate": result.get("gate"),
        "summary": result.get("summary"),
        "statements": [
            {
                "name": s.get("name"), "school": s.get("school"),
                "stance": (s.get("stmt") or {}).get("stance"),
                "action_bias": (s.get("stmt") or {}).get("action_bias"),
                "oppose_new_position": (s.get("stmt") or {}).get("oppose_new_position"),
                "points": (s.get("stmt") or {}).get("points"),
                "error": s.get("error"),
            }
            for s in ((result.get("debate") or {}).get("statements") or [])
        ],
    }


# ===== 自动跟单规则 (V2) =====
class AutoRuleModel(BaseModel):
    name: str
    match_kind: str                 # strategy / rule
    match_id: str
    side: str = "buy"
    size_mode: str = "fixed_amount" # fixed_amount / pct_equity
    size_value: float
    order_type: str = "next_open"
    cooldown_days: int = 5
    enabled: bool = True


@router.get("/auto_rules")
def list_auto_rules(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    return {"rules": paper_auto.load_auto_rules(_data_dir(request), _acc(request, account))}


@router.post("/auto_rules")
def create_auto_rule(request: Request, body: AutoRuleModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    try:
        rule = paper_auto.create_auto_rule(_data_dir(request), body.model_dump(), _acc(request, account))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"rule": rule}


@router.post("/auto_rules/{rule_id}/enabled")
def set_auto_rule_enabled(request: Request, rule_id: str, enabled: bool, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    rule = paper_auto.set_enabled(_data_dir(request), rule_id, enabled, _acc(request, account))
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"rule": rule}


@router.delete("/auto_rules/{rule_id}")
def delete_auto_rule(request: Request, rule_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    if not paper_auto.delete_auto_rule(_data_dir(request), rule_id, _acc(request, account)):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"ok": True}


# ===== 智能体记忆 / 知识库 / 每日收盘复盘 =====

class KnowledgeModel(BaseModel):
    title: str
    body: str = ""
    tags: list[str] | None = None


class ReviewScheduleModel(BaseModel):
    enabled: bool = True
    hour: int = 15
    minute: int = 35


@router.get("/memory")
def get_memory(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID),
               reviews: int = Query(10, ge=1, le=60),
               lessons: int = Query(50, ge=1, le=200)):
    """智能体的记忆总览: 自我画像 + 最近复盘 + 经验教训 + 知识库 + 调度状态。"""
    from app.services import preferences
    from app.strategy import paper_memory

    aid = _acc(request, account)
    data_dir = _data_dir(request)
    return {
        "status": paper_memory.status(data_dir, aid),
        "reviews": paper_memory.list_reviews(data_dir, aid, limit=reviews),
        "lessons": paper_memory.load_lessons(data_dir, aid)[:lessons],
        "knowledge": paper_memory.load_knowledge(data_dir, aid),
        "schedule": preferences.get_paper_review_schedule(),
    }


@router.get("/memory/reviews")
def list_memory_reviews(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID),
                        limit: int = Query(30, ge=1, le=400)):
    from app.strategy import paper_memory

    return {"reviews": paper_memory.list_reviews(_data_dir(request), _acc(request, account), limit=limit)}


@router.get("/memory/reviews/{day}")
def get_memory_review(request: Request, day: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_memory

    obj = paper_memory.get_review(_data_dir(request), _acc(request, account), day)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"无该日复盘记录: {day}")
    return {"review": obj}


@router.post("/memory/review")
def run_memory_review(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID),
                      day: str | None = Query(None, description="交易日 YYYY-MM-DD, 缺省今天"),
                      force: bool = Query(False, description="已有当日复盘时是否重跑")):
    """立即跑一次收盘复盘（写记忆/知识库并更新自我画像）。"""
    from app.strategy import paper_memory

    res = paper_memory.run_daily_review(_data_dir(request), _acc(request, account), day=day, force=force)
    if not res.get("ok"):
        # LLM 未配置/失败属于"可重试的业务失败", 不是 5xx
        return {"ok": False, "day": res.get("day"), "error": res.get("error")}
    return res


@router.get("/memory/knowledge")
def list_memory_knowledge(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_memory

    return {"knowledge": paper_memory.load_knowledge(_data_dir(request), _acc(request, account))}


@router.post("/memory/knowledge")
def add_memory_knowledge(request: Request, body: KnowledgeModel,
                         account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_memory

    entry = paper_memory.add_knowledge(
        _data_dir(request), _acc(request, account),
        title=body.title, body=body.body, tags=body.tags or [], source="manual",
    )
    if entry is None:
        raise HTTPException(status_code=400, detail="title 不能为空")
    return {"knowledge": entry}


@router.put("/memory/knowledge/{kb_id}")
def update_memory_knowledge(request: Request, kb_id: str, body: KnowledgeModel,
                            account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_memory

    entry = paper_memory.update_knowledge(
        _data_dir(request), _acc(request, account), kb_id,
        title=body.title, body=body.body, tags=body.tags or [],
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"知识条目不存在: {kb_id}")
    return {"knowledge": entry}


@router.delete("/memory/knowledge/{kb_id}")
def delete_memory_knowledge(request: Request, kb_id: str,
                            account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_memory

    if not paper_memory.delete_knowledge(_data_dir(request), _acc(request, account), kb_id):
        raise HTTPException(status_code=404, detail=f"知识条目不存在: {kb_id}")
    return {"ok": True}


@router.delete("/memory/lessons/{lesson_id}")
def archive_memory_lesson(request: Request, lesson_id: str,
                          account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """归档一条经验（归档后不再注入决策提示词）。"""
    from app.strategy import paper_memory

    if not paper_memory.retire_lesson(_data_dir(request), _acc(request, account), lesson_id):
        raise HTTPException(status_code=404, detail=f"经验不存在: {lesson_id}")
    return {"ok": True}


@router.get("/memory/schedule")
def get_memory_schedule():
    from app.services import preferences

    return preferences.get_paper_review_schedule()


@router.put("/memory/schedule")
def set_memory_schedule(body: ReviewScheduleModel):
    from app.services import preferences

    return preferences.set_paper_review_schedule(body.enabled, body.hour, body.minute)


# ===== 智能体 ↔ LLM 对话记录 / 用户提问 =====

class ChatModel(BaseModel):
    question: str


@router.get("/conversation")
def list_conversation(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID),
                      channel: str | None = Query(None, description="decision / review / chat"),
                      limit: int = Query(50, ge=1, le=500),
                      with_request: bool = Query(False, description="是否带上完整请求体(较大)")):
    """智能体与 LLM 的对话记录(新→旧)。

    `channel`: `decision` 盘中决策 / `review` 收盘复盘 / `chat` 我的提问。
    `with_request=false` 时列表不带请求体(仅给字符数), 点开单条再取全文。
    """
    from app.strategy import paper_conversation

    aid = _acc(request, account)
    data_dir = _data_dir(request)
    return {
        "turns": paper_conversation.list_turns(data_dir, aid, channel=channel,
                                               limit=limit, with_request=with_request),
        "stats": paper_conversation.stats(data_dir, aid),
        "labels": paper_conversation.CHANNEL_LABELS,
    }


@router.get("/conversation/{turn_id}")
def get_conversation_turn(request: Request, turn_id: str,
                          account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """单条对话往返的完整内容（含发给模型的请求体）。"""
    from app.strategy import paper_conversation

    turn = paper_conversation.get_turn(_data_dir(request), _acc(request, account), turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail=f"对话记录不存在: {turn_id}")
    return {"turn": turn}


@router.delete("/conversation")
def clear_conversation(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """清空该账户的对话记录。"""
    from app.strategy import paper_conversation

    n = paper_conversation.clear_turns(_data_dir(request), _acc(request, account))
    return {"ok": True, "removed": n}


@router.post("/chat")
async def ask_agent(request: Request, body: ChatModel,
                    account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """向智能体提问（非流式，兜底/脚本用；前端优先用 /chat/stream）。

    注意: **只做分析问答，不会下单**。要交易请走「跟单模式 → LLM跟单」或手动下单。
    """
    from app.strategy import paper_conversation

    return await paper_conversation.chat_once(_data_dir(request), _acc(request, account), body.question)


@router.post("/chat/stream")
async def ask_agent_stream(request: Request, body: ChatModel,
                           account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """向智能体提问 — NDJSON 流式返回，边生成边显示。

    协议: {"type":"meta","model"} / {"type":"delta","content"} /
          {"type":"ping"} / {"type":"error","message"} / {"type":"done","turn_id"}
    问答会写入对话记录（channel=chat），下次提问自动带上最近几轮。
    """
    from fastapi.responses import StreamingResponse

    from app.services.ndjson_heartbeat import with_heartbeat
    from app.strategy import paper_conversation

    aid = _acc(request, account)
    data_dir = _data_dir(request)

    async def gen():
        async for chunk in with_heartbeat(
            paper_conversation.chat_stream(data_dir, aid, body.question)
        ):
            yield chunk + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
