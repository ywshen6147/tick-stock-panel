"""信息面扩展表预设 — 补齐 TSP 完全空白的 7 层。

来源评估: `a-stock-data` 15 层对比后，TSP 有 7 层「基本空白且可直接补强」:
  研报 / 新闻 / 公告 / ETF 期权 / 舆情互动 / 宏观与利率 / 可转债。

落地方案走 TSP 自带的扩展数据表 (ext-data) 契约 —— 不改核心存储、展示、
回补链路，只在本模块做「公开 HTTP 接口 → 本地 schema」的转换。
`apps/services/ext_presets.py` 负责把它们注册进 `_presets()` 与拉取分发。

## 三条硬约定 (与 ext_data 的存储语义对齐)

1. **首列 = 该表天然唯一键**。snapshot 模式写入时按 `df.columns[0]` 去重
   (`write_ext_parquet`)，所以首列必须唯一，否则同一批数据会被静默折叠掉。
   其余列是展示/消费字段。
2. **市场级表用 `market_level=True`**（宏观/新闻/公告/期权/转债 —— 无 symbol 列，
   不做标的关联）；**标的级表用 `symbol_map`**（研报/舆情 —— 可被 ext 因子 JOIN）。
3. **不自动拉取**：`pull.enabled=False`，与内置概念/行业预设一致，由用户在
   扩展数据页手动触发 `POST /api/ext-data/presets/{id}/fetch`。

## 数据源 (全部零鉴权、2026-10-11 本机实测通过)

  研报     reportapi.eastmoney.com/report/list
  新闻     财联社电报 cls.cn (v1 + 本地签名) / 东财 7×24 np-weblist
  公告     巨潮 cninfo.com.cn/new/hisAnnouncement/query
  期权     新浪 hq.sinajs.cn (GBK, 合约清单 + T型报价 + 希腊字母)
  舆情     同花顺热榜 dq.10jqka.com.cn / 东财人气榜 emappdata
  宏观     中债收益率 yield.chinabond.com.cn (HTML) / 货币网定盘利率 CSV /
           东财数据中心 RPTA_WEB_RATE + RPT_ECONOMY_* / 华尔街见闻宏观日历
  可转债   东财数据中心 RPT_BOND_CB_LIST

未覆盖项 (数据源确实没有，不猜): 社会融资规模增量 —— 东财无对应报表，
人民银行口径需抓 xls (本机 venv 无 openpyxl)。社融的公布日程会出现在
「全球宏观日历」表里，数值本身暂缺。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.services.ext_data import ExtConfig, ExtField, PullConfig

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))

# 这些源要求浏览器 UA + 特定 Referer，缺了会 403 / 400。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_REPORT = "https://reportapi.eastmoney.com/report/list"
_EM_FASTNEWS = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
_CLS_ROLL = "https://www.cls.cn/v1/roll/get_roll_list"
_CNINFO_ANN = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_SINA_HQ = "https://hq.sinajs.cn/list="
_SINA_OPT_NAME = ("https://stock.finance.sina.com.cn/futures/api/openapi.php/"
                  "StockOptionService.getStockName")
_THS_HOT = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
_EM_HOT = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
_EM_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_CHINABOND = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
_CHINAMONEY = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/currency/{name}-chrt.csv"
_WSCN_CAL = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"

# 东财风控: 串行 + 小睡，避免触发限流 (社区实测 >5 req/s 或 1min >200 次会临时封 IP)
_EM_SLEEP = 0.35


def _today() -> date:
    return datetime.now(_CN_TZ).date()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _req_headers(extra: dict[str, str]) -> dict[str, str]:
    """出站头: 保留 tsp 标识 + 叠加本模块的浏览器 UA/Referer。"""
    from app.services.ext_pull import outbound_headers

    return outbound_headers({"User-Agent": _UA, **extra})


async def _get(client, url: str, *, headers: dict[str, str] | None = None,
               params: dict[str, Any] | None = None, timeout: float = 30.0):
    resp = await client.get(url, headers=_req_headers(headers or {}),
                            params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


async def _post(client, url: str, *, headers: dict[str, str] | None = None,
                data: dict[str, Any] | None = None, json_body: Any = None,
                params: dict[str, Any] | None = None, timeout: float = 30.0):
    resp = await client.post(url, headers=_req_headers(headers or {}),
                             data=data, json=json_body, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def _num(value: Any) -> float | None:
    """尽力转 float; 空串 / 非数值 → None (绝不 coerce 成 0)。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.casefold() in {"-", "none", "null", "nan", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    f = _num(value)
    return None if f is None else int(f)


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"none", "null", "nan"} else text


def _day(value: Any) -> str:
    """任意日期串 → YYYY-MM-DD; 失败返回空串。"""
    text = _text(value)
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    m = re.match(r"^(\d{4})\)?[-/年]?(\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return ""


def _ms_to_min(ts: Any) -> str:
    f = _num(ts)
    if f is None or f <= 0:
        return ""
    return datetime.fromtimestamp(f / 1000, _CN_TZ).strftime("%Y-%m-%d %H:%M")


def _attr_list(value: Any, key: str) -> str:
    """list[dict] 取某字段并按分号拼接; 兼容已是字符串/单值的情况。"""
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                label = _text(item.get(key) or item.get("name") or item.get("Name"))
            else:
                label = _text(item)
            if label:
                out.append(label)
        return ";".join(out)
    return _text(value)


# ---------------------------------------------------------------------------
# L2 研报层 — 东财研报库 (市场级拉取 → 按个股聚合)
# ---------------------------------------------------------------------------

def _report_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_report_em",
        label="研报覆盖(东财)",
        mode="snapshot",
        fields=[
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("研报数", "int", "近 90 天研报数"),
            ExtField("覆盖机构数", "int", "近 90 天覆盖机构数"),
            ExtField("最新发布日期", "string", "最新研报日期"),
            ExtField("最新评级", "string", "最新评级"),
            ExtField("最新机构", "string", "最新研报机构"),
            ExtField("最新标题", "string", "最新研报标题"),
            ExtField("今年EPS预测", "float", "今年一致预期 EPS(机构均值)"),
            ExtField("明年EPS预测", "float", "明年一致预期 EPS(机构均值)"),
            ExtField("后年EPS预测", "float", "后年一致预期 EPS(机构均值)"),
            ExtField("所属行业", "string", "东财行业"),
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
        ],
        description=("东财研报库近 90 天市场级拉取后按个股聚合: 覆盖度 / 最新评级 / "
                     "机构一致预期 EPS。可作 ext 因子 JOIN 到 enriched 帧。"),
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(url=_EM_REPORT, method="GET", schedule_minutes=1440, enabled=False),
    )


