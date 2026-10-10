# -*- coding: utf-8 -*-
"""模拟盘智能体的「记忆 + 知识库 + 每日收盘复盘」。

给 TSP 内置的「海平分时擒牛」交易智能体（``paper_llm`` LLM 跟单）补上三件事：

1. **记忆（lessons / profile）** —— 每天收盘复盘产出的经验教训与自我认知，
   持久化在账户目录下，并在**每次决策**时注入提示词，让后续判断不再重复犯错。
2. **知识库（knowledge）** —— 可检索的方法论条目 + 复盘沉淀出的规则；
   首次使用时自动播种《海平分时擒牛》核心要点。
3. **每日收盘复盘** —— 工作日收盘后自动跑：汇总当日成交/委托/平仓盈亏/形态触发，
   叫一次 LLM 产出结构化反思（做对了什么/做错了什么/教训/新规则/下一步关注），
   落盘为当日复盘记录，并回写经验、知识库与自我画像。

存储布局（``data/paper/accounts/<account>/memory/``）::

    profile.json          自我画像（滚动业绩 + 当前关注点 + 已内化纪律）
    lessons.jsonl         经验教训（append-only, 相似条目自动合并计数）
    knowledge.jsonl       知识库（方法论 + 复盘沉淀规则）
    state.json            调度状态（已复盘的日期 / 最近一次失败原因）
    daily/<YYYY-MM-DD>.json   当日复盘记录（含指标、成交、决策、模型反思）

设计约束:
  - **fail-open 但绝不编造**: 复盘取数全部来自本地已落盘的事实（paper 台账 /
    决策流水 / 分时形态历史）；缺哪块就标 null 并在 payload 里说明，不臆造。
  - **LLM 不可用不阻塞交易**: 复盘只写记忆，不参与下单路径；LLM 失败时记录
    ``state.last_error`` 供 UI 显示，且不影响实盘决策。
  - **幂等**: 同一天重复复盘默认跳过（``force=True`` 可覆盖）；写盘走原子替换。
  - **与 paper_llm 解耦**: 本模块不导入 paper_llm 的顶层符号（仅函数内懒导入），
    避免循环导入；paper_llm 反过来只懒导入本模块的 ``build_memory_context``。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from app.market_time import cn_now, cn_today
from app.services.fs_utils import atomic_write_text
from app.strategy import paper

logger = logging.getLogger(__name__)

MEMORY_SUBDIR = "memory"
# 提示词注入上限（控制 token；超出的按重要度截断）
CTX_MAX_REVIEWS = 3
CTX_MAX_LESSONS = 10
CTX_MAX_KNOWLEDGE = 5
# 长期上限（防无限增长）
MAX_LESSONS = 200
MAX_KNOWLEDGE = 200
# 复盘调度默认时间（收盘 + 盘后管道之后）
DEFAULT_REVIEW_HOUR = 15
DEFAULT_REVIEW_MINUTE = 35

# 写盘互斥（同账户的复盘/知识库写入；paper 域锁只管交易台账）
_WRITE_LOCK = threading.Lock()

# 相似经验合并阈值（去标点后的字符 3-gram Jaccard）
_LESSON_MERGE_RATIO = 0.72

_PUNCT_RE = re.compile(r"[\s,，。;；:：!！?？、\"'“”‘’()（）\[\]【】<>《》/\\|~`@#$%^&*+=_-]+")


# ══════════════════════════════════════════════════════════════════
# 路径与基础读写
# ══════════════════════════════════════════════════════════════════
def memory_root(data_dir: Path, account_id: str) -> Path:
    """账户的记忆根目录（不存在时创建）。"""
    root = paper._root(data_dir, account_id) / MEMORY_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _daily_dir(data_dir: Path, account_id: str) -> Path:
    d = memory_root(data_dir, account_id) / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(data_dir: Path, account_id: str) -> Path:
    return memory_root(data_dir, account_id) / "profile.json"


def _lessons_path(data_dir: Path, account_id: str) -> Path:
    return memory_root(data_dir, account_id) / "lessons.jsonl"


def _knowledge_path(data_dir: Path, account_id: str) -> Path:
    return memory_root(data_dir, account_id) / "knowledge.jsonl"


def _state_path(data_dir: Path, account_id: str) -> Path:
    return memory_root(data_dir, account_id) / "state.json"


def _review_path(data_dir: Path, account_id: str, day: str) -> Path:
    return _daily_dir(data_dir, account_id) / f"{day}.json"


def _read_json(path: Path) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("memory json read failed: %s", path, exc_info=True)
    return None


def _write_json(path: Path, obj: Any) -> None:
    try:
        atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception:
        logger.warning("memory json write failed: %s", path, exc_info=True)


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        if not path.exists():
            return out
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except Exception:
        logger.debug("memory jsonl read failed: %s", path, exc_info=True)
    return out


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    try:
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        atomic_write_text(path, text)
    except Exception:
        logger.warning("memory jsonl write failed: %s", path, exc_info=True)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{os.urandom(2).hex()}"


def _norm_text(s: Any) -> str:
    return _PUNCT_RE.sub("", str(s or "").strip().lower())


def _trigrams(s: str) -> set[str]:
    if len(s) <= 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _similar(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / max(1, len(ta | tb))


def _clip(v: Any, n: int) -> str:
    s = str(v or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _as_list(v: Any, *, limit: int = 20, width: int = 200) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for item in v:
        s = _clip(item, width)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _fnum(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


# ══════════════════════════════════════════════════════════════════
# 知识库
# ══════════════════════════════════════════════════════════════════
# 首次使用时播种的方法论要点（源自《海平分时擒牛》全7讲；与 paper_llm 提示词同源）
_SEED_KNOWLEDGE: tuple[dict[str, Any], ...] = (
    {
        "title": "核心基准：分时均价线（VWAP）",
        "tags": ["methodology", "基准"],
        "body": "分时均价线是海平体系的中心基准：价在均价线上方为多方主导，下方为空方主导。"
                "所有买点卖点判断先看与均价线的关系，再谈形态。",
    },
    {
        "title": "三个确定性买点（必须量价配合）",
        "tags": ["methodology", "买点"],
        "body": "① 回踩均价线不破并站稳上穿；② V型确认（放量拉升≥3% 后跌破均价线又快速收回，"
                "系统判据 v_shape）；③ 点火/横盘突破（放量破前30分钟高点且相对开盘≥3%，判据 ignite）。",
    },
    {
        "title": "卖出纪律（动能衰竭即走）",
        "tags": ["methodology", "卖点"],
        "body": "跌破分时均价线＝标准 S 点；无量拉升诱多（涨≥3% 但缩量）＝诱多卖点；"
                "量价背离（创新高但缩量）＝减仓；平/低开直接跳水＝离场。",
    },
    {
        "title": "风控纪律：止损优先于一切",
        "tags": ["methodology", "风控"],
        "body": "纪律 > 情绪，止损 > 扛单，胜率 > 单次暴利；硬止损 2-4 个点（系统默认 3%，代码兜底执行）。"
                "没有符合条件的机会就不交易——空仓也是一种操作。",
    },
    {
        "title": "罕见信号不要凑",
        "tags": ["methodology", "经验"],
        "body": "真实样本里多数突破是缩量的（属无量诱多）；ignite 长期为 false 是正常的，"
                "不要为了出手而放宽标准或脑补不存在的形态。",
    },
)


def load_knowledge(data_dir: Path, account_id: str) -> list[dict]:
    """读取知识库条目（按更新时间倒序）。"""
    rows = _read_jsonl(_knowledge_path(data_dir, account_id))
    rows.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
    return rows


def save_knowledge(data_dir: Path, account_id: str, rows: list[dict]) -> None:
    _write_jsonl(_knowledge_path(data_dir, account_id), rows[:MAX_KNOWLEDGE])


def add_knowledge(
    data_dir: Path,
    account_id: str,
    *,
    title: str,
    body: str = "",
    tags: list[str] | None = None,
    source: str = "manual",
    entry_id: str | None = None,
) -> dict | None:
    """新增/更新一条知识库条目（同标题视为同一条，合并正文与标签）。"""
    t = _clip(title, 80)
    if not t:
        return None
    b = _clip(body, 2000)
    tg = [str(x).strip() for x in (tags or []) if str(x).strip()][:8]
    now = cn_now().isoformat(timespec="seconds")
    with _WRITE_LOCK:
        rows = _read_jsonl(_knowledge_path(data_dir, account_id))
        hit: dict | None = None
        for r in rows:
            if (entry_id and r.get("id") == entry_id) or _norm_text(r.get("title")) == _norm_text(t):
                hit = r
                break
        if hit is not None:
            hit["title"] = t
            if b:
                hit["body"] = b
            hit["tags"] = sorted({*(hit.get("tags") or []), *tg})[:8]
            hit["updated_at"] = now
            hit["source"] = hit.get("source") or source
        else:
            hit = {
                "id": entry_id or _new_id("kb"),
                "title": t,
                "body": b,
                "tags": tg,
                "source": source,
                "created_at": now,
                "updated_at": now,
                "hits": 0,
            }
            rows.insert(0, hit)
        rows = rows[:MAX_KNOWLEDGE]
        save_knowledge(data_dir, account_id, rows)
    return hit


def update_knowledge(data_dir: Path, account_id: str, kb_id: str, **fields: Any) -> dict | None:
    with _WRITE_LOCK:
        rows = _read_jsonl(_knowledge_path(data_dir, account_id))
        out: dict | None = None
        for r in rows:
            if r.get("id") != kb_id:
                continue
            if "title" in fields and fields["title"]:
                r["title"] = _clip(fields["title"], 80)
            if "body" in fields:
                r["body"] = _clip(fields["body"], 2000)
            if "tags" in fields and isinstance(fields["tags"], list):
                r["tags"] = [str(x).strip() for x in fields["tags"] if str(x).strip()][:8]
            r["updated_at"] = cn_now().isoformat(timespec="seconds")
            out = r
            break
        if out is not None:
            save_knowledge(data_dir, account_id, rows)
    return out


def delete_knowledge(data_dir: Path, account_id: str, kb_id: str) -> bool:
    with _WRITE_LOCK:
        rows = _read_jsonl(_knowledge_path(data_dir, account_id))
        keep = [r for r in rows if r.get("id") != kb_id]
        if len(keep) == len(rows):
            return False
        save_knowledge(data_dir, account_id, keep)
    return True


def search_knowledge(
    rows: list[dict],
    query: str,
    *,
    tags: list[str] | None = None,
    limit: int = CTX_MAX_KNOWLEDGE,
) -> list[dict]:
    """极简检索：标签命中 + 标题/正文关键词命中打分（无外部依赖/无向量库）。"""
    q = _norm_text(query)
    want_tags = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}
    scored: list[tuple[float, dict]] = []
    for r in rows:
        score = 0.0
        rtags = {str(t).strip().lower() for t in (r.get("tags") or [])}
        if want_tags:
            score += 3.0 * len(want_tags & rtags)
        if q:
            title = _norm_text(r.get("title"))
            body = _norm_text(r.get("body"))
            if q in title:
                score += 4.0
            if q in body:
                score += 2.0
            for token in {q[i:i + 2] for i in range(max(0, len(q) - 1))}:
                if token and token in title:
                    score += 0.4
                if token and token in body:
                    score += 0.15
        if score <= 0:
            continue
        scored.append((score, r))
    scored.sort(key=lambda kv: (-kv[0], -float(kv[1].get("hits") or 0)))
    return [r for _, r in scored[:limit]]


def seed_knowledge(data_dir: Path, account_id: str) -> int:
    """播种方法论要点（幂等；已存在同标题则跳过）。返回新增条数。"""
    existing = {_norm_text(r.get("title")) for r in _read_jsonl(_knowledge_path(data_dir, account_id))}
    added = 0
    for item in _SEED_KNOWLEDGE:
        if _norm_text(item["title"]) in existing:
            continue
        if add_knowledge(data_dir, account_id, title=item["title"], body=item["body"],
                         tags=list(item["tags"]), source="methodology") is not None:
            added += 1
    return added


def ensure_ready(data_dir: Path, account_id: str) -> None:
    """首次使用时的初始化：创建目录 + 播种方法论知识库（幂等，便宜）。"""
    try:
        memory_root(data_dir, account_id)
        if not _knowledge_path(data_dir, account_id).exists():
            seed_knowledge(data_dir, account_id)
    except Exception:
        logger.debug("paper_memory ensure_ready failed", exc_info=True)


# ══════════════════════════════════════════════════════════════════
# 经验教训（记忆）
# ══════════════════════════════════════════════════════════════════
def load_lessons(data_dir: Path, account_id: str, *, active_only: bool = True) -> list[dict]:
    rows = _read_jsonl(_lessons_path(data_dir, account_id))
    if active_only:
        rows = [r for r in rows if str(r.get("status") or "active") == "active"]
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return rows


def append_lessons(
    data_dir: Path,
    account_id: str,
    items: list[dict],
    *,
    day: str,
) -> list[dict]:
    """写入复盘产出的经验；与既有相似条目自动合并（提升 confidence / 计数）。"""
    if not items:
        return []
    now = cn_now().isoformat(timespec="seconds")
    saved: list[dict] = []
    with _WRITE_LOCK:
        rows = _read_jsonl(_lessons_path(data_dir, account_id))
        for raw in items:
            if isinstance(raw, str):
                text = raw
                tags: list[str] = []
                conf = 0.6
                kind = "lesson"
            elif isinstance(raw, dict):
                text = str(raw.get("text") or raw.get("lesson") or "").strip()
                tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()][:6]
                conf = _fnum(raw.get("confidence"))
                conf = 0.6 if conf is None else max(0.0, min(1.0, conf))
                kind = str(raw.get("kind") or "lesson")[:20]
            else:
                continue
            text = _clip(text, 300)
            if len(text) < 4:
                continue
            norm = _norm_text(text)
            hit: dict | None = None
            for r in rows:
                if _similar(_norm_text(r.get("text")), norm) >= _LESSON_MERGE_RATIO:
                    hit = r
                    break
            if hit is not None:
                hit["hits"] = int(hit.get("hits") or 0) + 1
                hit["confidence"] = round(min(1.0, max(_fnum(hit.get("confidence")) or 0.6, conf) + 0.05), 2)
                hit["last_seen"] = day
                hit["tags"] = sorted({*(hit.get("tags") or []), *tags})[:6]
                saved.append(hit)
            else:
                rec = {
                    "id": _new_id("les"),
                    "date": day,
                    "last_seen": day,
                    "kind": kind,
                    "text": text,
                    "tags": tags,
                    "confidence": round(conf, 2),
                    "hits": 0,
                    "status": "active",
                    "created_at": now,
                }
                rows.append(rec)
                saved.append(rec)
        rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
        _write_jsonl(_lessons_path(data_dir, account_id), rows[:MAX_LESSONS])
    return saved


def retire_lesson(data_dir: Path, account_id: str, lesson_id: str) -> bool:
    """归档一条经验（不再注入提示词）。"""
    with _WRITE_LOCK:
        rows = _read_jsonl(_lessons_path(data_dir, account_id))
        ok = False
        for r in rows:
            if r.get("id") == lesson_id:
                r["status"] = "archived"
                ok = True
                break
        if ok:
            _write_jsonl(_lessons_path(data_dir, account_id), rows)
    return ok


# ══════════════════════════════════════════════════════════════════
# 每日复盘记录
# ══════════════════════════════════════════════════════════════════
def list_reviews(data_dir: Path, account_id: str, *, limit: int = 30) -> list[dict]:
    d = _daily_dir(data_dir, account_id)
    out: list[dict] = []
    try:
        for p in sorted(d.glob("*.json"), reverse=True)[: max(1, min(limit, 400))]:
            obj = _read_json(p)
            if isinstance(obj, dict):
                out.append(obj)
    except Exception:
        logger.debug("list_reviews failed", exc_info=True)
    return out


def get_review(data_dir: Path, account_id: str, day: str) -> dict | None:
    obj = _read_json(_review_path(data_dir, account_id, day))
    return obj if isinstance(obj, dict) else None


def _load_decisions_for_day(data_dir: Path, account_id: str, day: str) -> list[dict]:
    """读取当日 LLM 决策流水（llm_decisions.jsonl）。"""
    from app.strategy.paper_llm import _log_path  # 懒导入，避免循环

    rows: list[dict] = []
    p = _log_path(data_dir, account_id)
    try:
        if not p.exists():
            return rows
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if str(rec.get("ts") or "")[:10] == day:
                    rows.append(rec)
    except Exception:
        logger.debug("decision log read failed", exc_info=True)
    return rows


def _trading_day_ok(day: str | None = None) -> bool | None:
    """交易日判定（None = 未知，按放行处理）。"""
    try:
        from app.services import trading_day

        return trading_day.is_trading_day()
    except Exception:
        return None


def build_review_payload(data_dir: Path, account_id: str, day: str) -> dict[str, Any]:
    """汇总当日事实，作为复盘的输入（缺数据标 null，不臆造）。"""
    acc = paper.get_account(data_dir, account_id) or {}

    fills_today: list[dict] = []
    try:
        for f in paper.load_fills(data_dir, account_id):
            if f.get("date") != day:
                continue
            if f.get("kind") not in (None, "fill"):
                continue
            fills_today.append({
                "ts": f.get("ts"), "symbol": f.get("symbol"), "side": f.get("side"),
                "qty": f.get("qty"), "price": f.get("price"), "fee": f.get("fee"),
            })
    except Exception:
        logger.debug("fills_today failed", exc_info=True)

    orders_today: list[dict] = []
    try:
        for o in paper.load_orders(data_dir, account_id):
            if str(o.get("created_at") or "")[:10] != day:
                continue
            orders_today.append({
                "ts": o.get("created_at"), "symbol": o.get("symbol"), "side": o.get("side"),
                "qty": o.get("qty"), "order_type": o.get("order_type"),
                "status": o.get("status"), "source": o.get("source"),
                "reason": _clip(o.get("reason"), 120),
            })
    except Exception:
        logger.debug("orders_today failed", exc_info=True)

    rounds_today: list[dict] = []
    try:
        for r in paper.round_trips(data_dir, account_id):
            if r.get("close_date") != day:
                continue
            rounds_today.append({
                "symbol": r.get("symbol"), "open_date": r.get("open_date"),
                "qty": r.get("qty"), "pnl": r.get("pnl"), "pnl_pct": r.get("pnl_pct"),
                "holding_days": r.get("holding_days"),
            })
    except Exception:
        logger.debug("rounds_today failed", exc_info=True)

    positions: list[dict] = []
    try:
        for sym, pos in (paper.load_positions(data_dir, account_id) or {}).items():
            if int(pos.get("qty") or 0) <= 0:
                continue
            positions.append({"symbol": sym, "qty": pos.get("qty"),
                              "avg_cost": pos.get("avg_cost")})
    except Exception:
        logger.debug("positions failed", exc_info=True)

    nav_series: list[dict] = []
    try:
        for n in paper.load_nav(data_dir, account_id)[-20:]:
            nav_series.append({"date": n.get("date"), "nav": n.get("nav")})
    except Exception:
        logger.debug("nav failed", exc_info=True)
    nav_today = next((n["nav"] for n in reversed(nav_series) if n.get("date") == day), None)
    nav_prev = next((n["nav"] for n in reversed(nav_series) if str(n.get("date") or "") < day), None)
    day_pnl = (nav_today - nav_prev) if (nav_today is not None and nav_prev is not None) else None

    decisions = _load_decisions_for_day(data_dir, account_id, day)
    d_bucket = {"runs": len(decisions), "ordered": 0, "rejected": 0, "hold": 0,
                "reject_reasons": {}, "orders": []}
    for rec in decisions:
        res = str(rec.get("result") or "")
        if res in ("ordered", "rejected", "hold"):
            d_bucket[res] += 1
        if res == "rejected":
            key = _clip(rec.get("error") or "未知原因", 80)
            d_bucket["reject_reasons"][key] = d_bucket["reject_reasons"].get(key, 0) + 1
        if res == "ordered":
            d_bucket["orders"].append({
                "ts": rec.get("ts"), "action": rec.get("action"), "symbol": rec.get("symbol"),
                "qty": rec.get("qty"), "price": rec.get("price"),
                "reason": _clip(rec.get("reason"), 200),
                "trigger": rec.get("trigger") or "llm",
            })

    patterns: dict[str, int] = {}
    symbols: list[str] = []
    try:
        from app.strategy.paper_llm import resolve_symbols

        symbols = resolve_symbols(data_dir, account_id, acc)
    except Exception:
        symbols = []
    try:
        from app.strategy import intraday_history

        if symbols:
            pe = intraday_history.daily_pattern_events(data_dir, day, symbols)
            patterns = {k: len(v) for k, v in (pe or {}).items() if v}
    except Exception:
        logger.debug("patterns failed", exc_info=True)

    try:
        cumulative = paper.stats(data_dir, account_id)
    except Exception:
        cumulative = {}

    memory_so_far = {
        "focus": _as_list((load_profile(data_dir, account_id) or {}).get("focus"), limit=5),
        "disciplines": _as_list((load_profile(data_dir, account_id) or {}).get("disciplines"), limit=6),
        "recent_lessons": [r.get("text") for r in load_lessons(data_dir, account_id)[:12]],
    }

    return {
        "date": day,
        "account": {"id": account_id, "name": acc.get("name"), "auto_mode": acc.get("auto_mode"),
                    "cash": acc.get("cash"), "initial_cash": acc.get("initial_cash")},
        "nav": {"today": nav_today, "prev": nav_prev, "day_pnl": day_pnl, "series": nav_series},
        "cumulative": cumulative,
        "today": {
            "fills": fills_today,
            "orders": orders_today,
            "round_trips": rounds_today,
            "realized_pnl": round(sum(float(r.get("pnl") or 0) for r in rounds_today), 2),
            "positions_close": positions,
        },
        "decisions": d_bucket,
        "patterns": patterns,
        "memory_so_far": memory_so_far,
    }


# ── 复盘提示词 ─────────────────────────────────────────────────────
REVIEW_SYSTEM_PROMPT = """你是「海平分时擒牛」分时交易分析师的**盘后复盘**角色。

