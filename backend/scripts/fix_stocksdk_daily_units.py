"""一次性数据修复: stock-sdk 实时「盘中快照」写坏的日K volume/amount 单位。

背景:
  stock-sdk 的实时源走新浪 getHQNodeData (volume=股, amount=元), 而 provider
  get_realtime 按 batch.cn 约定把 amount 当「万元」×10000, volume 未做股→手。
  实时轮询写入 kline_daily 的「盘中快照」因此 volume 放大 100×、amount 放大
  10000× (amount/(volume*close) ≈ 10000, 正常应为 ≈100), 并污染 enriched 的
  换手率/量比/成交额与市场快照 → 概念/行业聚合统计失真。

本脚本:
  1) 对受影响日期的 kline_daily 分区做「按 ratio 定向修复」— 只改 ratio>1000
     的坏行 (volume÷100, amount÷10000), ratio≈100 的正确行保持不动。
  2) 删除对应日期的坏 enriched 分区。
  3) run_pipeline(new_dates_only=True) 从已修复的日K重建 enriched。

用法: python -m scripts.fix_stocksdk_daily_units [date...]
      不带参数时默认修复已知坏日期: 2026-09-05 2026-09-07 2026-09-24 2026-09-28 2026-09-29
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import polars as pl

# 后端 data 目录 (脚本从 backend/ 下运行; 也可被 pytest/CI 直接 import)
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# 默认受影响日期 (stock-sdk 实时快照写入的坏分区)
DEFAULT_DATES = ["2026-09-05", "2026-09-07", "2026-09-24", "2026-09-28", "2026-09-29"]

# 判定阈值: 正常手/元契约 ratio≈100; 坏数据 ratio≈10000。>1000 视为坏行。
_RATIO_BAD = 1000.0


def repair_daily_partition(daily_dir: Path, date_str: str) -> tuple[int, int]:
    """修复单个 kline_daily 分区的坏行。返回 (总行数, 修正行数)。"""
    part_dir = daily_dir / f"date={date_str}"
    part = part_dir / "part.parquet"
    if not part.exists():
        print(f"[skip] {date_str}: 无分区 {part}")
        return 0, 0
    df = pl.read_parquet(part)
    total = df.height
    with_ratio = df.with_columns(
        (pl.col("amount") / (pl.col("volume") * pl.col("close"))).alias("__ratio")
    )
    bad_mask = (pl.col("__ratio") > _RATIO_BAD) & pl.col("volume").is_not_null() & (pl.col("volume") > 0)
    bad = with_ratio.filter(bad_mask)
    if bad.height == 0:
        print(f"[ok] {date_str}: 无坏行 (total={total})")
        return total, 0
    fixed = bad.with_columns(
        (pl.col("volume") / 100.0).alias("volume"),
        (pl.col("amount") / 10000.0).alias("amount"),
    ).drop("__ratio")
    good = with_ratio.filter(~bad_mask).drop("__ratio")
    # 统一数值列为 Float64, 避免 good/fixed 的 Int/Float 不一致导致 vstack 失败
    for c in ("volume", "amount"):
        good = good.with_columns(pl.col(c).cast(pl.Float64, strict=False))
        fixed = fixed.with_columns(pl.col(c).cast(pl.Float64, strict=False))
    out = pl.concat([good, fixed]).sort(["symbol", "date"])
    # 原子替换: 先写临时文件再 rename, 避免后端并发读取到半截文件
    tmp = part_dir / "part.parquet.tmp"
    out.write_parquet(tmp)
    tmp.replace(part)
    print(f"[fix] {date_str}: 修正 {bad.height}/{total} 行 (volume÷100, amount÷10000)")
    return total, bad.height


def remove_bad_enriched(enriched_dir: Path, date_str: str) -> bool:
    """删除坏 enriched 分区, 触发管道重建。"""
    p = enriched_dir / f"date={date_str}"
    if p.exists():
        shutil.rmtree(p)
        print(f"[rm] enriched {date_str}: 已删除 (待重建)")
        return True
    return False


def main() -> int:
    dates = sys.argv[1:] or DEFAULT_DATES
    daily_dir = DATA_DIR / "kline_daily"
    enriched_dir = DATA_DIR / "kline_daily_enriched"

    total_fixed = 0
    for ds in dates:
        total, fixed = repair_daily_partition(daily_dir, ds)
        total_fixed += fixed
        remove_bad_enriched(enriched_dir, ds)

    print(f"\n日K修正合计: {total_fixed} 行; 开始重建 enriched …")
    from app.indicators.pipeline import run_pipeline  # noqa: E402

    written = run_pipeline(new_dates_only=True)
    print(f"enriched 重建完成, 写入 {written} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
