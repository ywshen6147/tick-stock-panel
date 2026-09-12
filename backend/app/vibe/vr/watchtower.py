"""每日盯盘数据层 —— 盘中 3 秒轮询的实时监控（短线投资实例专属，不回推开源仓库）。

五个模块的标的池：持仓股（后端可读）/ 自选股（前端传入）/ 总市值≥500亿大票（东财市值榜，
每日一次）/ 三连板以上（东财涨停池，60 秒刷新）/ 昨日成交额前十（收盘快照，次日复用）。

架构：常驻轮询线程只在交易时段活跃 —— 每 3 秒用腾讯批量行情（qt.gtimg.cn，L1 快照 3 秒
一帧、不封 IP、60 只/请求）拉全池，内存帧差分做异动检测（急拉急跌/触板开板，冷却去重），
异动事件当日落盘复盘。前端每 3 秒读内存快照（零上游请求，毫秒级响应）。
（v2 候选：mootdx TCP 主通道、量能脉冲规则、macOS 弹窗通知。）
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import astock

BEIJING = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

_DATA_DIR = Path(os.environ.get("VR_DATA_DIR") or Path.home() / ".vibe-research") / "monitor"

POLL_SECONDS = 3          # L1 快照 3 秒一帧（沪深两所官方口径），更快无意义
SURGE_WINDOW = 180        # 急拉急跌观察窗（秒）
SURGE_PCT = 1.5           # 3 分钟涨跌超此值报异动（500亿大票口径）
ALERT_COOLDOWN = 300      # 同票同类型异动冷却（秒）
FRAME_KEEP = 120          # 每只票保留帧数（120 帧 ≈ 6 分钟）

_lock = threading.Lock()
_wake = threading.Event()   # 录入持仓/改自选后唤醒线程立即重建快照
_thread: threading.Thread | None = None
_extra_watch: list[str] = []      # 前端传入的自选股
_frames: dict[str, deque] = {}    # code -> deque[(ts, price, amount)]
_limit_state: dict[str, bool] = {}  # code -> 上一帧是否封涨停
_alerts: list[dict] = []          # 当日异动事件（新的在前）
_alert_last: dict[tuple, float] = {}
_snapshot: dict = {}              # 前端直接读的最新快照
_bigcaps_cache: tuple[str, list] | None = None   # (date, [{code,name}])
_lianban_cache: tuple[float, list] = (0.0, [])
_turnover_saved_date = ""


# ---------------------------------------------------------------- 腾讯批量行情
def _prefix(code: str) -> str:
    if code[0] in ("6", "5", "9"):
        return "sh"
    if code[0] in ("4", "8") or code.startswith("92"):
        return "bj"
    return "sz"


def _tencent_batch(codes: list[str]) -> dict[str, dict]:
    """腾讯批量实时行情。返回 {code: {...}}；失败的批次静默跳过（下一轮自然补上）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 60):
        batch = codes[i : i + 60]
        q = ",".join(_prefix(c) + c for c in batch)
        try:
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={q}", headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=5).read().decode("gbk", "ignore")
        except Exception:  # noqa: BLE001
            continue
        for line in raw.split(";"):
            line = line.strip()
            m = re.match(r'v_(?:sh|sz|bj)(\d{6})="(.*)"', line)
            if not m:
                continue
            code, f = m.group(1), m.group(2).split("~")
            if len(f) < 49 or not f[3]:
                continue
            try:
                out[code] = {
                    "name": f[1],
                    "price": float(f[3]),
                    "prev_close": float(f[4] or 0),
                    "pct": float(f[32] or 0),
                    "amount": float(f[37] or 0) * 1e4,  # 万元 → 元（当日累计成交额）
                    "zt_price": float(f[47] or 0),
                    "dt_price": float(f[48] or 0),
                }
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------- 标的池
def _holdings() -> list[dict]:
    try:
        import portfolio as pf

        return pf._load().get("holdings", [])
    except Exception:  # noqa: BLE001
        return []