你的任务：基于当日真实成交、委托、平仓盈亏、形态触发与决策流水，做一次诚实、具体、
可执行的复盘，并把结论沉淀成**明天的纪律**。你不是在写总结报告，而是在给明天的自己
留一张"别再犯"的清单。

## 铁律
1. **只依据输入数据**。数据里没有的事实（未记录的盘口细节、传闻、消息面）一律不写；
   字段为 null 表示数据缺失，**不要脑补数值**。
2. **具体胜过正确**。禁止"要严格执行纪律""注意控制风险"这类空话；
   每条经验必须能对应到输入里的具体标的/时点/动作/数字。
3. **承认亏损与错误**。做错就写做错（追高、破位不卖、无量拉升没走、违反止损等）；
   赚钱但违规的操作也要标注（坏习惯不能因为赚钱被强化）。
4. 没有交易、没有触发时，如实说明"当日无操作"，并把重点放在为什么不该出手。

## 输出（严格 JSON，不要任何解释文字、不要 markdown 围栏）
{
  "summary": "一句话总结当日（含关键数字）",
  "what_worked": ["做对的、可复用的点（最多4条）"],
  "what_failed": ["做错/吃亏的点（最多4条）"],
  "mistakes": ["明确的操作错误（最多3条，没有就空数组）"],
  "lessons": [
    {"text": "可执行的经验教训（≤120字，必须具体到条件→动作）",
     "tags": ["买点|卖点|风控|择时|仓位|纪律"],
     "confidence": 0.8}
  ],
  "knowledge_updates": [
    {"title": "规则标题（≤30字）", "body": "规则内容（≤200字，条件→动作→验证方式）",
     "tags": ["..."]}
  ],
  "next_focus": ["明天最该盯的1-3件事（可核对的具体项）"],
  "rule_adjustments": [
    {"rule": "哪条规则/参数", "change": "怎么改", "reason": "依据今日哪条证据"}
  ]
}