async def _fetch_report(config: ExtConfig, data_dir: Path) -> int:
    end = _today()
    begin = end - timedelta(days=90)
    raw: list[dict] = []
    async with _httpx_client() as client:
        # 90 天窗口约 2500 篇 (pageSize=100 → 25 页), 上限给到 30 页留余量
        page, max_pages = 1, 30
        while page <= max_pages:
            params = {
                "industryCode": "*", "pageSize": "100", "industry": "*", "rating": "*",
                "ratingChange": "*", "beginTime": begin.isoformat(), "endTime": end.isoformat(),
                "pageNo": str(page), "fields": "", "qType": "0", "orgCode": "", "code": "",
                "rcode": "", "p": str(page), "pageNum": str(page), "pageNumber": str(page),
            }
            resp = await _get(client, _EM_REPORT, params=params,
                              headers={"Referer": "https://data.eastmoney.com/"})
            payload = resp.json()
            rows = payload.get("data") or []
            if not rows:
                break
            raw.extend(rows)
            if page >= int(payload.get("TotalPage") or 1):
                break
            page += 1
            await asyncio.sleep(_EM_SLEEP)

    agg: dict[str, dict] = {}
    for rec in raw:
        code = _text(rec.get("stockCode"))
        if not code:
            continue
        bucket = agg.setdefault(code, {"n": 0, "orgs": set(), "items": [], "eps": {}, "name": "",
                                       "indu": ""})
        bucket["n"] += 1
        org = _text(rec.get("orgSName"))
        if org:
            bucket["orgs"].add(org)
        bucket["items"].append((_text(str(rec.get("publishDate") or ""))[:10], rec))
        for key, field in (("this", "predictThisYearEps"), ("next", "predictNextYearEps"),
                           ("next2", "predictNextTwoYearEps")):
            value = _num(rec.get(field))
            if value is not None:
                bucket["eps"].setdefault(key, []).append(value)
        bucket["name"] = bucket["name"] or _text(rec.get("stockName"))
        bucket["indu"] = bucket["indu"] or _text(rec.get("indvInduName"))

    out: list[dict] = []
    for code, bucket in agg.items():
        bucket["items"].sort(key=lambda t: t[0], reverse=True)
        pub, latest = bucket["items"][0]
        eps = {k: (sum(v) / len(v) if v else None) for k, v in bucket["eps"].items()}
        out.append({
            "股票代码": code,
            "股票简称": bucket["name"],
            "研报数": bucket["n"],
            "覆盖机构数": len(bucket["orgs"]),
            "最新发布日期": pub,
            "最新评级": _text(latest.get("emRatingName")) or _text(latest.get("sRatingName")),
            "最新机构": _text(latest.get("orgSName")),
            "最新标题": _text(latest.get("title")),
            "今年EPS预测": eps.get("this"),
            "明年EPS预测": eps.get("next"),
            "后年EPS预测": eps.get("next2"),
            "所属行业": bucket["indu"],
        })
    if not out:
        raise ValueError("东财研报接口近 90 天返回 0 条")
    logger.info("研报覆盖: 原始 %d 篇 → 聚合 %d 只", len(raw), len(out))
    return _write(out, config, data_dir)


def _httpx_client():
    import httpx

    return httpx.AsyncClient(timeout=30.0, follow_redirects=True)


def _write(rows: list[dict], config: ExtConfig, data_dir: Path) -> int:
    """行 → parquet (含 symbol 映射与 14 列/字段定型)。

    不复用 `rows_to_parquet`: 它走 `pl.DataFrame(rows)` 的默认
    `infer_schema_length=100`，长表 (宏观日历 200+ 行) 若某数值列前 100 行全为
    null 会被推断成 Null 类型，第 101 行冒出数值即抛 ComputeError。
    这里显式 `infer_schema_length=None` (全量推断) 并对齐 config.fields 的类型。
    """
    import polars as pl

    from app.services.ext_data import apply_config_mapping, write_ext_parquet

    dtype_map = {"string": pl.Utf8, "int": pl.Int64, "float": pl.Float64, "bool": pl.Boolean}
    overrides = {"symbol": pl.Utf8, "code": pl.Utf8}
    for f in config.fields:
        overrides.setdefault(f.name, dtype_map.get(f.dtype, pl.Utf8))
    df = pl.DataFrame(rows, schema_overrides=overrides, infer_schema_length=None)
    df = apply_config_mapping(df, config, data_dir)
    if "symbol" in df.columns:
        df = df.with_columns(pl.col("symbol").cast(pl.Utf8))
    return write_ext_parquet(df, config, data_dir, snapshot_date=_today())


# ---------------------------------------------------------------------------
# L5 新闻层 — 财联社电报 + 东财 7×24
# ---------------------------------------------------------------------------

def _news_cls_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_news_cls",
        label="财联社电报",
        mode="snapshot",
        market_level=True,
        fields=[
            ExtField("快讯", "string", "快讯(时间+标题)"),
            ExtField("时间", "string", "发布时间"),
            ExtField("标题", "string", "标题"),
            ExtField("摘要", "string", "摘要"),
            ExtField("正文", "string", "正文"),
            ExtField("重要度", "string", "重要度(A/B/C)"),
            ExtField("阅读数", "int", "阅读数"),
            ExtField("关联股票", "string", "关联股票"),
            ExtField("标签", "string", "标签"),
        ],
        description="财联社电报(全市场快讯, 零鉴权)。反映盘面催化与题材热度。",
        pull=PullConfig(url=_CLS_ROLL, method="GET", schedule_minutes=1440, enabled=False),
    )


def _cls_sign(params: dict[str, str]) -> str:
    """财联社 v1 签名: md5(sha1(sorted_query_string))。"""
    qs = urllib.parse.urlencode(sorted(params.items()))
    return hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()


