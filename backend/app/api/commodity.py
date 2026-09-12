"""商品期货 + 汇率行情 API — 代理新浪国际期货/汇率接口(hq.sinajs.cn)。

前端跨域调用新浪会被浏览器 CORS 拦截，走 TSP 后端同源代理解决。
覆盖:
  - 商品: 黄金/白银/铜/原油/天然气/大豆/玉米/小麦 (纽约商品期货, hf_ 前缀)
  - 汇率: 美元指数(DINIW) / 美元人民币(fx_susdcny) / 美元日元(fx_susdjpy)

新浪国际期货字段(15 列):
  [0]当前价 [1]空 [2]买价 [3]卖价 [4]今开 [5]最高 [6]时间 [7]昨收 [8]昨结算
  [9..11]内部 [12]日期 [13]名称
涨跌幅 = (当前价 - 昨收) / 昨收。

新浪汇率字段(fx_ / DINIW 前缀):
  [0]时间 [1]现价 [2]? [3]? [4]量 [5]最低 [6]最高 [7]昨收
涨跌幅 = (现价 - 昨收) / 昨收。
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/commodity", tags=["commodity"])

# 新浪国际期货代码 → 显示名
_SINA_CODES = {
    "hf_GC": "黄金",
    "hf_SI": "白银",
    "hf_HG": "铜",
    "hf_CL": "原油",
    "hf_NG": "天然气",
    "hf_S": "大豆",
    "hf_C": "玉米",
    "hf_W": "小麦",
}

_SINA_URL = "https://hq.sinajs.cn/list=" + ",".join(_SINA_CODES.keys())

# 汇率代码 → 显示名（fx_/DINIW 格式，字段结构同 fx，用昨收[7]算涨跌幅）
_FX_CODES = {
    "DINIW": "美元指数",
    "fx_susdcny": "美元/人民币",
    "fx_susdjpy": "美元/日元",
}

_FX_URL = "https://hq.sinajs.cn/list=" + ",".join(_FX_CODES.keys())

_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


async def _fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        return resp.content.decode("gbk", "replace")


@router.get("/prices")
async def commodity_prices():
    """返回商品实时行情列表。"""
    try:
        text = await _fetch(_SINA_URL)
    except Exception as e:  # noqa: BLE001
        logger.warning("商品行情拉取失败: %s", e)
        return Response(
            content='{"error": "商品行情拉取失败"}', status_code=502, media_type="application/json"
        )

    rows: list[dict] = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        var, payload = line.split("=", 1)
        code = var.replace("var hq_str_", "").strip()
        if code not in _SINA_CODES:
            continue
        fields = payload.strip('";\n').split(",")
        if len(fields) < 14:
            continue
        name = fields[13] or _SINA_CODES[code]
        price = _f(fields[0])
        prev_close = _f(fields[7])
        change_pct = None
        if price is not None and prev_close not in (None, 0):
            change_pct = round((price - prev_close) / prev_close * 100, 2)
        rows.append({
            "code": code,
            "name": name,
            "label": _SINA_CODES[code],
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "date": fields[12] if len(fields) > 12 else None,
            "time": fields[6] if len(fields) > 6 else None,
        })

    return {"rows": rows, "count": len(rows)}


@router.get("/fx")
async def fx_prices():
    """返回汇率行情列表（美元指数/美元人民币/美元日元）。"""
    try:
        text = await _fetch(_FX_URL)
    except Exception as e:  # noqa: BLE001
        logger.warning("汇率行情拉取失败: %s", e)
        return Response(
            content='{"error": "汇率行情拉取失败"}', status_code=502, media_type="application/json"
        )

    rows: list[dict] = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        var, payload = line.split("=", 1)
        code = var.replace("var hq_str_", "").strip()
        if code not in _FX_CODES:
            continue
        fields = payload.strip('";\n').split(",")
        if len(fields) < 8:
            continue
        price = _f(fields[1])      # 现价
        prev_close = _f(fields[7])  # 昨收
        change_pct = None
        if price is not None and prev_close not in (None, 0):
            change_pct = round((price - prev_close) / prev_close * 100, 3)
        rows.append({
            "code": code,
            "name": _FX_CODES[code],
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "time": fields[0] if len(fields) > 0 else None,
        })

    return {"rows": rows, "count": len(rows)}


def _f(v: str) -> float | None:
    v = (v or "").strip()
    if not v or v == "-":
        return None
    try:
        n = float(v)
        return n
    except (TypeError, ValueError):
        return None
