"""东财 F10 财务数据 provider — 零鉴权, 纯 HTTP。

数据来源
--------
东方财富数据中心 F10 财报接口 ``datacenter.eastmoney.com/securities/api/data/v1/get``
(``source=HSF10``), 免注册免 Key, 覆盖沪深北全市场:

==============================  ===================================
表                              上游 reportName
==============================  ===================================
``income`` 利润表                ``GINCOME``
``balance_sheet`` 资产负债表     ``GBALANCE`` → ``BBALANCE`` → ``IBALANCE`` → ``SBALANCE``
``cash_flow`` 现金流量表         ``GCASHFLOW`` → ``BCASHFLOW`` → ``ICASHFLOW`` → ``SCASHFLOW``
``metrics`` 主要指标             ``MAINFINADATA``
``shares`` 历史股本              ``EH_EQUITY``(股本变动历史, 事件驱动)
==============================  ===================================

后缀 G/B/I/S = 东财按行业分表(通用工商 / 银行 / 保险 / 证券)。三张分表互不重叠:
银行股在 ``GBALANCE`` 里返回空, 必须下探 ``BBALANCE``; 因此资产负债表与现金流量表
按 G→B→I→S 回退。利润表的 ``GINCOME`` 实测已覆盖全部行业, 无需回退。

**为什么 shares 不用 MAINFINADATA**: 上游 ``MAINFINADATA`` 的 ``TOTAL_SHARE`` /
``A_FREE_SHARE`` 在历史期上是**错的** —— 半年报/年报(06-30、12-31)行携带的是
「当前」股本, 而一季报/三季报行携带旧股本, 于是同一只股票在 8 个报告期里来回跳。
实例(688553.SH): 2025-06-30 与 2025-12-31 给出 625,900,859, 但该股 2026-06-17
才『转增股上市』(见 ``EH_EQUITY``), 上述两期真实值应为 423,600,000。全市场
347/5571 只标的受此影响, 且因为 ``share_capital.apply_historical_float_shares()``
会把它当作 point-in-time 流通股本喂给回测, 属**静默污染**。故 shares 改用
东财专门的**股本变动历史**端点 ``RPT_F10_EH_EQUITY``: 每行是一次股本变动事件
(``END_DATE`` 变动日 / ``NOTICE_DATE`` 公告日), 天然适配「公告日可用 + asof 回填」。

设计要点
--------
1. **只声明 financial 数据集** — 其余数据集未声明, 自动保持原路由(`provider_has_dataset`
   返回 False), 因此本次接入不会改动日K/实时/分钟/除权/五档的任何配置。
2. **批量取数** — 上游支持 ``(SECUCODE in (...))(REPORT_DATE in (...))`` 组合过滤,
   pageSize 硬上限 500。按「标的批次 × 报告期」装填, 单请求接近 500 行:
   全市场 5571 只 × 8 期 ≈ 106 请求/表, 而非逐股 5571 次。
3. **单位遵项目契约** — metrics 全部为**百分点**(``factors/registry.py`` 声明
   "最新已公告净资产收益率(%)"), 与前端 ``fmt: 'pct'`` 一致。上游
   ``JYXJLYYSR``(经营现金流/营收) 给的是小数比率(0.779), 此处 ×100 转百分点,
   否则前端会显示成 0.78% 而非 77.94%。
4. **不猜数据** — 上游不提供的字段一律 ``None``(如稀释 ROE), 不做启发式补全。

已知边界
--------
- 三大报表为一季度/半年/三季/年报的**累计值**(与 TickFlow/扶摇口径一致), 非单季。
- ETF / 指数 无财报, 上游不返回行, 属预期行为。
- 报告期回溯深度取决于上游, 本插件一次最多取最近 8 期。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
import polars as pl

logger = logging.getLogger(__name__)

_API = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_HEADERS = {
    "Referer": "https://emweb.securities.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

_PAGE_SIZE = 500          # 上游硬上限: 实测传 1000/2000 仍只返回 500 行
_ROW_BUDGET = 480         # 单请求行预算(留余量), 除以期数得每批标的数
_HISTORY_PERIODS = 8      # 历史模式下每只标的保留的报告期数
_MAX_PAGES = 4            # 同一过滤条件翻页上限(防上游 count 异常时死循环)
_MAX_BATCH_SYMBOLS = 200  # 单批标的上限(单期模式下不希望 URL 过长)
_INTERVAL_S = 0.25        # 请求间隔 ≈4 req/s, 对上游友好
_TIMEOUT_S = 30.0
_RETRIES = 2
_CACHE_ENTRIES = 6        # 批量取数缓存保留的条目数

# ---- 股本变动历史 (shares) ----
_EQUITY_REPORT = "RPT_F10_EH_EQUITY"
_EQUITY_DATE_FIELD = "END_DATE"   # 变动日; 公告日另在 NOTICE_DATE
_EQUITY_PAGES = 6                 # 事件密集标的(万科A 全集 162 条)需要更多翻页
_EQUITY_ROWS_PER_SYMBOL = 34      # 全量历史的事件数预估, 仅用于把行预算摊成每批标的数
_EQUITY_MAX_EVENTS = 80           # 每只标的保留的最近事件数上限(取最近, 不取最早)
#
# 刻意**不设**日期下限: 股本变动是事件流, 长期不动的老公司可能十几年没有新事件,
# 一旦按日期截断, 这些标的会整只缺失(shares 表里查不到任何行)。实测截到 2018 年
# 会漏掉 177/5571 只(如 000020.SZ 最近一次变动停在 2015-12-29)。因此改为取全量、
# 再靠 _EQUITY_MAX_EVENTS 逐股封顶, 保证「有股本的标的必有行」。

# 数据集 -> 上游 reportName 按行业回退链。
#
# 东财 F10 的报表按行业分表: G=通用工商 / B=银行 / I=保险 / S=证券, 三张分表
# 互不重叠 —— 银行/保险/证券股在 G* 表里返回「返回数据为空」(实测: 000001.SZ
# 在 GBALANCE/GCASHFLOW 无数据, 在 BBALANCE/BCASHFLOW 有)。因此按 G→B→I→S
# 顺序回退, 每档只对上一档没取到任何报告期的标的做下探。
# 全市场金融股约 100 只, 回退成本很小(每档 1~2 个请求)。
#
# 利润表例外: GINCOME 实测已覆盖全部行业(银行/保险/证券均有数据), 不回退。
# 金融分表的字段名与通用表一致(实测 BBALANCE 命中 11/19, 差额全是银行本就
# 没有的「流动/非流动资产」分类字段), 因此三张表共用同一套 field_map。
_METRICS_REPORT = "RPT_F10_FINANCE_MAINFINADATA"
_REPORTS: dict[str, tuple[str, ...]] = {
    "income": ("RPT_F10_FINANCE_GINCOME",),
    "balance_sheet": (
        "RPT_F10_FINANCE_GBALANCE",
        "RPT_F10_FINANCE_BBALANCE",
        "RPT_F10_FINANCE_IBALANCE",
        "RPT_F10_FINANCE_SBALANCE",
    ),
    "cash_flow": (
        "RPT_F10_FINANCE_GCASHFLOW",
        "RPT_F10_FINANCE_BCASHFLOW",
        "RPT_F10_FINANCE_ICASHFLOW",
        "RPT_F10_FINANCE_SCASHFLOW",
    ),
    "metrics": (_METRICS_REPORT,),
    # 注意: shares 不在此表内 —— 它走 _EQUITY_REPORT(股本变动历史), 见 get_financials。
}

# 上游字段 -> 项目 canonical 列名(与 plugins/fuyao/provider.py 的口径对齐)
_INCOME_MAP = {
    "OPERATE_INCOME": "revenue",
    "OPERATE_COST": "operating_cost",
    "SALE_EXPENSE": "selling_expense",
    "MANAGE_EXPENSE": "admin_expense",
    "RESEARCH_EXPENSE": "rd_expense",
    "OPERATE_PROFIT": "operating_profit",
    "FINANCE_EXPENSE": "financial_expense",
    "TOTAL_PROFIT": "total_profit",
    "INCOME_TAX": "income_tax",
    "NETPROFIT": "net_income",
    "PARENT_NETPROFIT": "net_income_attributable",
    "DEDUCT_PARENT_NETPROFIT": "net_income_deducted",
    "NONBUSINESS_INCOME": "non_operating_income",
    "NONBUSINESS_EXPENSE": "non_operating_expense",
    "BASIC_EPS": "basic_eps",
    "DILUTED_EPS": "diluted_eps",
}

_BALANCE_MAP = {
    "TOTAL_ASSETS": "total_assets",
    "TOTAL_CURRENT_ASSETS": "total_current_assets",
    "TOTAL_NONCURRENT_ASSETS": "total_non_current_assets",
    "MONETARYFUNDS": "cash_and_equivalents",
    "ACCOUNTS_RECE": "accounts_receivable",
    "INVENTORY": "inventory",
    "FIXED_ASSET": "fixed_assets",
    "INTANGIBLE_ASSET": "intangible_assets",
    "GOODWILL": "goodwill",
    "TOTAL_LIABILITIES": "total_liabilities",
    "TOTAL_CURRENT_LIAB": "total_current_liabilities",
    "TOTAL_NONCURRENT_LIAB": "total_non_current_liabilities",
    "SHORT_LOAN": "short_term_borrowing",
    "LONG_LOAN": "long_term_borrowing",
    "ACCOUNTS_PAYABLE": "accounts_payable",
    "TOTAL_EQUITY": "total_equity",
    "TOTAL_PARENT_EQUITY": "equity_attributable",
    "UNASSIGN_RPOFIT": "retained_earnings",   # 上游拼写确为 RPOFIT
    "MINORITY_EQUITY": "minority_interest",
}

_CASHFLOW_MAP = {
    "NETCASH_OPERATE": "net_operating_cash_flow",
    "NETCASH_INVEST": "net_investing_cash_flow",
    "NETCASH_FINANCE": "net_financing_cash_flow",
    "CONSTRUCT_LONG_ASSET": "capex",
    "CCE_ADD": "net_cash_change",
}

# 主要指标: 上游全部为百分点(16.75 = 16.75%), 与注册表 "(%)" 声明一致。
# 例外仅 JYXJLYYSR(经营现金流/营收) 在上游是小数比率, 见 _SCALE。
_METRICS_MAP = {
    "EPSJB": "eps_basic",
    "BPS": "bps",
    "MGJYXJJE": "ocfps",
    "ROEJQ": "roe",                      # 加权 ROE
    "ZZCJLL": "roa",                     # 总资产净利率
    "XSMLL": "gross_margin",
    "XSJLL": "net_margin",
    "ZCFZL": "debt_to_asset_ratio",
    "TOTALOPERATEREVETZ": "revenue_yoy",
    "PARENTNETPROFITTZ": "net_income_yoy",
    "JYXJLYYSR": "operating_cash_to_revenue",
    "CHZZL": "inventory_turnover",       # 存货周转率(次)
    # ROEKCJQ(扣非加权 ROE) 语义上不是"稀释 ROE", 不映射到 roe_diluted, 留空由前端显示 —
}

# 股本变动历史(RPT_F10_EH_EQUITY) -> canonical 映射。
#
# 流通股本必须取 LISTED_A_SHARES(流通A股)。既不是 UNLIMITED_SHARES, 也不是
# TOTAL_SHARES - LIMITED_A_SHARES —— 后两者对含 H/B 股的发行人会把非 A 股股本
# 一并算进来。以本地 instruments.parquet 的 float_shares 为准实测 10/10 命中:
#   601318 中国平安 TOTAL=181.08亿  TOTAL-限售A=181.08亿(✗)  流通A=106.60亿(✓)
#   300750 宁德时代 TOTAL=46.27亿   UNLIMITED=44.79亿(✗)     流通A=42.61亿(✓)
#   002594 比亚迪   TOTAL=91.17亿   TOTAL-限售A=71.70亿(✗)   流通A=34.87亿(✓)
# total_shares 取 TOTAL_SHARES, 与 instruments.total_shares 亦 10/10 一致。
_SHARES_MAP = {
    "TOTAL_SHARES": "total_shares",      # 总股本, 单位: 股
    "LISTED_A_SHARES": "float_shares",   # 流通A股, 单位: 股
}

# 极端状态兜底字段: 尚无任何可流通股份时(上市前), 上游把 TOTAL_SHARES 与
# LIMITED_A_SHARES 设为相等而 LISTED_A_SHARES 留空(实测 0.4% 的行, 如 688836.SH)。
# 此时流通股本确为 0。仅在 LISTED_A_SHARES 为空且二者相等时判定, 不做其它猜测。
_SHARES_LIMITED_SRC = "LIMITED_A_SHARES"

_FIELD_MAPS: dict[str, dict[str, str]] = {
    "income": _INCOME_MAP,
    "balance_sheet": _BALANCE_MAP,
    "cash_flow": _CASHFLOW_MAP,
    "metrics": _METRICS_MAP,
    "shares": _SHARES_MAP,
}

# 需要换算单位的 canonical 列(上游给小数比率, 项目契约要百分点)
_SCALE: dict[str, dict[str, float]] = {
    "metrics": {"operating_cash_to_revenue": 100.0},
}

_COMMON_SRC = ("SECUCODE", "REPORT_DATE", "NOTICE_DATE")


@dataclass
class _Config:
    """占位配置对象 — loader 会读 .name / .display_name / .datasets, 保持接口一致。

    ``datasets`` 的键必须是**数据集名**(``financial``), 不是财务表名: 路由门控
    ``provider_has_dataset(provider, "financial")`` 直接查这个键, 键写错会让
    能力矩阵显示可选、但实际路由时静默回退到 TickFlow。
    """

    name: str = "em_financial"
    display_name: str = "东财F10财务"
    path: Any = None
    datasets: dict[str, Any] = field(default_factory=lambda: {"financial": {}})


def availability() -> tuple[bool, str]:
    """本插件零依赖零 Key, 恒可用。"""
    return True, "ok"


def _quarter_ends(today: date, count: int) -> list[str]:
    """返回 <= today 的最近 count 个季度末日期(降序, ISO 字符串)。

    最近一个季度末可能尚未披露(如 10 月初的 09-30), 因此调用方会多取一期做候选,
    再按实际返回的报告期截断。上游 REPORT_DATE 过滤只认 'YYYY-MM-DD' 字面量。
    """
    out: list[str] = []
    year, month = today.year, today.month
    while len(out) < count:
        if month in (3, 6, 9, 12):
            day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
            candidate = date(year, month, day)
            if candidate <= today:
                out.append(candidate.isoformat())
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def _norm_symbols(symbols: list[str] | None) -> list[str]:
    """归一为东财 SECUCODE 口径(``600519.SH``), 保持输入顺序并去重。

    TSP 内部 symbol 已是该格式; 这里额外容忍裸 6 位代码, 按号段补后缀,
    其余无法辨认的原样透传(由上游返回空来体现, 不静默丢弃)。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols or []:
        text = str(raw or "").strip().upper()
        if not text:
            continue
        if "." not in text and len(text) == 6 and text.isdigit():
            head = text[0]
            if head in ("6", "9"):
                text = f"{text}.SH"
            elif head in ("4", "8") or text.startswith("920"):
                text = f"{text}.BJ"
            else:
                text = f"{text}.SZ"
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _iso_date(value: Any) -> str | None:
    """上游 '2026-06-30 00:00:00' -> '2026-06-30'(项目财务表的存储口径)。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.split(" ")[0].split("T")[0] or None


def _to_float(value: Any) -> float | None:
    """数值列转换: 非数值/空 -> None(不猜、不补 0)。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class EmFinancialProvider:
    """东财 F10 财报 provider — 只实现 financial 数据集所需的方法。"""

    name = "em_financial"
    builtin = True

    def __init__(self) -> None:
        self.config = _Config()
        self._client = httpx.Client(
            timeout=_TIMEOUT_S, headers=_HEADERS, follow_redirects=True,
        )
        # 批量取数的投影结果缓存, 按 (report, 过滤条件, 投影字段) 命中。
        # 容量很小(按需淘汰最早项), 避免长驻内存 —— shares 全量约 10 万行,
        # 属最大一项, 故上限设在个位数条。
        self._cache: dict[tuple, list[dict]] = {}
        self._cache_order: list[tuple] = []

    # ---------------------------------------------------------------- 生命周期
    def close(self) -> None:
        """load_all 重建注册表时会调用。"""
        try:
            self._client.close()
        finally:
            self._cache.clear()
            self._cache_order.clear()

    # ---------------------------------------------------------------- 上游取数
    def _get_json(self, params: dict[str, Any]) -> dict:
        """带重试的单次请求。"""
        last: Exception | None = None
        for attempt in range(_RETRIES + 1):
            try:
                resp = self._client.get(_API, params=params)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001 — 网络类异常统一重试
                last = e
                if attempt < _RETRIES:
                    time.sleep(0.8 * (attempt + 1))
        assert last is not None
        raise last

    def _fetch_page(
        self,
        report: str,
        secucodes: list[str],
        keys: tuple[str, ...],
        *,
        date_field: str = "REPORT_DATE",
        dates: list[str] | None = None,
        max_pages: int = _MAX_PAGES,
    ) -> list[dict]:
        """按标的批次过滤取数(可翻页), 就地投影到需要的字段。

        投影很重要: GBALANCE 单行 319 个字段, 全市场 8 期若保留原始行会是数 GB;
        只留下 canonical 映射所需的键后, 内存占用回到几十 MB 量级。

        ``dates`` 给报告期等值过滤(四张财务报表); 不给则取该批标的的全部记录
        (股本变动历史走这条路径, 不设日期下限)。
        """
        symbol_list = ",".join(f'"{code}"' for code in secucodes)
        parts = [f"(SECUCODE in ({symbol_list}))"]
        if dates:
            date_list = ",".join(f"'{d}'" for d in dates)
            parts.append(f"({date_field} in ({date_list}))")
        flt = "".join(parts)

        rows: list[dict] = []
        page = 1
        while page <= max_pages:
            payload = self._get_json({
                "reportName": report,
                "columns": "ALL",
                "pageNumber": page,
                "pageSize": _PAGE_SIZE,
                "sortTypes": -1,
                "sortColumns": date_field,
                "source": "HSF10",
                "client": "PC",
                "filter": flt,
            })
            result = payload.get("result") or {}
            chunk = result.get("data") or []
            for raw in chunk:
                if isinstance(raw, dict):
                    rows.append({k: raw.get(k) for k in keys})
            total = int(result.get("count") or 0)
            if len(chunk) < _PAGE_SIZE or len(rows) >= total:
                break
            page += 1
        return rows

    def _batch_fetch(
        self,
        report: str,
        secucodes: list[str],
        keys: tuple[str, ...],
        *,
        date_field: str = "REPORT_DATE",
        dates: list[str] | None = None,
        rows_per_symbol: int = 1,
        max_pages: int = _MAX_PAGES,
    ) -> list[dict]:
        """按行预算分批取全量标的, 单批失败只隔离该批。"""
        cache_key = (
            report, date_field, tuple(secucodes), tuple(dates or ()), keys,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        est = len(dates) if dates else max(1, rows_per_symbol)
        per_batch = max(1, min(_ROW_BUDGET // est, _MAX_BATCH_SYMBOLS))
        total_batches = (len(secucodes) + per_batch - 1) // per_batch
        rows: list[dict] = []
        for index in range(0, len(secucodes), per_batch):
            chunk = secucodes[index:index + per_batch]
            batch_no = index // per_batch + 1
            try:
                rows.extend(self._fetch_page(
                    report, chunk, keys,
                    date_field=date_field, dates=dates, max_pages=max_pages,
                ))
            except Exception as e:  # noqa: BLE001 — 隔离失败批次, 不中断整表同步
                logger.warning(
                    "em_financial %s 第 %d/%d 批失败(%d 只), 已跳过: %s",
                    report, batch_no, total_batches, len(chunk), e,
                )
            time.sleep(_INTERVAL_S)

        if not rows:
            logger.debug("em_financial %s 本档未取到行(批次 %d)", report, total_batches)

        self._cache[cache_key] = rows
        self._cache_order.append(cache_key)
        while len(self._cache_order) > _CACHE_ENTRIES:
            self._cache.pop(self._cache_order.pop(0), None)
        return rows

    def _fetch_chain(
        self, reports: tuple[str, ...], secucodes: list[str], dates: list[str],
        keys: tuple[str, ...], label: str,
    ) -> list[dict]:
        """按行业分表回退链取数: 上一档没覆盖到的标的才下探下一档。"""
        rows: list[dict] = []
        remaining = list(secucodes)
        primary_covered = 0
        for step, report in enumerate(reports):
            if not remaining:
                break
            got = self._batch_fetch(report, remaining, keys, dates=dates)
            rows.extend(got)
            covered = {str(r.get("SECUCODE") or "") for r in got}
            before = len(remaining)
            remaining = [code for code in remaining if code not in covered]
            if step == 0:
                primary_covered = before - len(remaining)
            elif before != len(remaining):
                logger.debug(
                    "em_financial %s: 回退 %s 补到 %d 只, 余 %d 只",
                    label, report, before - len(remaining), len(remaining),
                )
        if primary_covered == 0:
            # 主表一行都没取到: 上游结构可能变了, 必须显式告警而不是静默空表
            logger.warning(
                "em_financial %s 主表 %s 未取到任何行 — 请检查上游接口是否变更",
                label, reports[0],
            )
        if remaining:
            logger.info(
                "em_financial %s: %d 只标的在上游全部分表均无数据(ETF/指数或未披露)",
                label, len(remaining),
            )
        return rows

    # ---------------------------------------------------------------- 数据组织
    @staticmethod
    def _to_frame(
        table: str, rows: list[dict], periods: int
    ) -> pl.DataFrame:
        """原始行 -> canonical 表: 按 symbol 分组、报告期降序取前 periods 期。"""
        field_map = _FIELD_MAPS[table]
        scale = _SCALE.get(table) or {}

        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            code = str(row.get("SECUCODE") or "").strip()
            if not code:
                continue
            by_symbol.setdefault(code, []).append(row)

        records: list[dict] = []
        for code, items in by_symbol.items():
            items.sort(key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
            for row in items[:periods]:
                record: dict[str, Any] = {
                    "symbol": code,
                    "period_end": _iso_date(row.get("REPORT_DATE")),
                    "announce_date": _iso_date(row.get("NOTICE_DATE")),
                }
                for src, dst in field_map.items():
                    value = _to_float(row.get(src))
                    factor = scale.get(dst)
                    if value is not None and factor is not None:
                        value *= factor
                    record[dst] = value
                records.append(record)

        if not records:
            return pl.DataFrame()

        columns = ["symbol", "period_end", "announce_date",
                   *field_map.values()]
        # infer_schema_length=None: 稀疏列(如仅个别行业有值的字段)前若干行可能全空,
        # 默认采样会推断成 Null 类型, 后续数值写入失败(与 fuyao provider 同修)。
        return pl.DataFrame(records, infer_schema_length=None).select(columns)

    @staticmethod
    def _to_equity_frame(rows: list[dict], latest_only: bool) -> pl.DataFrame:
        """股本变动事件 -> canonical shares 表。

        与 ``_to_frame`` 的关键差别: 这里 ``period_end`` 是**变动日**(END_DATE),
        ``announce_date`` 是公告日(NOTICE_DATE), 而**不是**报告期末。事件驱动比
        季度网格更贴合 ``share_capital.apply_historical_float_shares()`` 的 asof 回填:
        任意交易日都能落到「公告日不晚于该日」的最近一次变动, 不必被强行对齐到季度末
        (否则季中发生的转增/解禁要到下一季才生效, 回测会出现漂移)。

        可用日刻意取**公告日**而非变动日, 与该模块「历史日期使用公告日不晚于交易日的
        最新股本」的约定一致, 也与另四张财务表口径统一(都以公告日为准, 不引入前视)。
        代价是转增/解禁在「公告之后、生效之前」的几天里, 分子仍是变动前的成交量而
        分母已换成变动后的股本, 换手率会略微偏低; 该窗口通常只有数日, 可接受。

        ``latest_only`` 时每股只留最近一次变动 —— 增量同步用; 全量时保留最近
        ``_EQUITY_MAX_EVENTS`` 条(取最近, 不取最早, 保证 asof 起点不缺)。
        """
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            code = str(row.get("SECUCODE") or "").strip()
            if not code:
                continue
            by_symbol.setdefault(code, []).append(row)

        records: list[dict] = []
        for code, items in by_symbol.items():
            items.sort(key=lambda r: str(r.get("END_DATE") or ""), reverse=True)
            keep = items[:1] if latest_only else items[:_EQUITY_MAX_EVENTS]
            for row in keep:
                total = _to_float(row.get("TOTAL_SHARES"))
                float_shares = _to_float(row.get("LISTED_A_SHARES"))
                if float_shares is None:
                    limited = _to_float(row.get(_SHARES_LIMITED_SRC))
                    if total is not None and limited is not None and total == limited:
                        # 尚无任何可流通股份(上市前): 流通股本为 0, 而非"未知"
                        float_shares = 0.0
                records.append({
                    "symbol": code,
                    "period_end": _iso_date(row.get("END_DATE")),
                    "announce_date": _iso_date(row.get("NOTICE_DATE")),
                    "total_shares": total,
                    "float_shares": float_shares,
                })

        if not records:
            return pl.DataFrame()
        columns = ["symbol", "period_end", "announce_date",
                   "total_shares", "float_shares"]
        return pl.DataFrame(records, infer_schema_length=None).select(columns)

    def _get_shares(self, codes: list[str], latest_only: bool) -> pl.DataFrame:
        """股本变动历史(事件驱动)。

        用独立端点而非 MAINFINADATA, 原因见模块 docstring: 后者的历史股本有
        系统性错值, 会静默污染回测的 point-in-time 流通股本。
        """
        keys = (
            "SECUCODE", _EQUITY_DATE_FIELD, "NOTICE_DATE",
            *_SHARES_MAP.keys(), _SHARES_LIMITED_SRC,
        )
        rows = self._batch_fetch(
            _EQUITY_REPORT, codes, keys,
            date_field=_EQUITY_DATE_FIELD,
            rows_per_symbol=_EQUITY_ROWS_PER_SYMBOL,
            max_pages=_EQUITY_PAGES,
        )
        if not rows:
            return pl.DataFrame()
        return self._to_equity_frame(rows, latest_only)

    # ---------------------------------------------------------------- 对外契约
    def get_financials(
        self, table: str, symbols: list[str], latest_only: bool = True
    ) -> pl.DataFrame:
        """拉取一张财务表。table 取值见 services.financial_sync.FINANCIAL_TABLES。"""
        name = str(table or "").strip()
        if name not in _FIELD_MAPS:
            logger.warning("em_financial 不支持的财务表: %r", table)
            return pl.DataFrame()

        codes = _norm_symbols(symbols)
        if not codes:
            logger.warning("em_financial %s 跳过: 无有效标的", name)
            return pl.DataFrame()

        if name == "shares":
            return self._get_shares(codes, latest_only)

        periods = 1 if latest_only else _HISTORY_PERIODS
        # 多取一期做候选: 最近季度末可能尚未披露(如 10 月初查 09-30)
        dates = _quarter_ends(date.today(), periods + 1)
        keys = _COMMON_SRC + tuple(_FIELD_MAPS[name].keys())

        rows = self._fetch_chain(_REPORTS[name], codes, dates, keys, name)
        if not rows:
            return pl.DataFrame()
        return self._to_frame(name, rows, periods)

    # ---------------------------------------------------------------- 试拉
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」。financial 一个配置覆盖 5 张表, 这里逐表试拉并汇总。"""
        if dataset != "financial":
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": f"em_financial 只提供 financial 数据集; '{dataset}' 请继续使用原数据源",
            }

        probe = _norm_symbols(symbols) or ["600519.SH", "000001.SZ"]
        summary: dict[str, dict] = {}
        preview: list[dict] = []
        columns: list[str] = []
        total = 0
        errors: list[str] = []
        for table in _FIELD_MAPS:
            try:
                df = self.get_financials(table, probe, latest_only=False)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{table}: {e}")
                summary[table] = {"rows": 0, "symbols": 0}
                continue
            summary[table] = {
                "rows": df.height,
                "symbols": df["symbol"].n_unique() if not df.is_empty() else 0,
            }
            total += df.height
            if not columns and not df.is_empty():
                columns = df.columns
            if not preview and not df.is_empty():
                preview = df.head(5).to_dicts()

        out: dict[str, Any] = {
            "provider": self.name,
            "dataset": "financial",
            "rows": total,
            "columns": columns,
            "preview": preview,
            "tables": summary,
            "symbols": probe,
        }
        if errors:
            out["error"] = "; ".join(errors)
        return out
