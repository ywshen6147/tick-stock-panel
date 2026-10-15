"""stock-sdk 内置数据源 provider。

Original implementation by @forrany (PR #57), migrated to plugin architecture.
核心抓取/归一化逻辑保留原作者实现, 仅调整 import 路径与注册方式。

通过 bridge.mjs 调真实 stock-sdk 抓 A 股行情, 归一化到项目内部 schema。
方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
因此注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime

import polars as pl
import httpx

from app.data_providers.base import AssetType
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.market_time import cn_now
from app.plugins.stocksdk import bridge
from app.tickflow.rate_limits import chunked

logger = logging.getLogger(__name__)

# stock-sdk 支持的数据集(financial 不支持 → 不声明, 自动回退 tickflow)
_DATASETS = ("daily", "adj_factor", "minute", "realtime", "full_minute")

# 每次桥接调用的符号数。桥接内部按 concurrency 并发, 分批仅为进度反馈与超时控制。
_BATCH = 40
_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def _f(v):
    """安全数值转换: None/空/非数字 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s == "-":
        return None
    try:
        n = float(s)
        return n if (n == n) else None  # 排除 NaN
    except (ValueError, TypeError):
        return None


def _is_index_symbol(symbol: str | None) -> bool:
    """app 符号是否指数。

    只有两个组合被指数占用、A 股股票不会使用:
      .SH + 000xxx (上证/中证系列: 000001 上证指数、000300 沪深300、000680 科创综指 …)
      .SZ + 399xxx (深证系列: 399001 深证成指、399006 创业板指 …)
    必须带后缀判断 —— 000001.SZ 是「平安银行」股票, 000001.SH 才是上证指数。
    """
    s = str(symbol or "")
    dot = s.rfind(".")
    if dot == -1:
        return False
    code, suffix = s[:dot], s[dot + 1:].upper()
    if suffix == "SH":
        return len(code) == 6 and code.startswith("000") and code.isdigit()
    if suffix == "SZ":
        return len(code) == 6 and code.startswith("399") and code.isdigit()
    return False


def _normalize_volume_to_hands(item: dict) -> None:
    """把实时快照行的成交量统一到「手」(内部契约), 原地修改 item。

    上游成交量口径**不统一**（2026-10-13 实测）: 腾讯接口对 688 开头(科创板)
    返回「股」、其余返回「手」; 新浪出口恒为「股」。
    bridge 侧已按此归一化, 这里再做一次**幂等**收口 —— 用「成交额 ÷ 现价 ≈ 股数」
    自校验: 若 volume 与股数同阶 → 是「股」→ ÷100; 若约为股数的 1/100 → 已是「手」。

    幂等性: 已折算过的 volume 再次进入时 ratio≈0.01, 不会再被除, 故可安全重复调用。
    未归一化会让 vwap = amount/(volume×100) 缩小 100 倍、vol_rel_5m 变负,
    并污染 intraday_history 的分钟序列, 最终误导 LLM 决策。

    指数豁免: 指数的"价格"是点数, 成交额/点数 无股数含义, 自校验会得到随机比值
    (实测 000001.SH/399001.SZ/000680.SH 分别为 2.36/7.74/0.31)并误除 100 倍,
    故直接跳过。指数走 get_realtime_indices 专用路径。
    """
    if _is_index_symbol(item.get("symbol")):
        return
    vol = _f(item.get("volume"))
    if vol is None or vol <= 0:
        return
    amt = _f(item.get("amount"))          # 已在调用方归一化为「元」
    price = _f(item.get("last_price"))
    if not (amt and amt > 0 and price and price > 0):
        return
    shares = amt / price                  # 由成交额/现价反推的股数
    if shares > 0 and (vol / shares) > 0.1:
        item["volume"] = vol / 100.0
        logger.warning(
            "realtime 成交量口径异常(疑似按「股」上报), 已折算为「手」: %s volume=%s ≈ %.0f 股",
            item.get("symbol"), vol, shares,
        )