async def _fetch_news_cls(config: ExtConfig, data_dir: Path) -> int:
    params = {"app": "CailianpressWeb", "category": "", "last_time": "", "os": "web",
              "refresh_type": "1", "rn": "50", "sv": "7.7.5"}   # rn 上限 50 (传 100 返回空)
    async with _httpx_client() as client:
        payload: dict = {}
        for attempt in range(3):
            qs = urllib.parse.urlencode(sorted(params.items()))
            url = f"{_CLS_ROLL}?{qs}&sign={_cls_sign(params)}"
            resp = await _get(client, url, headers={"Referer": "https://www.cls.cn/"})
            payload = resp.json()
            if payload.get("errno") == 0:
                break
            await asyncio.sleep(1.0 + attempt)
    items = (payload.get("data") or {}).get("roll_data") or []
    if not items:
        raise ValueError(f"财联社电报返回 0 条 (errno={payload.get('errno')} msg={payload.get('msg')})")

    out: list[dict] = []
    for it in items:
        ts = _int(it.get("ctime") or it.get("modified_time"))
        when = datetime.fromtimestamp(ts, _CN_TZ).strftime("%Y-%m-%d %H:%M") if ts else ""
        title = _text(it.get("title"))
        brief = _text(it.get("brief"))
        content = _text(it.get("content"))
        key = f"{when} {title or brief[:40]}".strip()
        if not key:
            continue
        out.append({
            "快讯": key,
            "时间": when,
            "标题": title,
            "摘要": brief or content[:200],
            "正文": content,
            "重要度": _text(it.get("level")),
            "阅读数": _int(it.get("reading_num")),
            "关联股票": _attr_list(it.get("stock_list"), "name"),
            "标签": _attr_list(it.get("tags"), "name"),
        })
    logger.info("财联社电报: %d 条", len(out))
    return _write(out, config, data_dir)


def _news_724_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_news_724",
        label="东财7×24快讯",
        mode="snapshot",
        market_level=True,
        fields=[
            ExtField("快讯", "string", "快讯(时间+标题)"),
            ExtField("时间", "string", "发布时间"),
            ExtField("标题", "string", "标题"),
            ExtField("摘要", "string", "摘要"),
            ExtField("快讯ID", "string", "快讯 ID"),
            ExtField("关联板块", "string", "关联板块"),
            ExtField("评论数", "int", "评论数"),
        ],
        description="东方财富 7×24 全球快讯(零鉴权)。",
        pull=PullConfig(url=_EM_FASTNEWS, method="GET", schedule_minutes=1440, enabled=False),
    )


async def _fetch_news_724(config: ExtConfig, data_dir: Path) -> int:
    params = {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "",
              "pageSize": "100", "req_trace": str(int(time.time() * 1000))}
    async with _httpx_client() as client:
        resp = await _get(client, _EM_FASTNEWS, params=params,
                          headers={"Referer": "https://kuaixun.eastmoney.com/"})
        payload = resp.json()
    items = (payload.get("data") or {}).get("fastNewsList") or []
    if not items:
        raise ValueError("东财 7×24 快讯返回 0 条")

    out: list[dict] = []
    for it in items:
        when = _text(it.get("showTime"))
        title = _text(it.get("title"))
        summary = _text(it.get("summary"))
        key = f"{when} {title or summary[:40]}".strip()
        if not key:
            continue
        out.append({
            "快讯": key,
            "时间": when,
            "标题": title,
            "摘要": summary,
            "快讯ID": _text(it.get("code")),
            "关联板块": _text(it.get("stockList")),
            "评论数": _int(it.get("pinglun_Num")),
        })
    logger.info("东财 7×24 快讯: %d 条", len(out))
    return _write(out, config, data_dir)


# ---------------------------------------------------------------------------
# L7 公告层 — 巨潮全市场公告
# ---------------------------------------------------------------------------

def _annt_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_annt_cninfo",
        label="全市场公告(巨潮)",
        mode="snapshot",
        market_level=True,
        fields=[
            ExtField("公告", "string", "公告(代码+标题)"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("标题", "string", "公告标题"),
            ExtField("公告时间", "string", "公告时间"),
            ExtField("公告类型", "string", "公告类型"),
            ExtField("公告ID", "string", "公告 ID"),
            ExtField("PDF链接", "string", "PDF 链接"),
            ExtField("详情链接", "string", "详情页链接"),
        ],
        description=("巨潮资讯网近 2 日全市场公告(沪深北)。接口单页上限 30 条，"
                     "本表抓前 20 页 (约 600 条)，按公告时间倒序。"),
        pull=PullConfig(url=_CNINFO_ANN, method="POST", schedule_minutes=1440, enabled=False),
    )


async def _fetch_annt(config: ExtConfig, data_dir: Path) -> int:
    end = _today()
    begin = end - timedelta(days=2)
    headers = {"Referer": "https://www.cninfo.com.cn/new/disclosure",
               "Origin": "https://www.cninfo.com.cn",
               "X-Requested-With": "XMLHttpRequest"}
    out: list[dict] = []
    async with _httpx_client() as client:
        for page in range(1, 21):  # 单页上限 30 条 (实测 pageSize 传 100 也只回 30)
            form = {"pageNum": str(page), "pageSize": "30", "column": "szse",
                    "tabName": "fulltext", "plate": "", "stock": "", "searchkey": "",
                    "secid": "", "category": "", "trade": "",
                    "seDate": f"{begin.isoformat()}~{end.isoformat()}",
                    "sortName": "", "sortType": "", "isHLtitle": "true"}
            resp = await _post(client, _CNINFO_ANN, data=form, headers=headers)
            payload = resp.json()
            rows = payload.get("announcements") or []
            if not rows:
                break
            for rec in rows:
                code = _text(rec.get("secCode"))
                title = _text(rec.get("announcementTitle"))
                key = f"{code} {title}".strip()
                if not key:
                    continue
                adjunct = _text(rec.get("adjunctUrl"))
                ann_id = _text(rec.get("announcementId"))
                out.append({
                    "公告": key,
                    "股票代码": code,
                    "股票简称": _text(rec.get("secName")),
                    "标题": title,
                    "公告时间": _ms_to_min(rec.get("announcementTime")),
                    "公告类型": _text(rec.get("announcementTypeName")),
                    "公告ID": ann_id,
                    "PDF链接": f"https://static.cninfo.com.cn/{adjunct}" if adjunct else "",
                    "详情链接": (f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={ann_id}"
                                 if ann_id else ""),
                })
            if not payload.get("hasMore"):
                break
            await asyncio.sleep(_EM_SLEEP)
    if not out:
        raise ValueError("巨潮公告接口返回 0 条")
    logger.info("全市场公告: %d 条 (%s ~ %s)", len(out), begin, end)
    return _write(out, config, data_dir)


