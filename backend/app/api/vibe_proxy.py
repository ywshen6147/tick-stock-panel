"""vibe-astock 后端代理 — 把 TSP 前端的 /api/vibe/* 转发到 vibe-astock (:8910)。

为什么需要代理:
  - vibe-astock 的 server.py 没有 CORS 中间件（其测试明确禁止搬 VR 的 CORS），
    浏览器里 TSP 前端(:3011) 直接跨端口 fetch :8910 会被 CORS 拦截。
  - 走 TSP 后端(:3018) 做同源代理后，前端用 /api/vibe/... 即可，无 CORS 问题，
    且 Origin 始终是 127.0.0.1（vibe 的 _origin_ok 白名单内），写操作也能过。

覆盖的 vibe 端点（B1 阶段 1 涉及）:
  - 复盘:      /api/review/*  /api/verification/*  /api/market/*
  - 开盘核验:  /api/intraday/*
  - 个股深挖:  /api/deepdive/*
  - 盯盘:      /api/monitor/*  /api/positions  /api/watchlist?   (vr 子应用)
  - 通用:      GET/POST 透传

实现: httpx.AsyncClient 透传 method/query/body，流式响应（SSE）用 async 转发。
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vibe", tags=["vibe"])

# vibe-astock 后端地址（独立进程，由 TSP-整合启动.bat 拉起）
VIBE_BASE_URL = "http://127.0.0.1:8910"

# 透传时去掉的请求头（Host 指向本机、Content-Length 由 httpx 重算）
_SKIP_HEADERS = {"host", "content-length", "connection"}

# 流式端点（SSE）：这些响应体不适合缓冲，直接流式转发
_STREAM_PREFIXES = ("/api/review/stream",)


def _vibe_path(path: str) -> str:
    """/api/vibe/xxx -> /api/xxx（去掉 vibe 前缀）"""
    if path.startswith("/api/vibe/"):
        return path[len("/api/vibe"):]
    return path


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def vibe_proxy(request: Request, path: str):
    """透传 /api/vibe/* 到 vibe-astock。"""
    vibe_path = _vibe_path(f"/api/vibe/{path}")
    url = f"{VIBE_BASE_URL}{vibe_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _SKIP_HEADERS
    }
    body = await request.body()

    timeout = httpx.Timeout(180.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            req = client.build_request(
                request.method, url, headers=headers, content=body,
            )
            if vibe_path.startswith(_STREAM_PREFIXES):
                resp = await client.send(req, stream=True)
                return Response(
                    content=resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers={
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("content-length", "connection", "transfer-encoding")
                    },
                )
            resp = await client.send(req)
            content = resp.content
            return Response(
                content=content,
                status_code=resp.status_code,
                headers={
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in ("content-length", "connection")
                },
            )
    except httpx.ConnectError as e:
        logger.warning("vibe-astock 代理失败(服务未启动?): %s -> %s", url, e)
        return Response(
            content='{"error": "vibe-astock 服务未启动(端口 8910)。请运行 Vibe-Astock-静默启动.bat"}',
            status_code=502,
            media_type="application/json",
        )
    except httpx.HTTPError as e:
        logger.warning("vibe-astock 代理异常: %s -> %s", url, e)
        return Response(
            content=f'{{"error": "vibe 代理异常: {e}"}}',
            status_code=502,
            media_type="application/json",
        )
