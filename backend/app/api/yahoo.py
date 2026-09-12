"""雅虎财经 K 线代理 — 为盘面页的美股/商品/汇率提供 120 天日 K。

为什么要代理: 前端直接请求 query1.finance.yahoo.com 会被 CORS 拦截，
且雅虎在国内时通时不通，代理可加超时/重试/降级。

覆盖 symbol:
  - 美股指数: ^DJI 道琼斯 / ^GSPC 标普 / ^IXIC 纳斯达克 / ^HSI 恒生
  - 美股七姐妹: AAPL MSFT NVDA GOOGL AMZN META TSLA
  - 商品期货: GC=F 黄金 / SI=F 白银 / HG=F 铜 / CL=F 原油 / NG=F 天然气 / ZS=F 大豆 / ZC=F 玉米 / ZW=F 小麦
  - 汇率: DX-Y.NYB 美元指数 / CNY=X 美元人民币 / JPY=X 美元日元

返回格式: {symbol, name, currency, rows: [{date, open, high, low, close, volume}]}
雅虎 chart 接口: https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=120d&interval=1d
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Query, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/yahoo", tags=["yahoo"])

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# 常见 symbol → 显示名（未列出的直接用雅虎返回的 shortName）
_KNOWN = {
    "^DJI": "道琼斯", "^GSPC": "标普500", "^IXIC": "纳斯达克", "^HSI": "恒生指数",
    "GC=F": "黄金", "SI=F": "白银", "HG=F": "铜", "CL=F": "原油", "NG=F": "天然气",
    "ZS=F": "大豆", "ZC=F": "玉米", "ZW=F": "小麦",
    "DX-Y.NYB": "美元指数", "CNY=X": "美元/人民币", "JPY=X": "美元/日元",
}


@router.get("/kline")
async def yahoo_kline(
    symbol: str = Query(..., description="雅虎 symbol, 如 ^DJI / GC=F / CNY=X"),
    days: int = Query(120, ge=30, le=500, description="K 线根数(约)"),
):
    """拉取雅虎日 K，转成 OHLC 格式。"""
    # 构造雅虎 range（days → 1y 足够 500 根；120 → 6mo）
    yrange = "6mo"
    if days >= 250:
        yrange = "1y"
    elif days >= 120:
        yrange = "6mo"
    else:
        yrange = "3mo"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={yrange}&interval=1d"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=_HEADERS)
            resp.raise_for_status()
            j = resp.json()
    except httpx.HTTPError as e:
        logger.warning("雅虎 K 线失败 %s: %s", symbol, e)
        return Response(
            content=f'{{"error": "雅虎 K 线拉取失败: {e}"}}',
            status_code=502, media_type="application/json",
        )

    result = (j.get("chart") or {}).get("result")
    if not result:
        return Response(
            content='{"error": "雅虎无此标的 K 线数据"}',
            status_code=404, media_type="application/json",
        )
    meta = result[0].get("meta") or {}
    ts = result[0].get("timestamp") or []
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]

    rows: list[dict] = []
    for i, t in enumerate(ts):
        try:
            d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
            o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
            v = quote["volume"][i]
            if any(x is None for x in (o, h, l, c)):
                continue
            rows.append({
                "date": d,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(l, 4),
                "close": round(c, 4),
                "volume": v or 0,
            })
        except (IndexError, TypeError):
            continue

    name = _KNOWN.get(symbol) or meta.get("shortName") or meta.get("symbol") or symbol
    return {
        "symbol": symbol,
        "name": name,
        "currency": meta.get("currency"),
        "rows": rows[-days:],
    }