# ---------------------------------------------------------------------------
# L9 ETF 期权层 — 新浪合约清单 + T型报价 + 希腊字母
# ---------------------------------------------------------------------------

_SINA_VAR_RE = re.compile(r'var hq_str_([A-Za-z0-9_]+)="(.*?)";')

_OPT_UNDERLYINGS = (("510050", "50ETF"), ("510300", "300ETF"),
                    ("510500", "500ETF"), ("588000", "科创50ETF"))


def _opt_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_opt_etf",
        label="ETF期权(T型+希腊)",
        mode="snapshot",
        market_level=True,
        fields=[
            ExtField("合约代码", "string", "合约代码"),
            ExtField("合约名称", "string", "合约名称"),
            ExtField("标的ETF", "string", "标的 ETF"),
            ExtField("方向", "string", "认购 / 认沽"),
            ExtField("到期月", "string", "到期月"),
            ExtField("行权价", "float", "行权价"),
            ExtField("最新价", "float", "最新价"),
            ExtField("买一价", "float", "买一价"),
            ExtField("卖一价", "float", "卖一价"),
            ExtField("买量", "int", "买一量(张)"),
            ExtField("卖量", "int", "卖一量(张)"),
            ExtField("持仓量", "int", "持仓量(张)"),
            ExtField("涨跌幅", "float", "涨跌幅 %"),
            ExtField("成交量", "int", "成交量(张)"),
            ExtField("成交额", "float", "成交额(元)"),
            ExtField("Delta", "float", "Delta"),
            ExtField("Gamma", "float", "Gamma"),
            ExtField("Theta", "float", "Theta"),
            ExtField("Vega", "float", "Vega"),
            ExtField("隐含波动率", "float", "隐含波动率 %"),
        ],
        description=("4 只 ETF 期权(50/300/500/科创50)各挂牌月份的全部活跃合约: T型报价 + "
                     "希腊字母 + IV。新浪源 GBK，零鉴权。"),
        pull=PullConfig(url=_SINA_OPT_NAME, method="GET", schedule_minutes=1440, enabled=False),
    )


def _sina_vars(text: str) -> dict[str, list[str]]:
    """解析 hq.sinajs.cn 多行响应: {变量名: [字段...]}。"""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = _SINA_VAR_RE.search(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        out[name] = [p.strip() for p in value.split(",")] if value else []
    return out


async def _fetch_opt(config: ExtConfig, data_dir: Path) -> int:
    hdr = {"Referer": "https://stock.finance.sina.com.cn/"}
    codes: dict[str, dict] = {}       # 合约代码 → 元信息
    async with _httpx_client() as client:
        for underlying, cate in _OPT_UNDERLYINGS:
            try:
                resp = await _get(client, _SINA_OPT_NAME, headers=hdr,
                                  params={"exchange": "null", "cate": cate})
                months = resp.json()["result"]["data"]["contractMonth"]
            except Exception as e:  # noqa: BLE001
                logger.warning("期权月份获取失败 %s: %s", cate, e)
                continue
            # 全挂牌月份 (新浪实测每月给出 11~16 个活跃合约, 即真实可交易档位)
            month_codes = [m.replace("-", "")[2:] for m in months[1:]][:6]
            for month in month_codes:
                for call, flag, label in ((True, "OP_UP_", "认购"), (False, "OP_DOWN_", "认沽")):
                    try:
                        resp = await _get(client, f"{_SINA_HQ}{flag}{underlying}{month}",
                                          headers=hdr)
                        resp.encoding = "gbk"
                        var = _sina_vars(resp.text).get(f"{flag}{underlying}{month}") or []
                    except Exception as e:  # noqa: BLE001
                        logger.warning("期权合约清单失败 %s%s: %s", flag, underlying, e)
                        continue
                    for token in var:
                        if not token.startswith("CON_OP_"):
                            continue
                        code = token.replace("CON_OP_", "")
                        codes[code] = {"标的ETF": underlying, "方向": label, "到期月": month}
                    await asyncio.sleep(0.15)

        if not codes:
            raise ValueError("新浪期权接口未返回任何合约")

        # 批量取 T型报价 + 希腊字母 (每个请求最多 60 个代码, 避免 URL 过长)
        quote: dict[str, list[str]] = {}
        greek: dict[str, list[str]] = {}
        all_codes = sorted(codes)
        for i in range(0, len(all_codes), 60):
            chunk = all_codes[i:i + 60]
            for prefix, sink in (("CON_OP_", quote), ("CON_SO_", greek)):
                param = ",".join(f"{prefix}{c}" for c in chunk)
                try:
                    resp = await _get(client, f"{_SINA_HQ}{param}", headers=hdr)
                    resp.encoding = "gbk"
                    sink.update(_sina_vars(resp.text))
                except Exception as e:  # noqa: BLE001
                    logger.warning("期权批量取值失败 (%s, %d 个): %s", prefix, len(chunk), e)
                await asyncio.sleep(0.2)

    out: list[dict] = []
    for code, meta in codes.items():
        v = quote.get(f"CON_OP_{code}") or []
        if len(v) < 43:
            continue
        g_raw = greek.get(f"CON_SO_{code}") or []
        # 希腊字母解析: raw[1:4] 是 3 个空串，必须跳过，否则 Delta/IV 全错位
        g = ([g_raw[0]] + g_raw[4:]) if len(g_raw) >= 16 else []
        out.append({
            "合约代码": code,
            "合约名称": _text(v[37]),
            "标的ETF": meta["标的ETF"],
            "方向": meta["方向"],
            "到期月": meta["到期月"],
            "行权价": _num(v[7]),
            "最新价": _num(v[2]),
            "买一价": _num(v[1]),
            "卖一价": _num(v[3]),
            "买量": _int(v[0]),
            "卖量": _int(v[4]),
            "持仓量": _int(v[5]),
            "涨跌幅": _num(v[6]),
            "成交量": _int(v[41]),
            "成交额": _num(v[42]),
            "Delta": _num(g[2]) if len(g) > 2 else None,
            "Gamma": _num(g[3]) if len(g) > 3 else None,
            "Theta": _num(g[4]) if len(g) > 4 else None,
            "Vega": _num(g[5]) if len(g) > 5 else None,
            "隐含波动率": (round(_num(g[6]) * 100, 4) if len(g) > 6 and _num(g[6]) is not None
                        else None),
        })
    if not out:
        raise ValueError("新浪期权接口未返回可解析的合约报价")
    logger.info("ETF 期权: %d 个合约", len(out))
    return _write(out, config, data_dir)


# ---------------------------------------------------------------------------
# L10 舆情互动层 — 同花顺热榜 + 东财人气榜
# ---------------------------------------------------------------------------

def _hot_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_hot_rank",
        label="舆情热度榜(同花顺+东财)",
        mode="snapshot",
        fields=[
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("同花顺排名", "int", "同花顺热榜排名"),
            ExtField("同花顺人气值", "float", "同花顺人气值"),
            ExtField("涨跌幅", "float", "涨跌幅 %"),
            ExtField("排名变化", "int", "排名变化"),
            ExtField("人气标签", "string", "人气标签"),
            ExtField("概念标签", "string", "概念标签"),
            ExtField("东财人气排名", "int", "东财人气榜排名"),
            ExtField("东财排名变化", "int", "东财人气榜排名变化"),
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
        ],
        description=("同花顺热榜(hour, 100 只) + 东财人气榜(100 只) 合并到个股维度: "
                     "热度排名 / 人气值 / 概念标签 / 排名变化。可作热度因子 JOIN。"),
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(url=_THS_HOT, method="GET", schedule_minutes=1440, enabled=False),
    )