def _bigcaps() -> list[dict]:
    """总市值 ≥500 亿名单（东财市值榜降序翻页，每日一次；东财挂了用昨日落盘名单）。"""
    global _bigcaps_cache
    today = datetime.now(BEIJING).strftime("%Y%m%d")
    if _bigcaps_cache and _bigcaps_cache[0] == today:
        return _bigcaps_cache[1]
    out: list[dict] = []
    for page in range(1, 10):
        url = ("https://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f20&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               "&fields=f12,f14,f20" % page)
        try:
            r = astock.em_get(url, headers={"User-Agent": UA}, timeout=10)
            diff = (r.json().get("data") or {}).get("diff") or []
        except Exception:  # noqa: BLE001
            break
        if not diff:
            break
        stop = False
        for p in diff:
            cap = p.get("f20")
            if not isinstance(cap, (int, float)):
                continue
            if cap < 5e10:
                stop = True
                break
            out.append({"code": str(p.get("f12", "")), "name": p.get("f14", "")})
        if stop:
            break
    file = _DATA_DIR / "bigcaps.json"
    if out:
        _bigcaps_cache = (today, out)
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({"date": today, "stocks": out}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        return out
    try:  # 东财不可用：回退最近一次落盘名单
        saved = json.loads(file.read_text())
        _bigcaps_cache = (today, saved["stocks"])
        return saved["stocks"]
    except Exception:  # noqa: BLE001
        return []


def _lianban3() -> list[dict]:
    """三连板以上名单（东财涨停池，盘中 60 秒刷新一次，附连板数）。"""
    global _lianban_cache
    if time.time() - _lianban_cache[0] < 60:
        return _lianban_cache[1]
    today = datetime.now(BEIJING).date()
    stocks: list[dict] = []
    for back in range(8):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        pool = astock.em_zt_topic_pool("getTopicZTPool", d, "fbt:asc")
        if pool:
            stocks = [
                {"code": str(p.get("c", "")), "name": p.get("n", ""), "boards": int(p.get("lbc") or 1)}
                for p in pool
                if int(p.get("lbc") or 1) >= 3
            ]
            break
    _lianban_cache = (time.time(), sorted(stocks, key=lambda x: -x["boards"]))
    return _lianban_cache[1]


def _turnover_file() -> Path:
    return _DATA_DIR / "turnover_top.json"


def _turnover_top10_live() -> list[dict]:
    url = ("https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=10&po=1&np=1"
           "&fltt=2&invt=2&fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
           "&fields=f12,f14,f6")
    try:
        r = astock.em_get(url, headers={"User-Agent": UA}, timeout=10)
        diff = (r.json().get("data") or {}).get("diff") or []
        return [{"code": str(p.get("f12", "")), "name": p.get("f14", "")} for p in diff]
    except Exception:  # noqa: BLE001
        return []


def _turnover_yesterday() -> tuple[str, list[dict]]:
    """昨日成交额前十。有收盘快照用快照；首日没有则用实时榜暂代（标注清楚）。"""
    today = datetime.now(BEIJING).strftime("%Y%m%d")
    try:
        saved = json.loads(_turnover_file().read_text())
        if saved.get("date") and saved["date"] != today and saved.get("stocks"):
            return "昨日成交前十", saved["stocks"]
    except Exception:  # noqa: BLE001
        pass
    live = _turnover_top10_live()
    return "成交前十（今日实时·首日暂代昨日）", live


def _save_turnover_snapshot() -> None:
    """收盘后存当日成交前十快照，供次日作「昨日前十」。每天存一次。"""
    global _turnover_saved_date
    today = datetime.now(BEIJING).strftime("%Y%m%d")
    if _turnover_saved_date == today:
        return
    live = _turnover_top10_live()
    if live:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _turnover_file().write_text(json.dumps({"date": today, "stocks": live}, ensure_ascii=False))
            _turnover_saved_date = today
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 交易时段
def _market_phase() -> str:
    """'open' 交易中 / 'break' 午休 / 'closed' 收盘或非交易日。（节假日不特判：池自然无变化）"""
    now = datetime.now(BEIJING)
    if now.weekday() >= 5:
        return "closed"
    hm = now.hour * 100 + now.minute
    if 915 <= hm <= 1132:
        return "open"
    if 1132 < hm < 1258:
        return "break"
    if 1258 <= hm <= 1505:
        return "open"
    return "closed"


# ---------------------------------------------------------------- 异动检测
def _emit(code: str, name: str, kind: str, msg: str, sources: list[str]) -> None:
    key = (code, kind)
    now = time.time()
    if now - _alert_last.get(key, 0) < ALERT_COOLDOWN:
        return
    _alert_last[key] = now
    evt = {
        "ts": datetime.now(BEIJING).strftime("%H:%M:%S"),
        "code": code,
        "name": name,
        "kind": kind,
        "msg": msg,
        "sources": sources,
    }
    _alerts.insert(0, evt)
    del _alerts[500:]
    try:  # 当日事件落盘（收盘复盘）
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(BEIJING).strftime("%Y%m%d")
        with open(_DATA_DIR / f"alerts-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _detect(code: str, q: dict, sources: list[str]) -> None:
    now = time.time()
    frames = _frames.setdefault(code, deque(maxlen=FRAME_KEEP))
    frames.append((now, q["price"], q["amount"]))

    # 规则① 急拉/急跳水：SURGE_WINDOW 秒窗口首尾涨跌幅
    old = next((f for f in frames if now - f[0] <= SURGE_WINDOW), None)
    if old and old[1] > 0 and now - old[0] >= 60:  # 至少积累 1 分钟才判
        delta = (q["price"] / old[1] - 1) * 100
        if delta >= SURGE_PCT:
            _emit(code, q["name"], "急拉", f"3分钟 +{delta:.1f}%（现价 {q['price']}，今日 {q['pct']:+.1f}%）", sources)
        elif delta <= -SURGE_PCT:
            _emit(code, q["name"], "急跌", f"3分钟 {delta:.1f}%（现价 {q['price']}，今日 {q['pct']:+.1f}%）", sources)

    # 规则② 触板/开板（涨停价来自行情快照，含北交所 30cm）
    if q["zt_price"] > 0:
        is_limit = q["price"] >= q["zt_price"] - 1e-6
        was_limit = _limit_state.get(code)
        if was_limit is not None:
            if is_limit and not was_limit:
                _emit(code, q["name"], "封板", f"触及涨停 {q['zt_price']}", sources)
            elif was_limit and not is_limit:
                _emit(code, q["name"], "开板", f"涨停打开（现价 {q['price']}，今日 {q['pct']:+.1f}%）", sources)
        _limit_state[code] = is_limit


# ---------------------------------------------------------------- 主循环
def _build_snapshot(quotes: dict, holdings: list[dict], watch: list[str],
                    bigcaps: list[dict], lianban: list[dict],
                    turnover_label: str, turnover: list[dict], phase: str) -> dict:
    def row(code: str) -> dict:
        q = quotes.get(code) or {}
        return {
            "code": code,
            "name": q.get("name", ""),
            "price": q.get("price"),
            "pct": q.get("pct"),
            "amount": q.get("amount"),
        }

    hold_rows = []
    for h in holdings:
        r = row(h["code"])
        r["cost"] = h.get("cost")
        r["shares"] = h.get("shares")
        if r["price"] and h.get("cost"):
            r["pnl_pct"] = round((r["price"] / h["cost"] - 1) * 100, 2)
        hold_rows.append(r)

    lianban_rows = []
    for s in lianban:
        r = row(s["code"])
        r["boards"] = s["boards"]
        q = quotes.get(s["code"]) or {}
        if q.get("zt_price") and q.get("price") is not None:
            r["is_limit"] = q["price"] >= q["zt_price"] - 1e-6
        lianban_rows.append(r)

    bigcap_rows = sorted(
        (row(b["code"]) for b in bigcaps),
        key=lambda r: -(r["pct"] if isinstance(r["pct"], (int, float)) else -999),
    )[:10]

    return {
        "ts": datetime.now(BEIJING).strftime("%H:%M:%S"),
        "phase": phase,
        "poll_seconds": POLL_SECONDS,
        "holdings": hold_rows,
        "watchlist": [row(c) for c in watch],
        "bigcap": {"total": len(bigcaps), "top": bigcap_rows},
        "lianban3": lianban_rows,
        "turnover": {"label": turnover_label, "stocks": [row(t["code"]) for t in turnover]},
        "alerts": _alerts[:120],
    }


def _loop() -> None:
    global _snapshot
    while True:
        phase = _market_phase()
        if phase == "closed":
            _save_turnover_snapshot()

        # 非交易时段也低频全量重建（收盘后录入持仓/加自选立刻可见），只是不做异动检测
        try:
            holdings = _holdings()
            with _lock:
                watch = list(_extra_watch)
            bigcaps = _bigcaps()
            lianban = _lianban3()
            turnover_label, turnover = _turnover_yesterday()

            pool: dict[str, list[str]] = {}
            for c in (h["code"] for h in holdings):
                pool.setdefault(c, []).append("持仓")
            for c in watch:
                pool.setdefault(c, []).append("自选")
            for b in bigcaps:
                pool.setdefault(b["code"], []).append("大票")
            for s in lianban:
                pool.setdefault(s["code"], []).append("连板")
            for t in turnover:
                pool.setdefault(t["code"], []).append("昨十")

            quotes = _tencent_batch(list(pool.keys()))
            if phase == "open":
                for code, q in quotes.items():
                    _detect(code, q, pool[code])
            _snapshot = _build_snapshot(quotes, holdings, watch, bigcaps, lianban,
                                        turnover_label, turnover, phase)
        except Exception:  # noqa: BLE001  数据源抖动不杀线程，下一轮重试
            pass
        _wake.wait(POLL_SECONDS if phase == "open" else 20)
        _wake.clear()


def ensure_started() -> None:
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="watchtower", daemon=True)
        _thread.start()


def poke() -> None:
    """外部数据变化（录入持仓/改自选）后唤醒线程，立即重建快照。"""
    _wake.set()


def set_watch(codes: list[str]) -> None:
    global _extra_watch
    clean = [c for c in codes if re.match(r"^\d{6}$", c)][:100]
    with _lock:
        changed = clean != _extra_watch
        _extra_watch = clean
    if changed:
        poke()


def get_snapshot() -> dict:
    return _snapshot or {"ts": "", "phase": _market_phase(), "poll_seconds": POLL_SECONDS,
                         "holdings": [], "watchlist": [], "bigcap": {"total": 0, "top": []},
                         "lianban3": [], "turnover": {"label": "", "stocks": []}, "alerts": []}
