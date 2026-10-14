"""Python ↔ Node 桥接: 通过 subprocess 调用 bridge.mjs 使用真实 stock-sdk 抓数据。

Original implementation by @forrany (PR #57), migrated to plugin architecture.

后端是 Python, stock-sdk 是 Node/JS 包, 这里用 subprocess 把二者接起来:
每次调用 spawn 一个 `node bridge.mjs`, 从 stdin 喂 JSON job, 从 stdout 读 JSON 结果。
批内并发由 bridge.mjs 内部承担, 一次进程调用摊薄 node 启动开销。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_BRIDGE_MJS = _HERE / "bridge.mjs"

# 默认超时(秒)。全市场(realtime/instruments)与大批量 daily 可能较久, provider 侧按 op 调大。
DEFAULT_TIMEOUT = 120


class StockSDKBridgeError(RuntimeError):
    """桥接调用失败(node 缺失 / stock-sdk 未安装 / 子进程异常 / 结果非法)。"""


def _log_bridge_stderr(op: str | None, stderr: str | None) -> None:
    """把桥接子进程的 stderr 转成日志(capture_output 会把它吞掉)。

    成功返回时 stderr 里通常承载**降级/回退告警** —— 例如「新浪全市场失败 →
    回退 sdk.batch.cn」、「分钟K 已用新浪兜底」。这些是排障的关键线索: 数据源
    一旦被静默替换, 口径/时效可能与预期不同(2026-10-13 的量纲事故即由回退路径
    引入且当时无任何日志可见)。故此处逐行 warn, 不再丢弃。
    """
    text = (stderr or "").strip()
    if not text:
        return
    for line in text.splitlines():
        line = line.strip()
        if line:
            logger.warning("stock-sdk 桥接告警 (op=%s): %s", op, line)


def _node_bin() -> str | None:
    """定位 node 可执行文件: 优先环境变量 STOCK_SDK_NODE, 否则 PATH 中的 node。"""
    env = os.getenv("STOCK_SDK_NODE")
    if env:
        return env if (Path(env).exists() or shutil.which(env)) else None
    return shutil.which("node")


def run_job(job: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """执行一次桥接 job, 返回解析后的 dict(含 `rows`)。失败抛 StockSDKBridgeError。"""
    node = _node_bin()
    if not node:
        raise StockSDKBridgeError(
            "未找到 node 可执行文件。请安装 Node.js(>=18)或设置 STOCK_SDK_NODE 指向 node。"
        )
    if not _BRIDGE_MJS.exists():
        raise StockSDKBridgeError(f"桥接脚本缺失: {_BRIDGE_MJS}")

    payload = json.dumps(job, ensure_ascii=False)
    op = job.get("op")
    _t0 = time.perf_counter()
    logger.info("stock-sdk 桥接开始 (op=%s, timeout=%ss)", op, timeout)
    try:
        proc = subprocess.run(
            [node, str(_BRIDGE_MJS)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(_HERE),
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("stock-sdk 桥接超时 (op=%s, %ss)", op, timeout)
        raise StockSDKBridgeError(f"stock-sdk 桥接超时(op={op}, {timeout}s)") from e
    except OSError as e:
        logger.warning("stock-sdk 启动 node 失败 (op=%s): %s", op, e)
        raise StockSDKBridgeError(f"启动 node 失败: {e}") from e
    logger.info("stock-sdk 桥接完成 (op=%s, %.2fs)", op, time.perf_counter() - _t0)

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise StockSDKBridgeError(f"stock-sdk 桥接非零退出({proc.returncode}): {tail}")

    # 成功路径也透传 stderr 中的降级/回退告警(见 _log_bridge_stderr)。
    _log_bridge_stderr(op, proc.stderr)

    out = (proc.stdout or "").strip()
    if not out:
        raise StockSDKBridgeError(f"stock-sdk 桥接无输出。stderr: {(proc.stderr or '').strip()[-500:]}")
    try:
        result = json.loads(out)
    except json.JSONDecodeError as e:
        raise StockSDKBridgeError(f"stock-sdk 桥接输出非法 JSON: {out[:500]}") from e

    if not result.get("ok"):
        raise StockSDKBridgeError(f"stock-sdk 桥接返回错误: {result.get('error')}")
    return result


def availability() -> tuple[bool, str]:
    """探活: 返回 (是否可用, 原因)。用于 UI 与日志。不抛异常。"""
    node = _node_bin()
    if not node:
        return False, "未找到 node(需 Node.js>=18 或设置 STOCK_SDK_NODE)"
    try:
        result = run_job({"op": "ping"}, timeout=20)
        return True, f"ok (stock-sdk {result.get('version', '?')})"
    except StockSDKBridgeError as e:
        return False, str(e)