async def _fetch_hot(config: ExtConfig, data_dir: Path) -> int:
    merged: dict[str, dict] = {}
    async with _httpx_client() as client:
        # 同花顺热榜: 缺 stock_type/list_type 会返回 Param Error
        resp = await _get(client, _THS_HOT,
                          params={"stock_type": "a", "type": "hour", "list_type": "normal"},
                          headers={"Referer": "https://dq.10jqka.com.cn/"})
        ths_items = ((resp.json().get("data") or {}).get("stock_list")) or []
        for it in ths_items:
            code = _text(it.get("code"))
            if not code:
                continue
            row = merged.setdefault(code, {"name": "", "ths_rank": None, "heat": None,
                                           "pct": None, "chg": None, "pop": "", "concepts": "",
                                           "em_rank": None, "em_chg": None})
            row["name"] = row["name"] or _text(it.get("name"))
            row["ths_rank"] = _int(it.get("order"))
            row["heat"] = _num(it.get("rate"))
            row["pct"] = _num(it.get("rise_and_fall"))
            row["chg"] = _int(it.get("hot_rank_chg"))
            tag = it.get("tag") or {}
            if isinstance(tag, dict):
                row["pop"] = _attr_list(tag.get("popularity_tag"), "name")
                row["concepts"] = _attr_list(tag.get("concept_tag"), "name")

        await asyncio.sleep(_EM_SLEEP)
        # 东财人气榜
        resp = await _post(client, _EM_HOT, json_body={
            "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
            "marketType": "", "pageNo": 1, "pageSize": 100,
        }, headers={"Referer": "https://vipmoney.eastmoney.com/"})
        em_rows = resp.json().get("data") or []
        for it in em_rows:
            sc = _text(it.get("sc"))          # 形如 SH600418
            code = sc[2:] if len(sc) > 2 else sc
            if not code:
                continue
            row = merged.setdefault(code, {"name": "", "ths_rank": None, "heat": None,
                                           "pct": None, "chg": None, "pop": "", "concepts": "",
                                           "em_rank": None, "em_chg": None})
            row["em_rank"] = _int(it.get("rk"))
            row["em_chg"] = _int(it.get("hisRc"))

        # 补名称/价格: 人气榜只给带前缀代码
        missing = [c for c, r in merged.items() if not r["name"]]
        if missing:
            secids = []
            for code in missing:
                prefix = "1" if code.startswith(("5", "6", "9")) else "0"
                secids.append(f"{prefix}.{code}")
            try:
                resp = await _get(client, _EM_ULIST, params={
                    "fltt": "2", "invt": "2", "np": "1", "secids": ",".join(secids),
                    "fields": "f12,f14,f2,f3", "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                }, headers={"Referer": "https://quote.eastmoney.com/"})
                diff = ((resp.json().get("data") or {}).get("diff")) or []
                if isinstance(diff, dict):
                    diff = list(diff.values())
                for it in diff:
                    code = _text(it.get("f12"))
                    if code in merged:
                        merged[code]["name"] = merged[code]["name"] or _text(it.get("f14"))
            except Exception as e:  # noqa: BLE001
                logger.warning("人气榜名称补齐失败(不影响主数据): %s", e)

    out: list[dict] = []
    for code, row in merged.items():
        if not (row["ths_rank"] or row["em_rank"]):
            continue
        out.append({
            "股票代码": code,
            "股票简称": row["name"],
            "同花顺排名": row["ths_rank"],
            "同花顺人气值": row["heat"],
            "涨跌幅": row["pct"],
            "排名变化": row["chg"],
            "人气标签": row["pop"],
            "概念标签": row["concepts"],
            "东财人气排名": row["em_rank"],
            "东财排名变化": row["em_chg"],
        })
    if not out:
        raise ValueError("同花顺热榜与东财人气榜均返回 0 条")
    logger.info("舆情热度榜: %d 只", len(out))
    return _write(out, config, data_dir)


# ---------------------------------------------------------------------------
# L11 宏观与利率层 — 中债曲线 + 定盘利率 + LPR / 东财经济数据 / 宏观日历
# ---------------------------------------------------------------------------

# 中债返回的曲线名 → 本地列名前缀。匹配用子串 (全名见 skill:
# 「中债国债收益率曲线」/「中债商业银行普通债收益率曲线(AAA)」/「中债中短期票据收益率曲线(AAA)」)
_CB_CURVES = (("国债", "国债"), ("商业银行", "银行AAA"), ("中短期票据", "中短票AAA"))
_CB_TENORS = ["3m", "6m", "1y", "3y", "5y", "7y", "10y", "30y"]
_CB_TENOR_LABEL = {"3m": "3月", "6m": "6月", "1y": "1年", "3y": "3年",
                   "5y": "5年", "7y": "7年", "10y": "10年", "30y": "30年"}
_CB_HEADER = ["曲线名称", "日期", "3月", "6月", "1年", "3年", "5年", "7年", "10年", "30年"]


def _macro_curve_preset() -> ExtConfig:
    names = ["日期"]
    names += [f"国债_{_CB_TENOR_LABEL[t]}" for t in _CB_TENORS]
    names += [f"银行AAA_{_CB_TENOR_LABEL[t]}" for t in ("1y", "3y", "5y", "10y")]
    names += [f"中短票AAA_{_CB_TENOR_LABEL[t]}" for t in ("1y", "3y", "5y", "10y")]
    names += ["FR001", "FR007", "FR014", "FDR001", "FDR007", "FDR014", "LPR_1年", "LPR_5年"]
    return ExtConfig(
        id="ext_macro_curve",
        label="利率曲线与资金面",
        mode="snapshot",
        market_level=True,
        fields=[ExtField("日期", "string", "日期")]
        + [ExtField(n, "float", n) for n in names[1:]],
        description=("中债收益率曲线(国债 8 档 / 银行 AAA 4 档 / 中短票 AAA 4 档) + "
                     "银行间回购定盘利率(FR/FDR) + LPR，近 90 天，单位 %。"),
        pull=PullConfig(url=_CHINABOND, method="GET", schedule_minutes=1440, enabled=False),
    )


async def _fetch_macro_curve(config: ExtConfig, data_dir: Path) -> int:
    end = _today()
    begin = end - timedelta(days=90)
    table: dict[str, dict] = {}

    async with _httpx_client() as client:
        # 1) 中债收益率曲线 (HTML 表格; 一次性取全曲线 ycqx)
        resp = await _get(client, _CHINABOND, params={
            "startDate": begin.isoformat(), "endDate": end.isoformat(),
            "gjqx": 0, "qxId": "ycqx", "locale": "cn_ZH",
        }, headers={"Referer": "https://yield.chinabond.com.cn/"})
        text = re.sub(r"<!--.*?-->", "", resp.content.decode("utf-8", "replace"), flags=re.S)
        tables = text.split("<table")
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[-1], re.S) if len(tables) > 2 else []
        cells = [[re.sub(r"<[^>]+>|\s+", "", c)
                  for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)] for tr in trs]
        if not cells or cells[0] != _CB_HEADER:
            raise ValueError(f"中债收益率页面表头不符合预期: {cells[0] if cells else None}")
        for rec in cells[1:]:
            if len(rec) != len(_CB_HEADER):
                continue
            # 曲线名称形如「中债国债收益率曲线」/「中债商业银行普通债收益率曲线(AAA)」
            curve = next((label for token, label in _CB_CURVES if token in rec[0]), "")
            if not curve:
                continue
            day = rec[1]
            row = table.setdefault(day, {"日期": day})
            for tenor, value in zip(_CB_TENORS, rec[2:]):
                if curve == "国债" or tenor in ("1y", "3y", "5y", "10y"):
                    row[f"{curve}_{_CB_TENOR_LABEL[tenor]}"] = _num(value)

        await asyncio.sleep(_EM_SLEEP)

        # 2) 回购定盘利率 FR / FDR (货币网 CSV)
        for kind, name in (("FR", "frr"), ("FDR", "fdr")):
            try:
                resp = await _get(client, _CHINAMONEY.format(name=name),
                                  headers={"Referer": "https://www.chinamoney.com.cn/chinese/bkfrr/"})
                for line in resp.content.decode("utf-8-sig").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(",")
                    if len(parts) != 9:
                        continue
                    day = parts[0].strip()
                    if not (begin.isoformat() <= day <= end.isoformat()):
                        continue
                    row = table.setdefault(day, {"日期": day})
                    for idx, tenor in ((6, "001"), (7, "007"), (8, "014")):
                        row[f"{kind}{tenor}"] = _num(parts[idx])
            except Exception as e:  # noqa: BLE001
                logger.warning("回购定盘利率 %s 拉取失败(跳过): %s", kind, e)
            await asyncio.sleep(_EM_SLEEP)

        # 3) LPR (东财数据中心)
        try:
            payload = await _fetch_dc(client, "RPTA_WEB_RATE",
                                      columns="TRADE_DATE,LPR1Y,LPR5Y",
                                      sort="TRADE_DATE", order="-1", page_size=40)
            for rec in payload:
                day = _text(str(rec.get("TRADE_DATE") or ""))[:10]
                if not day or not (begin.isoformat() <= day <= end.isoformat()):
                    continue
                row = table.setdefault(day, {"日期": day})
                row["LPR_1年"] = _num(rec.get("LPR1Y"))
                row["LPR_5年"] = _num(rec.get("LPR5Y"))
        except Exception as e:  # noqa: BLE001
            logger.warning("LPR 拉取失败(跳过): %s", e)

    out = [table[k] for k in sorted(table, reverse=True)]
    if not out:
        raise ValueError("利率曲线三路数据源全部为空")
    logger.info("利率曲线与资金面: %d 个交易日", len(out))
    return _write(out, config, data_dir)


