"""五派辩论引擎 — 四位顾问（蔡森/缠论/CAN SLIM/K线战法）三轮讨论, 结论交海平裁判。

设计要点（与用户确认的口径）:
  - **触发**: 不是每轮都跑。海平先"初判", 只有它**有出手意图**(action≠hold)时才召集,
    跑完辩论再让海平"终判"。持有/观望的常规 tick 零额外成本。
  - **权威**: 海平是最终裁判; 但若 **≥3 派明确反对开新仓**, 代码层闸门禁止开仓
    (减仓/止损不受限) —— 见 :func:`risk_gate`。
  - **可见**: 每派每轮都写一条 channel="debate" 的对话留痕, 面板可逐条看它们怎么吵。

失败纪律: 任一派调用失败只记为"该派弃权"(degraded), 不影响其余派与最终下单;
辩论整体失败(全部弃权)时返回 ok=False, 由调用方决定是否跳过本轮出手。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from app.services import ai_provider
from app.strategy.paper_personas import ANALYSTS, analyst_system_prompt

logger = logging.getLogger(__name__)

# 默认轮数: 第 1 轮独立分析 → 第 2 轮互相看到观点后修正 → 第 3 轮各自给定论
DEFAULT_ROUNDS = 3
# 单派单轮的超时(秒): 四派并行, 一轮 ≈ 该值上限
_ANALYST_TIMEOUT_S = 180.0
# 每轮输入里上一轮发言的截断长度(防止上下文滚雪球)
_MAX_STATEMENT_CHARS_IN_PROMPT = 1600
# 记录到对话里的单条发言上限
_MAX_STATEMENT_CHARS_STORED = 6000
# 风控闸门: 明确反对开新仓的派数达到该值 → 禁止开仓
RISK_GATE_OPPOSE_THRESHOLD = 3

_ROUND_HINT = {
    1: "这是第 1 轮：独立分析当前盘面与持仓，给出你的立场。",
    2: "这是第 2 轮：先看你同伴的意见，再修正或坚持你的立场。",
    3: "这是第 3 轮：给定论 —— 明确表态、关键价位与失效条件。",
}


def _compact_features(ctx: dict[str, Any], limit: int = 6) -> dict[str, Any]:
    """只带辩论需要的盘口字段, 避免把整份 4 万字上下文×4 派重复发送。"""
    feats = ctx.get("features") or {}
    out: dict[str, Any] = {}
    for sym, f in list(feats.items())[:limit]:
        if not isinstance(f, dict):
            continue
        keep = ("price", "day_open", "prev_close", "pct_from_open", "vwap",
                "high", "low", "amount", "vol_ratio_5m_today")
        out[sym] = {k: f.get(k) for k in keep if f.get(k) is not None}
    return out


def build_debate_payload(ctx: dict[str, Any], market: dict[str, Any] | None,
                         intent: dict[str, Any] | None = None) -> dict[str, Any]:
    """四派共用的"事实包"（客观数据 + 海平初判意图, 不含任何一派的结论）。"""
    payload: dict[str, Any] = {
        "market": market or {},
        "features": _compact_features(ctx),
        "positions": ctx.get("positions") or {},
        "orders_today": [
            {k: o.get(k) for k in ("symbol", "side", "qty", "status")}
            for o in (ctx.get("orders_today") or [])
        ],
        "account": {
            k: ctx.get(k) for k in ("cash", "nav", "day_pnl", "total_pnl") if ctx.get(k) is not None
        },
    }
    if intent:
        # 海平初判的"出手意图"要给四派看到, 否则无从"反对/支持这次出手"
        payload["haiping_preliminary_intent"] = {
            k: intent.get(k) for k in ("action", "symbol", "qty", "reason", "basis")
            if intent.get(k) is not None
        }
    return payload


def _parse_statement(text: str) -> dict[str, Any] | None:
    """容错解析某派的 JSON 发言（支持 ```json 围栏与前后废话）。"""
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start:end + 1])
        except Exception:  # noqa: BLE001
            return None
    return obj if isinstance(obj, dict) else None


def _statement_line(analyst_name: str, stmt: dict[str, Any] | None, raw: str) -> str:
    """给下一轮同伴看的一句话摘要。"""
    if not stmt:
        return f"【{analyst_name}】未能给出结构化结论（原文节选）：{raw[:300]}"
    points = "; ".join(str(p) for p in (stmt.get("points") or [])[:3])
    levels = stmt.get("key_levels") or {}
    lv = "、".join(f"{k}:{v}" for k, v in levels.items() if v)
    return (
        f"【{analyst_name}】立场={stmt.get('stance')} 倾向={stmt.get('action_bias')} "
        f"反对开新仓={bool(stmt.get('oppose_new_position'))} 信心={stmt.get('confidence')}"
        + (f" 关键位({lv})" if lv else "")
        + (f" 要点: {points}" if points else "")
    )


async def _run_analyst(analyst: dict, payload: dict[str, Any], round_no: int,
                       prior_lines: list[str],
                       model: str | None = None) -> tuple[dict[str, Any] | None, str, str | None]:
    """跑一派的单轮。返回 (parsed_or_None, raw_text, error_or_None)。

    ``model``: 四派可单独指定模型（None/空 = 跟随全局 AI 设置）—— 一轮辩论 12 次
    调用, 让四派跑轻量快模型可显著提速。
    """
    user_parts = [
        f"# 五派辩论 · 第 {round_no} 轮",
        _ROUND_HINT.get(round_no, ""),
        "",
        "## 客观事实（JSON，禁止编造其它数字）",
        json.dumps(payload, ensure_ascii=False, default=str),
    ]
    if prior_lines:
        user_parts += ["", "## 上一轮各派发言（供你参考/反驳）"]
        for line in prior_lines:
            user_parts.append("- " + line[:_MAX_STATEMENT_CHARS_IN_PROMPT])
    messages = [
        {"role": "system", "content": analyst_system_prompt(analyst)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    try:
        text = await ai_provider.generate_ai_text(
            messages, temperature=0.2, max_tokens=None, timeout=_ANALYST_TIMEOUT_S,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 — 单派失败只算弃权
        return None, "", f"{type(exc).__name__}: {exc}"
    text = (text or "").strip()
    if not text:
        return None, "", "空响应"
    stmt = _parse_statement(text)
    if stmt is None:
        return None, text, "未得到有效 JSON"
    return stmt, text, None


def _record_turn(data_dir: Path, account_id: str, analyst: dict, round_no: int,
                 payload: dict[str, Any], raw: str, stmt: dict[str, Any] | None,
                 error: str | None, model: str | None = None) -> None:
    """每派每轮一条 channel=debate 留痕（失败静默, 绝不影响交易）。"""
    try:
        from app.strategy import paper_conversation

        paper_conversation.record_turn(
            data_dir, account_id, channel="debate",
            request={
                "round": round_no,
                "analyst": analyst["name"],
                "analyst_id": analyst["id"],
                "school": analyst.get("school"),
                "model": model or "(跟随全局)",
                "facts": payload,
            },
            response=(raw or "(无发言)")[:_MAX_STATEMENT_CHARS_STORED],
            parsed=stmt,
            meta={
                "round": round_no,
                "analyst_id": analyst["id"],
                "analyst_name": analyst["name"],
                "school": analyst.get("school"),
                "model": model or "",
                "stance": (stmt or {}).get("stance"),
                "action_bias": (stmt or {}).get("action_bias"),
                "oppose_new_position": bool((stmt or {}).get("oppose_new_position")),
                "confidence": (stmt or {}).get("confidence"),
                "degraded": stmt is None,
            },
            system_chars=len(analyst_system_prompt(analyst)),
            error=error,
        )
    except Exception:
        logger.debug("record debate turn failed", exc_info=True)


# 立场归一化: 四派可能各用不同模型, 实测豆包系会回英文 ("neutral"/"bullish"),
# 若不归一, synthesize 会把 "neutral" 与 "中性" 当成两种立场分别计数。
_STANCE_ALIASES = {
    "bullish": "多", "bull": "多", "long": "多", "多": "多", "看多": "多", "偏多": "多",
    "bearish": "空", "bear": "空", "short": "空", "空": "空", "看空": "空", "偏空": "空",
    "neutral": "中性", "mixed": "中性", "flat": "中性", "中性": "中性", "观望": "中性",
}


def normalize_stance(value: Any) -> str:
    """把模型的立场表述归一成 多/空/中性（未知值原样返回, 便于发现新表述）。"""
    text = str(value or "").strip()
    return _STANCE_ALIASES.get(text.lower(), text or "未知")


def synthesize(rounds: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """把最后一轮的立场汇总成共识/分歧 + 关键位 + 反对票（纯代码, 不额外调模型）。"""
    final = [s for s in (rounds[-1] if rounds else []) if s.get("stmt")]
    stance_count: dict[str, int] = {}
    bias_count: dict[str, int] = {}
    levels: dict[str, list[str]] = {}
    oppose = 0
    for s in final:
        stmt = s["stmt"]
        stance = normalize_stance(stmt.get("stance"))
        stance_count[stance] = stance_count.get(stance, 0) + 1
        bias_count[str(stmt.get("action_bias") or "未知")] = bias_count.get(
            str(stmt.get("action_bias") or "未知"), 0) + 1
        if stmt.get("oppose_new_position"):
            oppose += 1
        for k, v in (stmt.get("key_levels") or {}).items():
            if v:
                levels.setdefault(str(k), []).append(str(v))
    total = len(final)
    consensus = ""
    if total:
        top = max(stance_count.items(), key=lambda kv: kv[1])
        consensus = f"{total} 派表态: " + "、".join(
            f"{k} {v} 派" for k, v in sorted(stance_count.items(), key=lambda kv: -kv[1])
        )
        if top[1] > total / 2:
            consensus += f" → 多数偏{top[0]}"
        else:
            consensus += " → 无明显多数（分歧）"
    return {
        "statements": final,
        "stance_count": stance_count,
        "action_bias_count": bias_count,
        "oppose_new_position": oppose,
        "responded": total,
        "consensus": consensus,
        "key_levels": levels,
        "last_round": len(rounds),
    }


def risk_gate(debate: dict[str, Any]) -> dict[str, Any]:
    """≥RISK_GATE_OPPOSE_THRESHOLD 派明确反对开新仓 → 禁止开仓（减仓/止损不受限）。"""
    oppose = int(debate.get("oppose_new_position") or 0)
    allow_open = oppose < RISK_GATE_OPPOSE_THRESHOLD
    return {
        "allow_open": allow_open,
        "oppose_count": oppose,
        "threshold": RISK_GATE_OPPOSE_THRESHOLD,
        "reason": (
            "" if allow_open
            else f"{oppose} 派明确反对开新仓（阈值 {RISK_GATE_OPPOSE_THRESHOLD}）→ 本轮禁止开仓, 只允许减仓/止损"
        ),
    }


def render_for_haiping(debate: dict[str, Any], gate: dict[str, Any]) -> str:
    """给海平看的辩论纪要（纯文本块, 注入它的 user 上下文）。"""
    lines = ["## 五派辩论纪要（四派顾问, 由你最终裁定）"]
    if debate.get("consensus"):
        lines.append(f"- 汇总: {debate['consensus']}")
    if debate.get("key_levels"):
        lv = "；".join(
            f"{k}=" + "/".join(dict.fromkeys(v)) for k, v in debate["key_levels"].items()
        )
        lines.append(f"- 各派关键位: {lv}")
    for s in debate.get("statements") or []:
        stmt = s.get("stmt") or {}
        lines.append(
            f"- 【{s['name']}】立场={stmt.get('stance')} 倾向={stmt.get('action_bias')} "
            f"反对开新仓={bool(stmt.get('oppose_new_position'))}"
            + (f" 失效条件: {stmt.get('invalidation')}" if stmt.get("invalidation") else "")
        )
        for p in (stmt.get("points") or [])[:3]:
            lines.append(f"    · {p}")
    if not debate.get("responded"):
        lines.append("- 注意: 本轮四派均未给出有效结论 —— 请勿据此加仓, 按你自己的纪律执行。")
    if not gate.get("allow_open"):
        lines.append(f"- **风控闸门已触发**: {gate.get('reason')}")
    return "\n".join(lines)


async def run_debate(
    data_dir: Path,
    account_id: str,
    ctx: dict[str, Any],
    *,
    market: dict[str, Any] | None = None,
    intent: dict[str, Any] | None = None,
    rounds: int = DEFAULT_ROUNDS,
    model: str | None = None,
    model_by_id: dict[str, str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """召集四派三轮辩论。返回 {ok, rounds, debate, gate, summary, degraded}。

    ``model``: 四派统一使用的模型（None/空 = 跟随全局 AI 设置）。
    ``model_by_id``: 逐派模型覆盖 {analyst_id: model}；某项为空则回落到 ``model``。
    """
    rounds = max(1, min(int(rounds or DEFAULT_ROUNDS), 3))
    payload = build_debate_payload(ctx, market, intent)
    all_rounds: list[list[dict[str, Any]]] = []
    degraded_total = 0

    for round_no in range(1, rounds + 1):
        prior_lines = (
            [_statement_line(s["name"], s.get("stmt"), s.get("raw") or "")
             for s in all_rounds[-1]]
            if all_rounds else []
        )
        if on_progress:
            on_progress(f"第 {round_no}/{rounds} 轮：四派并行分析中…")

        # 先算出**每派实际生效的模型**，调用与留痕都用它 —— 否则留痕会记成统一值,
        # 与实际调用不一致（"用了哪个模型"就查不出来了）。
        per_analyst_models = {
            a["id"]: ((model_by_id or {}).get(a["id"]) or model) for a in ANALYSTS
        }
        results = await asyncio.gather(*[
            _run_analyst(a, payload, round_no, prior_lines, model=per_analyst_models[a["id"]])
            for a in ANALYSTS
        ])

        rows: list[dict[str, Any]] = []
        for analyst, (stmt, raw, err) in zip(ANALYSTS, results, strict=True):
            if err:
                degraded_total += 1
                logger.warning(
                    "五派辩论第 %d 轮 %s 弃权: %s", round_no, analyst["name"], err,
                )
            _record_turn(data_dir, account_id, analyst, round_no, payload, raw, stmt, err,
                         model=per_analyst_models[analyst["id"]])
            rows.append({
                "id": analyst["id"], "name": analyst["name"],
                "school": analyst.get("school"), "stmt": stmt, "raw": raw, "error": err,
            })
        all_rounds.append(rows)

    debate = synthesize(all_rounds)
    gate = risk_gate(debate)
    result = {
        "ok": bool(debate["responded"]),
        "rounds": rounds,
        "debate": debate,
        "gate": gate,
        "summary": render_for_haiping(debate, gate),
        "degraded": degraded_total,
    }
    if on_progress:
        on_progress(
            f"辩论完成：{debate['responded']}/{len(ANALYSTS)} 派给出结论"
            + (f"，{debate['oppose_new_position']} 派反对开新仓" if debate["oppose_new_position"] else "")
            + ("" if gate["allow_open"] else "（闸门触发：禁止开仓）")
        )
    return result
