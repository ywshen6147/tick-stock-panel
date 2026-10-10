"""离线回补某一交易日的全市场分钟K → data/kline_minute/date=YYYY-MM-DD。

为什么需要独立脚本（而不是让 MinuteRefreshService 的修复轮去做）:
  逐标的 cnMinute 拉全市场一天实测 ≈15 分钟（4 并发）。修复轮持有轮次锁，
  拉全天会把盘中增量轮一并卡住 → 盘中分时反而停摆。故盘中修复轮保持有界
  （只种当前分钟），历史某日补齐留给本脚本离线执行。

用法（在后端目录下，用项目 venv）::

    # 盘中「中途才启动 TSP」→ 补齐今天上午的缺口（默认日=今天，不必写 --date）
    .venv\\Scripts\\python.exe scripts\\backfill_minute_day.py

    # 盘后 / 周末补最近交易日
    .venv\\Scripts\\python.exe scripts\\backfill_minute_day.py

    # 指定日期 + 并发 + 只回补前 N 只（先试跑）
    .venv\\Scripts\\python.exe scripts\\backfill_minute_day.py --date 2026-09-30 --workers 6 --limit 200

为什么需要它（自动路径为什么**不**补历史）:
  MinuteRefreshService 的盘中全市场分钟是"实时快照 → 合成当前分钟K"逐分钟累积,
  只能从**服务启动那一刻**开始有数据; 而"全天修复轮"刻意保持有界(只种当前分钟),
  因为逐标的 cnMinute 拉全市场一天实测 ≈15 分钟, 修复轮持轮次锁 → 会把盘中实时轮
  一并卡住。所以「中途启动」留下的上午缺口由本脚本离线补齐(不占轮次锁)。

特性:
  - 幂等: 走 kline_sync._write_minute_partition 的 unique(symbol, datetime)
    keep="last" 合并 — 重复执行/中断重跑都安全, 且能与盘中合成分钟共存
    (合成走 kline_minute_rt, 本脚本写真实主分区 kline_minute)。
  - 并发按 chunk 分块（provider 内部 40 只/请求），单块失败重试一次后跳过,
    不拖垮整轮; 失败块可重跑补齐。
  - 数据源: full_minute_data_provider 偏好的自定义源（默认 stocksdk）。
  - 可直接在 TSP 运行中执行（不抢 mining 锁、不动 kline_minute_rt）。
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime
from pathlib import Path

# 允许 `python scripts/xxx.py` 直接运行时找到 app 包
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import polars as pl  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import kline_sync  # noqa: E402
from app.services import preferences  # noqa: E402


def _latest_daily_date(data_dir: Path) -> str | None:
    """kline_daily 最新分区日期。"""
    parts = sorted((data_dir / "kline_daily").glob("date=*"))
    return parts[-1].name.split("=", 1)[1] if parts else None


def _default_target(data_dir: Path) -> str | None:
    """默认回补日 = 我们能看到的**最新**日期。

    取 kline_daily 最新分区与两个分钟目录最新分区的最大值:
      - 盘中"中途启动"场景: 合成分钟目录(kline_minute_rt)当天已有分区 →
        默认就是**今天**, 不必手写 --date 就能补今天上午;
      - 盘后/周末想补上一个交易日: 分钟目录还是上一交易日 → 默认即那天。
    """
    candidates: list[str] = []
    daily = _latest_daily_date(data_dir)
    if daily:
        candidates.append(daily)
    for name in ("kline_minute_rt", "kline_minute"):
        parts = sorted((data_dir / name).glob("date=*"))
        if parts:
            candidates.append(parts[-1].name.split("=", 1)[1])
    return max(candidates) if candidates else None


def _resolve_provider():
    """按 full_minute 偏好解析自定义分钟源（与 MinuteRefreshService 同一入口）。"""
    try:
        name = preferences.get_full_minute_data_provider()
    except Exception:  # noqa: BLE001 — 偏好读取异常按默认源处理
        name = "stocksdk"
    provider, use_tickflow, err = kline_sync._resolve_full_minute_provider(name)
    if not use_tickflow and provider is not None and callable(getattr(provider, "get_minute", None)):
        return name, provider
    # 偏好非自定义源（或解析失败）→ 直接试 stocksdk 插件
    fallback, use_tf2, err2 = kline_sync._resolve_full_minute_provider("stocksdk")
    if not use_tf2 and fallback is not None and callable(getattr(fallback, "get_minute", None)):
        return "stocksdk", fallback
    raise SystemExit(f"找不到可用的分钟数据源（偏好 {name}: {err or err2}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="离线回补某交易日全市场分钟K")
    ap.add_argument("--date", help="YYYY-MM-DD，默认取最新可见日期（盘中=今天，盘后/周末=最近交易日）")
    ap.add_argument("--workers", type=int, default=4, help="并发块数（默认 4）")
    ap.add_argument("--limit", type=int, default=0, help="只回补前 N 只（试跑用，0=全部）")
    ap.add_argument("--chunk", type=int, default=40, help="每块标的数（默认 40）")
    args = ap.parse_args()

    data_dir = Path(settings.data_dir)
    target = args.date or _default_target(data_dir)
    if not target:
        print("无法确定回补日期（无任何日线/分钟分区），请用 --date 指定")
        return 2
    day = date.fromisoformat(target)
    start = datetime.combine(day, dtime(9, 0))
    end = datetime.combine(day, dtime(15, 30))

    provider_name, provider = _resolve_provider()
    instruments = provider.get_instruments("stock") or []
    symbols = [i["symbol"] for i in instruments if i.get("symbol")]
    if args.limit:
        symbols = symbols[: args.limit]
    if not symbols:
        print("标的池为空（instruments 未加载）")
        return 2

    chunks = [symbols[i:i + args.chunk] for i in range(0, len(symbols), args.chunk)]
    print(f"回补日 {target}  数据源 {provider_name}  标的 {len(symbols)}  块数 {len(chunks)}  并发 {args.workers}")

    frames: list[pl.DataFrame] = []
    failed: list[list[str]] = []
    t0 = time.perf_counter()

    def _fetch(chunk: list[str]) -> tuple[pl.DataFrame | None, bool]:
        for attempt in (1, 2):  # 单块失败重试一次
            try:
                df = provider.get_minute(chunk, start, end, freq="1m")
                return (df, True)
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f"  块失败({len(chunk)}只): {exc}")
        return (None, False)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for chunk, (df, ok) in zip(chunks, pool.map(_fetch, chunks), strict=True):
            done += 1
            if ok and df is not None and not df.is_empty():
                frames.append(df)
            elif not ok:
                failed.append(chunk)
            if done % 10 == 0 or done == len(chunks):
                el = time.perf_counter() - t0
                print(f"  进度 {done}/{len(chunks)} 块  已取 {sum(f.height for f in frames)} 行  {el:.0f}s")

    if not frames:
        print("未取到任何数据（上游异常？）")
        return 1

    merged = pl.concat(frames, how="vertical_relaxed")
    merged = merged.unique(subset=["symbol", "datetime"], keep="last", maintain_order=True)
    try:
        merged = kline_sync._enforce_minute_beijing_wallclock(merged, source=provider_name)
    except Exception as exc:  # noqa: BLE001 — 守卫失败不写入，避免污染分区
        print(f"分钟 datetime 契约校验失败，中止: {exc}")
        return 1

    out_dir = data_dir / "kline_minute"
    written = kline_sync._write_minute_partition(merged, out_dir)
    el = time.perf_counter() - t0
    print(f"完成: 写入 {written} 行 → {out_dir.name}/date={target}/  "
          f"标的 {merged['symbol'].n_unique()}  耗时 {el:.0f}s")
    if failed:
        print(f"失败块 {len(failed)} 个（{sum(len(c) for c in failed)} 只）— 可重跑本脚本补齐（幂等）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