async def _fetch_dc(client, report_name: str, *, columns: str = "ALL",
                    sort: str | None = None, order: str = "-1",
                    page_size: int = 100, max_pages: int = 12,
                    extra: dict[str, Any] | None = None) -> list[dict]:
    """东财数据中心统一查询 (分页拼装)。"""
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"reportName": report_name, "columns": columns, "pageSize": str(page_size),
                  "pageNumber": str(page), "source": "WEB", "client": "WEB"}
        if sort:
            params["sortColumns"] = sort
            params["sortTypes"] = order
        if extra:
            params.update({k: str(v) for k, v in extra.items()})
        resp = await _get(client, _EM_DC, params=params,
                          headers={"Referer": "https://data.eastmoney.com/"})
        payload = resp.json()
        if payload.get("code") != 0:
            raise ValueError(f"东财报表 {report_name}: {payload.get('message')}")
        result = payload.get("result") or {}
        data = result.get("data") or []
        rows.extend(data)
        if page >= int(result.get("pages") or 1) or not data:
            break
        await asyncio.sleep(_EM_SLEEP)
    return rows


_ECON_SPECS = (
    # (报表, 期间字段, [(列名, 源字段, 说明)])
    ("RPT_ECONOMY_PMI", "REPORT_DATE", [("制造业PMI", "MAKE_INDEX"), ("非制造业PMI", "NMAKE_INDEX")]),
    ("RPT_ECONOMY_CPI", "REPORT_DATE", [("CPI同比", "NATIONAL_SAME"), ("CPI环比", "NATIONAL_SEQUENTIAL")]),
    ("RPT_ECONOMY_PPI", "REPORT_DATE", [("PPI同比", "BASE_SAME")]),
    ("RPT_ECONOMY_GDP", "REPORT_DATE", [("GDP同比", "SUM_SAME"), ("GDP现价_亿元", "DOMESTICL_PRODUCT_BASE")]),
    ("RPT_ECONOMY_TOTAL_RETAIL", "REPORT_DATE",
     [("社零同比", "RETAIL_TOTAL_SAME"), ("社零_亿元", "RETAIL_TOTAL"), ("社零累计同比", "RETAIL_ACCUMULATE_SAME")]),
    ("RPT_ECONOMY_DEPOSIT_RESERVE", "MONTH_DATE",
     [("存准率_大型", "INTEREST_RATE_BB"), ("存准率_中小", "INTEREST_RATE_SB")]),
)