def _sina_get_with_retry(url: str, headers: dict, retries: int = 3) -> httpx.Response:
    """带短退避重试的新浪 GET(网络抖动/SSL 握手偶发超时用)。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, headers=headers, timeout=12.0)
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    assert last is not None
    raise last


@dataclass
class _StockSDKConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "stocksdk"
    display_name: str = "stock-sdk（免费行情）"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _yyyymmdd(dt: datetime | None) -> str | None:
    return dt.strftime("%Y%m%d") if dt else None


class StockSDKProvider:
    """内置 stock-sdk 数据源。"""

    name = "stocksdk"
    builtin = True
    # 分钟历史深度能力(可选声明, 未声明视为深历史): stock-sdk 免费分时接口
    # 只保留最近 5 个交易日的 1 分钟数据, 分时档位/默认值据此收窄。
    minute_history_days = 5

    def __init__(self) -> None:
        self.config = _StockSDKConfig()

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        pass

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        logger.info("stock-sdk daily 拉取开始(%d symbols)", len(symbols))
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            job = {
                "op": "daily",
                "symbols": chunk,
                "adjust": "none",
                "start": _yyyymmdd(start_time),
                "end": _yyyymmdd(end_time),
            }
            try:
                result = bridge.run_job(job, timeout=180)
            except bridge.StockSDKBridgeError as e:
                logger.warning("stock-sdk daily 拉取失败(%d symbols): %s", len(chunk), e)
                result = {"rows": {}}
            for sym, rows in (result.get("rows") or {}).items():
                if not rows:
                    continue
                df = normalize_daily(rows, default_symbol=sym, source=self.name)
                if not df.is_empty():
                    # 东财日K行的 timestamp 是「K线日期零点」而非行情时刻, normalize_daily
                    # 会把它改名为 quote_ts; 而 data_integrity 以 quote_ts 判盘中快照
                    # (约定: null=权威历史, 当日时刻<15:30=盘中快照)。零点命中收盘线
                    # 之前 → 整分区被误判「停机前的盘中快照」→ 每次重启都全市场重拉。
                    # 批量日K是权威历史, quote_ts 必须为 null (仅实时 flush 写真实值)。
                    df = df.with_columns(pl.lit(None, dtype=pl.Int64).alias("quote_ts"))
                    frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            job = {
                "op": "adj",
                "symbols": chunk,
                "start": _yyyymmdd(start_time),
                "end": _yyyymmdd(end_time),
            }
            try:
                result = bridge.run_job(job, timeout=240)
            except bridge.StockSDKBridgeError as e:
                logger.warning("stock-sdk adj 拉取失败(%d symbols): %s", len(chunk), e)
                result = {"rows": {}}
            flat: list[dict] = []
            for rows in (result.get("rows") or {}).values():
                flat.extend(rows or [])
            if flat:
                df = normalize_adj_factors(flat, source=self.name)
                if not df.is_empty():
                    frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        period = "".join(ch for ch in str(freq) if ch.isdigit()) or "1"
        logger.info("stock-sdk minute 拉取开始(%d symbols, period=%s)", len(symbols), period)

        # 上游区间查询的分钟 open 为日级常量(伪值), 单日查询才给最新交易日真实
        # 分钟 open → 末尾 3 个自然日逐日单拉(跳过周末, 覆盖周五收盘后场景),
        # 其余历史段仍走单个区间任务控制桥接成本。伪 open 由 _null_degenerate_opens
        # 在 _minute_df 内置 null。
        windows: list[tuple[datetime | None, datetime | None]] = []
        if (
            start_time is not None
            and end_time is not None
            and end_time.date() > start_time.date()
        ):
            tail_start = end_time.date() - timedelta(days=3)
            if tail_start > start_time.date():
                head_end = datetime.combine(tail_start - timedelta(days=1), dtime.max)
                windows.append((start_time, head_end))
            else:
                tail_start = start_time.date()
            day = tail_start
            while day <= end_time.date():
                if day.weekday() < 5:
                    windows.append((
                        datetime.combine(day, dtime.min),
                        datetime.combine(day, dtime.max),
                    ))
                day += timedelta(days=1)
        else:
            windows.append((start_time, end_time))

        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        total = len(chunks) * len(windows)
        step = 0
        for win_start, win_end in windows:
            for chunk in chunks:
                step += 1
                job = {
                    "op": "minute",
                    "symbols": chunk,
                    "period": period,
                    "start": _yyyymmdd(win_start),
                    "end": _yyyymmdd(win_end),
                }
                try:
                    result = bridge.run_job(job, timeout=180)
                except bridge.StockSDKBridgeError as e:
                    logger.warning("stock-sdk minute 拉取失败(%d symbols): %s", len(chunk), e)
                    result = {"rows": {}}
                for sym, rows in (result.get("rows") or {}).items():
                    df = self._minute_df(rows, sym)
                    if not df.is_empty():
                        frames.append(df)
                if on_chunk_done:
                    on_chunk_done(step, total)
        # 末窗口(最新一日)的真实 open 与首窗口可能重叠同日(时区/边界), keep="last"
        # 由上层 _write_minute_partition 的 unique 处理; 这里仅拼接。
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- full_minute (全量分钟, 盘中轮动/板块切换等消费方) ----
    # 由 MinuteRefreshService 分两种轮次消费, 各自的数据源不同:
    #
    # 1) 稳态增量轮 → get_intraday_latest(): stock-sdk 没有 TickFlow
    #    intraday.universe 那样的"全市场每只最新 N 根"单请求端点; 逐标的
    #    cnMinute 全市场扫一轮要 10+ 分钟, 撑不起盘中刷新节奏。故用全市场实时
    #    快照 (单次分页, ~10-15s) 合成"当前分钟K": datetime = 当前北京分钟,
    #    OHLC = 最新价, volume/amount = 0。落盘走 _write_minute_partition 的
    #    unique(symbol,datetime) 幂等合并 (同分钟 keep="last"), 跨分钟累积出全天。
    #
    # 2) 全天修复轮 → get_intraday_batch(): **有界**, 只种"当前分钟", 不拉全天历史。
    #    取舍: 逐标的 cnMinute 拉全市场一天实测 ≈15 分钟 (4 并发); 修复轮持轮次锁,
    #    拉全天会把盘中增量轮一起卡住 → 实时分时反而停摆。故与增量轮同口径。
    #    因此**服务只要从开盘前运行**, 就逐分钟累积出全天分时 (自启动时刻起)。
    #    要补齐历史某日全天分钟 → 走离线并发回补
    #    (backend/scripts/backfill_minute_day.py): 不占轮次锁、不影响盘中实时。
    #    曾踩的坑: 早期实现被误当"真修复", 实际补不了历史, 导致板块轮动/分时图
    #    只能从进程启动时刻开始 (实测 2026-09-30 从 13:35 起)。
    #
    # 局限: 合成K无真实 OHLC/量额 → 活跃度维度退化 (卡片有 has_amount 降级路径),
    # 离线回补并入真实K后量额恢复。
    def get_intraday_latest(self, count: int = 3) -> pl.DataFrame:
        """全市场实时快照 → 当前分钟合成K (每只标的一根, 增量轮)。"""
        del count  # 快照只取当前盘口, 无"最近 N 根"概念
        rows = self.get_realtime()
        if not rows:
            return pl.DataFrame()
        # 保留时区 (Asia/Shanghai): _enforce_minute_beijing_wallclock 对 tz-aware
        # 输入走「转北京墙钟」分支, 不依赖交易时段启发式 — 午休/收盘后自测也能过
        now = cn_now().replace(second=0, microsecond=0)
        bars: list[dict] = []
        for row in rows:
            symbol = row.get("symbol")
            price = row.get("last_price")
            if not symbol or price is None:
                continue
            price = float(price)
            bars.append({
                "symbol": symbol,
                "datetime": now,
                "open": price, "high": price, "low": price, "close": price,
                "volume": 0.0, "amount": 0.0,
            })
        if not bars:
            return pl.DataFrame()
        return pl.DataFrame(bars, schema={
            "symbol": pl.Utf8, "datetime": pl.Datetime("us", "Asia/Shanghai"),
            "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
            "volume": pl.Float64, "amount": pl.Float64,
        })

    def get_intraday_batch(self, symbols: list[str] | None = None, count: int = 300) -> pl.DataFrame:
        """有界修复轮: 全市场实时快照 → 当前分钟合成K, 可按 symbols 过滤。

        **不拉全天历史**: 全市场逐标的 cnMinute 一天 ≈15 分钟, 会卡住增量轮
        (见上方取舍说明)。历史某日的全天补齐走离线并发回补脚本。
        """
        df = self.get_intraday_latest(count=count)
        if symbols and not df.is_empty():
            wanted = set(symbols)
            df = df.filter(pl.col("symbol").is_in(wanted))
        return df

    @staticmethod
    def _null_degenerate_opens(df: pl.DataFrame) -> pl.DataFrame:
        """把"日级常量"的伪分钟 open 置 null。

        stock-sdk 上游对历史日的分钟 open 只给全天常量(如涨跌停价/日开),
        并非真实分钟开盘价; 只有最新交易日在单日查询下给真实值。伪 open 入库
        会让分钟K的 close-vs-open 口径全偏(如分时量恒红), 故按日检测:
        同日 rows>10 且 open 唯一值<=3 而 close 唯一值>10 → open 判定非分钟级,
        置 null(fail-closed, 不伪造 prev_close 替代)。
        """
        if df.is_empty() or "open" not in df.columns or "datetime" not in df.columns:
            return df
        stats = df.group_by(pl.col("datetime").dt.date()).agg(
            pl.len().alias("n"),
            pl.col("open").n_unique().alias("uo"),
            pl.col("close").n_unique().alias("uc"),
        )
        fake_dates = stats.filter(
            (pl.col("n") > 10) & (pl.col("uo") <= 3) & (pl.col("uc") > 10)
        )["datetime"]
        if fake_dates.is_empty():
            return df
        logger.warning(
            "stock-sdk minute open 为日级常量, 置 null: %s %s",
            df["symbol"][0] if "symbol" in df.columns else "?", fake_dates.to_list(),
        )
        return df.with_columns(
            pl.when(pl.col("datetime").dt.date().is_in(fake_dates))
            .then(None)
            .otherwise(pl.col("open"))
            .alias("open")
        )

    @staticmethod
    def _minute_df(rows: list[dict], symbol: str) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        # bridge 分钟行含 timestamp(ms, UTC 基准)。A 股分时按北京时间墙钟展示,
        # 故转 Asia/Shanghai 后去掉时区得到 naive 北京时间(如 09:35)。
        if "timestamp" in df.columns:
            df = df.with_columns(
                pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
                .dt.replace_time_zone("UTC")
                .dt.convert_time_zone("Asia/Shanghai")
                .dt.replace_time_zone(None)
                .cast(pl.Datetime("us"))
                .alias("datetime")
            )
        elif "date" in df.columns:
            df = df.with_columns(
                pl.col("date").str.to_datetime("%Y-%m-%d %H:%M", strict=False).alias("datetime")
            )
        df = df.with_columns(pl.lit(symbol).alias("symbol"))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        df = StockSDKProvider._null_degenerate_opens(df)
        keep = [c for c in _MINUTE_CANONICAL if c in df.columns]
        return df.select(keep) if "datetime" in keep else pl.DataFrame()

    # ---- realtime (全市场快照) ----
    def get_realtime(self, symbols: list[str] | None = None) -> list[dict]:
        """拉取实时行情。传 symbols(自选/指定) 时走腾讯单股报价(稳定可达)；
        不传时走全市场快照(东财 clist, 绕过 linkdiary)。"""
        if symbols:
            logger.info("stock-sdk realtime 拉取开始(%d 只自选)", len(symbols))
            job = {"op": "realtime", "symbols": list(symbols)}
        else:
            logger.info("stock-sdk realtime 拉取开始(全市场快照)")
            job = {"op": "realtime"}
        try:
            result = bridge.run_job(job, timeout=120)
        except bridge.StockSDKBridgeError as e:
            logger.warning("stock-sdk realtime 拉取失败: %s", e)
            return []
        rows = result.get("rows") or []
        normalized: list[dict] = []
        for row in rows:
            item = dict(row)
            # stock-sdk 的 changePercent 是百分数值(-1.15 = -1.15%);
            # provider 入口契约统一使用小数制(-0.0115 = -1.15%)。
            if item.get("change_pct") is not None:
                item["change_pct"] = float(item["change_pct"]) / 100
            # stock-sdk 全量实时行情的 amount 单位为万元;内部日K统一使用元。
            if item.get("amount") is not None:
                item["amount"] = float(item["amount"]) * 10_000
            # 成交量口径收口(幂等): 上游腾讯对新股/科创板等口径不一, 统一折成「手」。
            _normalize_volume_to_hands(item)
            normalized.append(item)
        return normalized

    # ---- 指数实时 (新浪) ----
    # TSP 核心四大指数(000001.SH/399001.SZ/399006.SZ/000680.SH)通过新浪 hq.sinajs.cn
    # 独立接口拉取(s_sh000001/s_sz399001/s_sz399006/s_sh000680), 稳定不反爬。
    # quote_service 通过 get_realtime_indices(symbols) 回调补拉指数, 与股票实时并存。
    _SINA_INDEX_MAP = {
        "000001.SH": "s_sh000001",
        "399001.SZ": "s_sz399001",
        "399006.SZ": "s_sz399006",
        "000680.SH": "s_sh000680",
    }

    def get_realtime_indices(self, symbols: list[str] | None = None) -> list[dict]:
        """拉取指定指数(默认核心四大指数)的实时快照。

        返回与 get_realtime 同构的行(含 symbol), 便于 _process_full_market_records
        按 symbol 识别为指数。新浪返回格式: 名称,现价,涨跌额,涨跌幅%,成交量,成交额。
        成交量已折成「手」、成交额折成「元」, 与 get_realtime 同一契约。
        """
        if not symbols:
            symbols = list(self._SINA_INDEX_MAP.keys())
        wanted = [s for s in symbols if s in self._SINA_INDEX_MAP]
        if not wanted:
            return []
        codes = ",".join(self._SINA_INDEX_MAP[s] for s in wanted)
        url = f"https://hq.sinajs.cn/list={codes}"
        headers = {
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        try:
            resp = _sina_get_with_retry(url, headers=headers, retries=3)
        except Exception as e:  # noqa: BLE001
            logger.warning("新浪指数实时拉取失败: %s", e)
            return []
        rows: list[dict] = []
        for line in resp.text.splitlines():
            line = line.strip()
            m = re.match(r'var hq_str_s_([a-z]+)(\d+)="([^"]*)"', line)
            if not m:
                continue
            _exch, _code, payload = m.group(1), m.group(2), m.group(3)
            fields = payload.split(",")
            if len(fields) < 6:
                continue
            app_symbol = self._app_symbol_from_sina(_exch, _code)
            if not app_symbol or app_symbol not in self._SINA_INDEX_MAP:
                continue
            try:
                pct = _f(fields[3])
                # 量纲收口 —— 新浪 s_ 接口 [4]成交量 的单位**按交易所不同**:
                #   沪市指数 = 万股(= 手/100), 深市指数 = 手; [5]成交额则统一是 万元。
                # 内部契约是 volume=手、amount=元(与自家 kline_index_daily 一致)。
                # 实测 2026-10-13 跨沪深 14 只指数(上证/上证50/沪深300/科创综指/科创50/
                # 中证500 与 深证成指/中小100/创业板50/深证综指…)交叉验证: 沪 6/6 为
                # 万股、深 7/8 为手, 且腾讯同刻与自家指数日K 均为「手」可作基准。
                # 不折算会让沪市指数成交量小 100 倍、所有指数成交额小 10000 倍。
                vol = _f(fields[4])
                if vol is not None:
                    vol = vol * 100 if _exch.lower() == "sh" else vol
                amt = _f(fields[5])
                if amt is not None:
                    amt = amt * 10_000  # 万元 → 元
                rows.append({
                    "symbol": app_symbol,
                    "name": fields[0].strip(),
                    "last_price": _f(fields[1]),
                    "prev_close": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "volume": vol,
                    "amount": amt,
                    "change_pct": (pct / 100) if pct is not None else None,  # 百分数→小数制
                    "timestamp": int(time.time() * 1000),
                })
            except (ValueError, TypeError):
                continue
        return rows

    @staticmethod
    def _app_symbol_from_sina(exch: str, code: str) -> str | None:
        e = exch.lower()
        if e == "sh":
            return f"{code}.SH"
        if e == "sz":
            return f"{code}.SZ"
        if e == "bj":
            return f"{code}.BJ"
        return None

    # ---- instruments (标的维表) ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """返回 tickflow Instrument 形状的行(symbol/name/code/exchange/region/type + ext),

        供 instrument_sync._flatten_instruments 复用同一 flatten 路径, 列结构与 tickflow 一致。
        当前覆盖 A 股股票。
        """
        if asset_type != "stock":
            return []
        try:
            result = bridge.run_job({"op": "instruments"}, timeout=120)
        except bridge.StockSDKBridgeError as e:
            logger.warning("stock-sdk instruments 拉取失败: %s", e)
            return []
        return result.get("rows") or []

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        symbols = symbols or ["600519.SH"]
        if dataset == "daily":
            df = self.get_daily(symbols, None, None)
            return _preview("daily", df)
        if dataset == "adj_factor":
            df = self.get_adj_factors(symbols, None, None)
            return _preview("adj_factor", df)
        if dataset == "minute":
            df = self.get_minute(symbols, None, None)
            return _preview("minute", df)
        if dataset == "realtime":
            rows = self.get_realtime()
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        raise ValueError(f"stock-sdk 不支持数据集: {dataset}")


def _preview(dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": "stocksdk",
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