约束：lessons 最多 4 条，knowledge_updates 最多 2 条（只在确实形成新规则时才写），
next_focus 最多 3 条。没有就返回空数组，**不要为了凑数编造**。"""


def _review_model(data_dir: Path, account_id: str) -> str:
    """收盘复盘用哪个模型（账户级 llm_review_model；空 = 交给 _reflect 走「分析」组/全局）。"""
    try:
        acc = paper.get_account(data_dir, account_id) or {}
    except Exception:  # noqa: BLE001 — 读配置失败不该阻断复盘
        return ""
    return str(acc.get("llm_review_model") or "").strip()


async def _reflect(payload: dict[str, Any], *,
                   model: str | None = None) -> tuple[dict[str, Any] | None, str]:
    """调 LLM 产出结构化复盘。返回 (review|None, raw_text)。

    ``model``: 复盘可单独指定模型（账户级 ``llm_review_model``；空 = 跟随
    「分析」组模型，再空则跟随全局 ai_model）。
    """
    from app.services import ai_provider
    from app.strategy.paper_llm import _extract_json  # 复用同一套容错解析

    if not ai_provider.ai_configured():
        return None, "AI 未配置（设置 → AI）"
    user = ("以下是该模拟盘账户当日的事实数据（JSON）。请按复盘要求输出严格 JSON：\n"
            + json.dumps(payload, ensure_ascii=False))
    messages = [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        text = await ai_provider.generate_ai_text(
            messages, temperature=0.2, max_tokens=None, timeout=240.0,
            model=model or ai_provider.role_model("analysis"),
        )
    except Exception as e:  # noqa: BLE001
        return None, f"复盘 LLM 调用失败: {e}"
    if not (text or "").strip():
        return None, "复盘 LLM 返回空响应（可能是推理预算/限流/内容过滤）"
    obj = _extract_json(text)
    if obj is None:
        return None, f"复盘未得到有效 JSON: {str(text)[:200]}"
    return obj, text


def _normalize_review(obj: dict[str, Any]) -> dict[str, Any]:
    lessons: list[dict] = []
    for item in (obj.get("lessons") or [])[:4]:
        if isinstance(item, str):
            t = item
            tags: list[str] = []
            conf = 0.6
        elif isinstance(item, dict):
            t = item.get("text") or item.get("lesson") or ""
            tags = [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()][:6]
            conf = _fnum(item.get("confidence"))
            conf = 0.6 if conf is None else max(0.0, min(1.0, conf))
        else:
            continue
        t = _clip(t, 300)
        if len(t) >= 4:
            lessons.append({"text": t, "tags": tags, "confidence": conf})

    kb: list[dict] = []
    for item in (obj.get("knowledge_updates") or [])[:2]:
        if not isinstance(item, dict):
            continue
        title = _clip(item.get("title"), 80)
        if not title:
            continue
        kb.append({
            "title": title,
            "body": _clip(item.get("body"), 2000),
            "tags": [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()][:8],
        })

    adjust: list[dict] = []
    for item in (obj.get("rule_adjustments") or [])[:5]:
        if not isinstance(item, dict):
            continue
        r = _clip(item.get("rule"), 120)
        c = _clip(item.get("change"), 200)
        if r or c:
            adjust.append({"rule": r, "change": c, "reason": _clip(item.get("reason"), 200)})

    return {
        "summary": _clip(obj.get("summary"), 500),
        "what_worked": _as_list(obj.get("what_worked"), limit=4),
        "what_failed": _as_list(obj.get("what_failed"), limit=4),
        "mistakes": _as_list(obj.get("mistakes"), limit=3),
        "lessons": lessons,
        "knowledge_updates": kb,
        "next_focus": _as_list(obj.get("next_focus"), limit=3),
        "rule_adjustments": adjust,
    }


def _state(data_dir: Path, account_id: str) -> dict:
    obj = _read_json(_state_path(data_dir, account_id))
    return obj if isinstance(obj, dict) else {}


def _patch_state(data_dir: Path, account_id: str, patch: dict) -> dict:
    st = _state(data_dir, account_id)
    st.update(patch)
    _write_json(_state_path(data_dir, account_id), st)
    return st


def run_daily_review(
    data_dir: Path,
    account_id: str,
    *,
    day: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """跑一次收盘复盘（幂等）。

    返回 {ok, day, skipped?, review?, error?}。**不中断交易路径**：任何异常都被
    收敛成 ok=False + error，并写进 state.last_error 供 UI 显示。
    """
    day = day or cn_today().isoformat()
    try:
        account_id = paper.validate_account_id(account_id)
    except ValueError as e:
        return {"ok": False, "day": day, "error": str(e)}

    ensure_ready(data_dir, account_id)

    existing = get_review(data_dir, account_id, day)
    if existing is not None and not force:
        return {"ok": True, "day": day, "skipped": "already_reviewed", "review": existing}

    # 结算当日（幂等）：保证平仓盈亏/定版净值可用。失败不阻断复盘。
    try:
        paper.settle_day(data_dir, day, account_id)
    except Exception:
        logger.debug("settle_day before review failed (non-fatal)", exc_info=True)

    payload = build_review_payload(data_dir, account_id, day)
    review_obj, raw = None, ""
    _t0 = time.perf_counter()
    try:
        import asyncio

        review_obj, raw = asyncio.run(_reflect(payload, model=_review_model(data_dir, account_id)))
    except RuntimeError:
        # 已在事件循环内（APScheduler 的 AsyncIOScheduler 走线程池，一般不会到这里）
        review_obj, raw = None, "复盘需在无事件循环的线程中运行"
    _elapsed_ms = (time.perf_counter() - _t0) * 1000

    # 对话留痕: 让用户看见复盘这次"发了什么、模型回了什么"（失败不影响复盘）
    try:
        from app.strategy import paper_conversation

        paper_conversation.record_turn(
            data_dir, account_id, channel="review",
            request=payload, response=raw or "", parsed=review_obj,
            meta={"date": day},
            elapsed_ms=_elapsed_ms, system_chars=len(REVIEW_SYSTEM_PROMPT),
            error=None if review_obj is not None else _clip(raw, 200),
        )
    except Exception:
        logger.debug("record review turn failed", exc_info=True)

    if review_obj is None:
        _patch_state(data_dir, account_id, {
            "last_attempt_day": day,
            "last_error": raw[:300],
            "last_attempt_at": cn_now().isoformat(timespec="seconds"),
        })
        return {"ok": False, "day": day, "error": raw[:300]}

    review = _normalize_review(review_obj)
    metrics = {
        "nav": payload["nav"]["today"],
        "day_pnl": payload["nav"]["day_pnl"],
        "realized_pnl": payload["today"]["realized_pnl"],
        "fills": len(payload["today"]["fills"]),
        "orders": len(payload["today"]["orders"]),
        "round_trips": len(payload["today"]["round_trips"]),
        "decisions": payload["decisions"]["runs"],
        "ordered": payload["decisions"]["ordered"],
        "hold": payload["decisions"]["hold"],
        "rejected": payload["decisions"]["rejected"],
        "patterns": payload["patterns"],
        "cumulative": payload["cumulative"],
    }
    record = {
        "date": day,
        "account": account_id,
        "created_at": cn_now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "today": payload["today"],
        "decisions": payload["decisions"],
        "patterns": payload["patterns"],
        "nav_series": payload["nav"]["series"],
        "memory_so_far": payload["memory_so_far"],
        "review": review,
    }
    _write_json(_review_path(data_dir, account_id, day), record)

    # 回写记忆与知识库
    saved_lessons: list[dict] = []
    try:
        saved_lessons = append_lessons(data_dir, account_id, review["lessons"], day=day)
    except Exception:
        logger.warning("append_lessons failed", exc_info=True)

    kb_added = 0
    for item in review["knowledge_updates"]:
        try:
            if add_knowledge(data_dir, account_id, title=item["title"], body=item["body"],
                             tags=item.get("tags"), source="review") is not None:
                kb_added += 1
        except Exception:
            logger.debug("knowledge upsert failed", exc_info=True)

    profile = refresh_profile(data_dir, account_id, review=review)
    _patch_state(data_dir, account_id, {
        "last_review_day": day,
        "last_review_at": record["created_at"],
        "last_error": None,
        "reviews": int(_state(data_dir, account_id).get("reviews") or 0) + 1,
    })
    return {
        "ok": True,
        "day": day,
        "review": record,
        "lessons_saved": len(saved_lessons),
        "knowledge_saved": kb_added,
        "profile": profile,
    }


def review_all_accounts(data_dir: Path, *, day: str | None = None) -> dict[str, Any]:
    """对当日有成交/委托的所有活跃账户跑复盘（调度器入口）。

    交易日闸门在调用方（daily_pipeline）已把，处在此处再兜一层：确定休市直接跳过。
    """
    day = day or cn_today().isoformat()
    if _trading_day_ok(day) is False:
        return {"ok": True, "day": day, "skipped": "holiday", "accounts": []}

    results: list[dict] = []
    try:
        accounts = paper.list_accounts(data_dir)
    except Exception:
        logger.warning("review_all_accounts: list_accounts failed", exc_info=True)
        return {"ok": False, "day": day, "error": "list_accounts failed", "accounts": []}

    for acc in accounts:
        aid = str(acc.get("id") or "")
        if not aid:
            continue
        if str(acc.get("status") or "active") != "active":
            continue
        try:
            payload = build_review_payload(data_dir, aid, day)
        except Exception:
            logger.debug("review precheck failed account=%s", aid, exc_info=True)
            continue
        # 当日无任何交易/委托 → 无可复盘（不烧 token）
        if not payload["today"]["fills"] and not payload["today"]["orders"]:
            results.append({"account": aid, "ok": True, "skipped": "no_activity"})
            continue
        try:
            res = run_daily_review(data_dir, aid, day=day)
            results.append({"account": aid, **{k: v for k, v in res.items() if k != "review"}})
        except Exception as e:  # noqa: BLE001
            logger.warning("daily review failed account=%s: %s", aid, e)
            results.append({"account": aid, "ok": False, "error": str(e)[:200]})
    return {"ok": True, "day": day, "accounts": results}


# ══════════════════════════════════════════════════════════════════
# 自我画像
# ══════════════════════════════════════════════════════════════════
def load_profile(data_dir: Path, account_id: str) -> dict:
    obj = _read_json(_profile_path(data_dir, account_id))
    return obj if isinstance(obj, dict) else {}


def refresh_profile(data_dir: Path, account_id: str, *, review: dict | None = None) -> dict:
    """重算自我画像：业绩指标 + 关注点 + 已内化纪律 + 形态统计。"""
    try:
        cumulative = paper.stats(data_dir, account_id)
    except Exception:
        cumulative = {}
    try:
        nav = paper.load_nav(data_dir, account_id)
    except Exception:
        nav = []

    reviews = list_reviews(data_dir, account_id, limit=30)
    focus: list[str] = []
    disciplines: list[str] = []
    pattern_totals: dict[str, int] = {}
    pnl_by_date: dict[str, float] = {}
    for r in reviews:
        day = str(r.get("date") or "")
        rv = r.get("review") or {}
        for item in (rv.get("next_focus") or []):
            s = _clip(item, 120)
            if s and s not in focus:
                focus.append(s)
        for item in (rv.get("lessons") or []):
            s = _clip((item or {}).get("text") if isinstance(item, dict) else item, 120)
            if s and s not in disciplines:
                disciplines.append(s)
        for k, v in (r.get("patterns") or {}).items():
            pattern_totals[k] = pattern_totals.get(k, 0) + int(v or 0)
        pnl = (r.get("metrics") or {}).get("day_pnl")
        if day and pnl is not None:
            pnl_by_date[day] = float(pnl)

    if review is not None:
        for item in (review.get("next_focus") or []):
            s = _clip(item, 120)
            if s and s not in focus:
                focus.insert(0, s)

    # 连胜/连亏（按复盘记录的日盈亏）
    streak_days, streak_pnl = 0, 0.0
    for day in sorted(pnl_by_date, reverse=True):
        v = pnl_by_date[day]
        if streak_days == 0:
            streak_days, streak_pnl = 1, v
            continue
        if (v > 0) == (streak_pnl > 0):
            streak_days += 1
            streak_pnl += v
        else:
            break

    lessons = load_lessons(data_dir, account_id)[:MAX_LESSONS]
    top_lessons = sorted(lessons, key=lambda r: -float(r.get("confidence") or 0))[:12]

    profile = {
        "account": account_id,
        "updated_at": cn_now().isoformat(timespec="seconds"),
        "days_reviewed": len(reviews),
        "last_review_day": str(reviews[0].get("date")) if reviews else None,
        "rounds": cumulative.get("rounds"),
        "win_rate": cumulative.get("win_rate"),
        "profit_loss_ratio": cumulative.get("profit_loss_ratio"),
        "avg_holding_days": cumulative.get("avg_holding_days"),
        "realized_pnl": cumulative.get("realized_pnl"),
        "max_drawdown": cumulative.get("max_drawdown"),
        "nav_latest": (nav[-1].get("nav") if nav else None),
        "focus": focus[:5],
        "disciplines": disciplines[:8],
        "pattern_totals": pattern_totals,
        "streak": {"days": streak_days, "pnl": round(streak_pnl, 2)},
        "top_lessons": [{"text": _clip(r.get("text"), 160),
                         "confidence": r.get("confidence"),
                         "tags": r.get("tags") or []} for r in top_lessons],
    }
    _write_json(_profile_path(data_dir, account_id), profile)
    return profile


# ══════════════════════════════════════════════════════════════════
# 提示词注入（trade 决策用）
# ══════════════════════════════════════════════════════════════════
def build_memory_context(
    data_dir: Path,
    account_id: str,
    *,
    symbols: list[str] | None = None,
    max_reviews: int = CTX_MAX_REVIEWS,
    max_lessons: int = CTX_MAX_LESSONS,
    max_knowledge: int = CTX_MAX_KNOWLEDGE,
) -> dict[str, Any]:
    """构造注入交易决策提示词的记忆块。

    内容刻意保持"少而准"：画像摘要 + 最近几天的复盘结论 + 高置信度经验 +
    与当前标的/主题相关的知识库条目。模型看到的每一行都应该能改变它的行为。
    """
    out: dict[str, Any] = {
        "enabled": True,
        "profile": {},
        "recent_reviews": [],
        "lessons": [],
        "knowledge": [],
        "note": "",
    }
    try:
        # 知识库首次使用自动播种（幂等）
        ensure_ready(data_dir, account_id)

        profile = load_profile(data_dir, account_id) or {}
        out["profile"] = {
            "days_reviewed": profile.get("days_reviewed"),
            "win_rate": profile.get("win_rate"),
            "profit_loss_ratio": profile.get("profit_loss_ratio"),
            "realized_pnl": profile.get("realized_pnl"),
            "max_drawdown": profile.get("max_drawdown"),
            "streak": profile.get("streak"),
            "focus": _as_list(profile.get("focus"), limit=5),
            "disciplines": _as_list(profile.get("disciplines"), limit=6),
        }

        for r in list_reviews(data_dir, account_id, limit=max_reviews):
            rv = r.get("review") or {}
            out["recent_reviews"].append({
                "date": r.get("date"),
                "summary": _clip(rv.get("summary"), 200),
                "what_failed": _as_list(rv.get("what_failed"), limit=3),
                "mistakes": _as_list(rv.get("mistakes"), limit=3),
                "next_focus": _as_list(rv.get("next_focus"), limit=3),
            })

        lessons = sorted(
            load_lessons(data_dir, account_id),
            key=lambda r: (-float(r.get("confidence") or 0), str(r.get("date") or "")),
        )[:max_lessons]
        out["lessons"] = [{
            "date": r.get("date"),
            "text": _clip(r.get("text"), 200),
            "tags": r.get("tags") or [],
            "confidence": r.get("confidence"),
        } for r in lessons]

        query = " ".join(symbols or [])
        kb = search_knowledge(load_knowledge(data_dir, account_id), query,
                              limit=max_knowledge) if query else []
        if len(kb) < max_knowledge:
            # 检索不足时补位: 先补**复盘沉淀的新规则**（最新在前，这是"进步"的载体），
            # 再补方法论基线（保证模型始终带着体系认知）。
            have = {r.get("id") for r in kb}
            all_kb = load_knowledge(data_dir, account_id)
            for source in ("review", "methodology"):
                for r in all_kb:
                    if len(kb) >= max_knowledge:
                        break
                    if r.get("id") in have or r.get("source") != source:
                        continue
                    kb.append(r)
                    have.add(r.get("id"))
                if len(kb) >= max_knowledge:
                    break
        out["knowledge"] = [{"title": r.get("title"), "body": _clip(r.get("body"), 400)}
                            for r in kb[:max_knowledge]]

        out["note"] = (
            "这是你的历史记忆与知识库。下单前先核对 lessons：若本次动作与某条经验冲突，"
            "必须在 reason 里说明这次为何例外，否则不做。"
        )
    except Exception:
        logger.warning("build_memory_context failed account=%s", account_id, exc_info=True)
        out["enabled"] = False
        out["note"] = "记忆读取失败（不影响交易，按原方法论判断）"
    return out


def status(data_dir: Path, account_id: str) -> dict[str, Any]:
    """记忆/知识库的状态摘要（UI 用）。"""
    try:
        account_id = paper.validate_account_id(account_id)
    except ValueError:
        return {"available": False, "error": "invalid account"}
    try:
        ensure_ready(data_dir, account_id)
        reviews = list_reviews(data_dir, account_id, limit=400)
        lessons = load_lessons(data_dir, account_id)
        kb = load_knowledge(data_dir, account_id)
        st = _state(data_dir, account_id)
        return {
            "available": True,
            "account": account_id,
            "profile": load_profile(data_dir, account_id),
            "counts": {"reviews": len(reviews), "lessons": len(lessons), "knowledge": len(kb)},
            "last_review_day": st.get("last_review_day"),
            "last_review_at": st.get("last_review_at"),
            "last_error": st.get("last_error"),
            "seed_ready": any("methodology" in (r.get("tags") or []) for r in kb),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("memory status failed account=%s: %s", account_id, e)
        return {"available": False, "error": str(e)[:200]}