def _macro_econ_preset() -> ExtConfig:
    cols = ["期间"] + [c for _, _, pairs in _ECON_SPECS for c, _ in pairs]
    return ExtConfig(
        id="ext_macro_econ",
        label="宏观经济指标",
        mode="snapshot",
        market_level=True,
        fields=[ExtField("期间", "string", "数据期间")]
        + [ExtField(n, "float", n) for n in cols[1:]],
        description=("东财数据中心月度/季度宏观: PMI(制造/非制造) · CPI · PPI · GDP · "
                     "社零 · 存款准备金率，各取近 24 期。季度值(如 GDP)按季末月对齐到对应月份行。"),
        pull=PullConfig(url=_EM_DC, method="GET", schedule_minutes=1440, enabled=False),
    )


def _period_key(raw: str, fallback: str) -> str:
    """把期间文本归一成「按月」的排序键, 让不同报表能对齐到同一行。

      - 月度报表: 2026年08月份       → 2026-08
      - 季度报表: 2026年第1-2季度    → 2026-06   (累计到第 n 季 → 该季末月)
      - 无 TIME 的报表 (存准率): 用 MONTH_DATE/REPORT_DATE → 2026-09
    """
    text = _text(raw)
    m = re.match(r"^(\d{4})年(\d{1,2})月", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})年第(.+?)季度", text)
    if m:
        digits = re.sub(r"\D", "", m.group(2))
        if digits:
            return f"{int(m.group(1)):04d}-{int(digits[-1]) * 3:02d}"
    return _day(fallback)[:7] or text


async def _fetch_macro_econ(config: ExtConfig, data_dir: Path) -> int:
    table: dict[str, dict] = {}
    async with _httpx_client() as client:
        for report, period_field, pairs in _ECON_SPECS:
            try:
                rows = await _fetch_dc(client, report, sort=period_field, order="-1",
                                       page_size=24, max_pages=1)
            except Exception as e:  # noqa: BLE001
                logger.warning("宏观报表 %s 拉取失败(跳过): %s", report, e)
                continue
            for rec in rows:
                period = _period_key(_text(rec.get("TIME")), _text(rec.get(period_field)))
                if not period:
                    continue
                row = table.setdefault(period, {"期间": period})
                for col, src in pairs:
                    value = _num(rec.get(src))
                    if value is not None:
                        row[col] = value
            await asyncio.sleep(_EM_SLEEP)
    out = [table[k] for k in sorted(table, reverse=True)]
    if not out:
        raise ValueError("宏观经济指标全部报表为空")
    logger.info("宏观经济指标: %d 期", len(out))
    return _write(out, config, data_dir)


def _macro_calendar_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_macro_calendar",
        label="全球宏观日历",
        mode="snapshot",
        market_level=True,
        fields=[
            ExtField("日历条目", "string", "日历条目(时间+标题)"),
            ExtField("时间", "string", "公布时间"),
            ExtField("国家", "string", "国家/地区"),
            ExtField("标题", "string", "事件标题"),
            ExtField("类型", "string", "data 数据 / event 事件"),
            ExtField("重要度", "int", "重要度 1-4"),
            ExtField("公布值", "float", "公布值"),
            ExtField("预期值", "float", "预期值"),
            ExtField("前值", "float", "前值"),
            ExtField("修正值", "float", "修正值"),
            ExtField("单位", "string", "单位"),
            ExtField("周期", "string", "数据周期"),
        ],
        description=("华尔街见闻全球宏观日历(近 14 天): 经济数据公布值/预期/前值 + "
                     "重要事件(央行议息等)。社融等中国数据的公布日程也在此表中。"),
        pull=PullConfig(url=_WSCN_CAL, method="GET", schedule_minutes=1440, enabled=False),
    )


async def _fetch_macro_calendar(config: ExtConfig, data_dir: Path) -> int:
    # 接口单次只允许一周，按 7 天切片；取「前 7 天 + 后 7 天」
    today = _today()
    windows = [(today - timedelta(days=7), today - timedelta(days=1)),
               (today, today + timedelta(days=6))]
    out: list[dict] = []
    seen: set[str] = set()
    async with _httpx_client() as client:
        for start, stop in windows:
            resp = await _get(client, _WSCN_CAL, params={
                "start": str(int(datetime.combine(start, datetime.min.time(),
                                                  _CN_TZ).timestamp())),
                "end": str(int(datetime.combine(stop, datetime.min.time(),
                                                _CN_TZ).timestamp())),
            })
            items = ((resp.json().get("data") or {}).get("items")) or []
            for it in items:
                pub = _int(it.get("public_date"))
                when = datetime.fromtimestamp(pub, _CN_TZ).strftime("%Y-%m-%d %H:%M") if pub else ""
                title = _text(it.get("title"))
                key = f"{when} {title}".strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "日历条目": key,
                    "时间": when,
                    "国家": _text(it.get("country")),
                    "标题": title,
                    "类型": {"FD": "data", "FE": "event"}.get(_text(it.get("calendar_type")), ""),
                    "重要度": _int(it.get("importance")),
                    "公布值": _num(it.get("actual")),
                    "预期值": _num(it.get("forecast")),
                    "前值": _num(it.get("previous")),
                    "修正值": _num(it.get("revised")),
                    "单位": _text(it.get("unit")),
                    "周期": _text(it.get("period")),
                })
            await asyncio.sleep(0.5)
    out.sort(key=lambda r: r["时间"], reverse=True)
    if not out:
        raise ValueError("过去 7 天与未来 7 天均无宏观日历条目")
    logger.info("全球宏观日历: %d 条", len(out))
    return _write(out, config, data_dir)


# ---------------------------------------------------------------------------
# L15 可转债层 — 东财可转债全表
# ---------------------------------------------------------------------------

_CB_QUOTE_COLUMNS = ("f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,"
                     "f235~10~SECURITY_CODE~TRANSFER_PRICE,"
                     "f236~10~SECURITY_CODE~TRANSFER_VALUE,"
                     "f2~10~SECURITY_CODE~CURRENT_BOND_PRICE,"
                     "f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO")


def _cb_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_cb_em",
        label="可转债(东财)",
        mode="snapshot",
        market_level=True,
        fields=[
            ExtField("转债代码", "string", "转债代码"),
            ExtField("转债名称", "string", "转债名称"),
            ExtField("状态", "string", "upcoming 未上市 / listed 交易中 / delisted 已摘牌"),
            ExtField("正股代码", "string", "正股代码"),
            ExtField("正股名称", "string", "正股名称"),
            ExtField("评级", "string", "债项评级"),
            ExtField("发行规模_亿", "float", "发行规模(亿元)"),
            ExtField("申购日", "string", "申购日"),
            ExtField("申购代码", "string", "申购代码"),
            ExtField("上市日", "string", "上市日"),
            ExtField("摘牌日", "string", "摘牌日"),
            ExtField("到期日", "string", "到期日"),
            ExtField("转股起始日", "string", "转股起始日"),
            ExtField("初始转股价", "float", "初始转股价"),
            ExtField("最新转股价", "float", "最新转股价"),
            ExtField("债价", "float", "最新债价"),
            ExtField("正股价", "float", "正股最新价"),
            ExtField("转股价值", "float", "转股价值"),
            ExtField("转股溢价率", "float", "转股溢价率 %"),
        ],
        description=("东财数据中心可转债全表: 基本条款(评级/规模/日期) + 最新转股价 / "
                     "债价 / 正股价 / 转股价值 / 溢价率。默认剔除已摘牌。"),
        pull=PullConfig(url=_EM_DC, method="GET", schedule_minutes=1440, enabled=False),
    )


async def _fetch_cb(config: ExtConfig, data_dir: Path) -> int:
    async with _httpx_client() as client:
        rows = await _fetch_dc(client, "RPT_BOND_CB_LIST",
                               sort="PUBLIC_START_DATE", order="-1",
                               page_size=500, max_pages=8,
                               extra={"quoteColumns": _CB_QUOTE_COLUMNS, "quoteType": "0"})
    if not rows:
        raise ValueError("东财可转债列表为空，接口可能已变更")
    today = _today().isoformat()          # 按北京时间，不看本机时区
    out: list[dict] = []
    for rec in rows:
        listing = _day(rec.get("LISTING_DATE"))
        delist = _day(rec.get("DELIST_DATE"))
        market = _text(rec.get("TRADE_MARKET"))
        if (delist and delist <= today) or market == "STAS00":     # STAS00 = 退市板块
            continue                                              # 默认剔除已摘牌
        if market not in ("CNSESH", "CNSESZ"):
            status = "unknown"
        elif listing and listing <= today:
            status = "listed"
        elif listing or _day(rec.get("PUBLIC_START_DATE")):
            status = "upcoming"
        else:
            status = "unknown"
        code = _text(rec.get("SECURITY_CODE"))
        if not code:
            continue
        out.append({
            "转债代码": code,
            "转债名称": _text(rec.get("SECURITY_NAME_ABBR")),
            "状态": status,
            "正股代码": _text(rec.get("CONVERT_STOCK_CODE")),
            "正股名称": _text(rec.get("SECURITY_SHORT_NAME")),
            "评级": _text(rec.get("RATING")),
            "发行规模_亿": _num(rec.get("ACTUAL_ISSUE_SCALE")),
            "申购日": _day(rec.get("PUBLIC_START_DATE")),
            "申购代码": _text(rec.get("CORRECODE")),
            "上市日": listing,
            "摘牌日": delist,
            "到期日": _day(rec.get("EXPIRE_DATE")),
            "转股起始日": _day(rec.get("TRANSFER_START_DATE")),
            "初始转股价": _num(rec.get("INITIAL_TRANSFER_PRICE")),
            "最新转股价": _num(rec.get("TRANSFER_PRICE")),
            "债价": _num(rec.get("CURRENT_BOND_PRICE")),
            "正股价": _num(rec.get("CONVERT_STOCK_PRICE")),
            "转股价值": _num(rec.get("TRANSFER_VALUE")),
            "转股溢价率": _num(rec.get("TRANSFER_PREMIUM_RATIO")),
        })
    if not out:
        raise ValueError("东财可转债列表过滤后为空")
    logger.info("可转债: %d 只(已剔除摘牌)", len(out))
    return _write(out, config, data_dir)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

# (预设工厂, 拉取实现) — 顺序即扩展数据页的展示顺序
_INFO_SPECS: tuple[tuple[Callable[[], ExtConfig], Callable], ...] = (
    (_report_preset, _fetch_report),                    # L2  研报
    (_news_cls_preset, _fetch_news_cls),                # L5  新闻
    (_news_724_preset, _fetch_news_724),                # L5  新闻
    (_annt_preset, _fetch_annt),                        # L7  公告
    (_opt_preset, _fetch_opt),                          # L9  ETF 期权
    (_hot_preset, _fetch_hot),                          # L10 舆情互动
    (_macro_curve_preset, _fetch_macro_curve),          # L11 宏观与利率
    (_macro_econ_preset, _fetch_macro_econ),            # L11 宏观与利率
    (_macro_calendar_preset, _fetch_macro_calendar),    # L11 宏观与利率
    (_cb_preset, _fetch_cb),                            # L15 可转债
)


def info_presets() -> list[ExtConfig]:
    """信息面 7 层的全部预设定义 (启动时由 ensure_builtin_presets 落 config.json)。"""
    return [factory() for factory, _ in _INFO_SPECS]


def info_fetcher(config_id: str) -> Callable | None:
    """按 id 取拉取实现; 非信息面预设返回 None。"""
    for factory, fetcher in _INFO_SPECS:
        if factory().id == config_id:
            return fetcher
    return None
