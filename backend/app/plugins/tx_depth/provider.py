"""腾讯五档盘口 provider — 零鉴权, 纯 HTTP, 只声明 depth5 数据集。

数据来源
--------
腾讯行情批量快照 ``https://qt.gtimg.cn/q=sh600519,sz000001,...``, 免注册免 Key,
覆盖沪深北全市场(沪 sh / 深 sz / 北 bj)。单请求可携带数十只代码, 休市时返回
上一交易日收盘定格盘口 —— 「真封板」判定(涨停价上卖一量==0 / 跌停价上买一量==0)
以收盘定格值为准, 与 depth_service 盘后定版语义一致。

响应格式(GBK, ``~`` 分隔, 0 基下标, 经 600519/000001/920002/688553 实测):
    [9..18]   买一价,买一量,买二价,买二量,...,买五价,买五量
    [19..28]  卖一价,卖一量,卖二价,卖二量,...,卖五价,卖五量
    [30]      快照时间 ``YYYYMMDDHHMMSS``(北京时间)
    量为**手**(手数), 缺档填 0。

与消费方契约的对接(app/services/depth_service.py)
--------------------------------------------------
``get_depth_batch(symbols) -> {symbol: {"bid_volumes": [...5档买量...],
"ask_volumes": [...5档卖量...], "timestamp": 毫秒}}``。消费方只取
``bid_volumes[0]``(买一)与 ``ask_volumes[0]``(卖一) 做真假封板判定,
并把量当**封单量(手)**落盘 —— ``api/monitor_rules.py`` 计算
``封单额 = 封单量 × 100 × 价格``, 证实 canonical 单位就是手, 与腾讯零换算。

设计要点
--------
1. **只声明 depth5 数据集** — 其余数据集未声明自动保持原路由, 本次接入不改动
   日K/除权/实时/分钟/全量分时/财务的任何配置。
2. **键取请求侧 symbol** — 响应按「请求代码 → TSP symbol」回填, 不依赖响应顺序。
3. **不猜数据** — 字段缺失/非数值的档位记 ``None``, 不补 0(0 在封板语义里
   表示「该价无挂单」, 补 0 会把缺档误判成真封板)。

已知边界
--------
- 腾讯盘口为快照(L1), 非逐笔重演; 盘中精度取决于轮询间隔(depth_service 节流)。
- 指数无盘口语义, 请求指数代码会得到全 0 档位; depth_service 只拉涨跌停名单,
  实际不会出现。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_API = "https://qt.gtimg.cn/q="
_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

_BATCH = 60          # 单请求代码数(URL 长度与上游友好折衷; 服务侧批上限 100)
_INTERVAL_S = 0.2    # 批间小睡, 服务层另有 2s 级节流
_TIMEOUT_S = 10.0
_MIN_FIELDS = 29     # 需要下标 0..28(卖五量), 再少视为无效行

# 响应下标 -> 语义
_BID_VOL_IDX = (10, 12, 14, 16, 18)   # 买一量..买五量(手)
_ASK_VOL_IDX = (20, 22, 24, 26, 28)   # 卖一量..卖五量(手)
_TIME_IDX = 30                        # 快照时间 YYYYMMDDHHMMSS

_TZ_CN = timezone(timedelta(hours=8))
_LINE_RE = re.compile(r"^v_(sh|sz|bj)([A-Za-z0-9]+)$")

_SUFFIX_TO_MARKET = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def availability() -> tuple[bool, str]:
    """本插件零依赖零 Key, 恒可用。"""
    return True, "ok"


def _norm_symbols(symbols: list[str] | None) -> list[str]:
    """归一为 TSP 口径(``600519.SH``), 保持输入顺序并去重。

    容忍裸 6 位代码, 按号段补后缀(6/9 开头→SH, 4/8/920 开头→BJ, 其余→SZ);
    其余无法辨认的原样透传(取数时查不到市场前缀会被跳过, 不静默丢键)。
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


def _to_tencent_code(symbol: str) -> str | None:
    """``600519.SH`` -> ``sh600519``; 不认得的返回 None。"""
    if "." not in symbol:
        return None
    code, _, suffix = symbol.partition(".")
    market = _SUFFIX_TO_MARKET.get(suffix)
    if not market or not code:
        return None
    return f"{market}{code}"


def _parse_int(value: str) -> int | None:
    """档位量转换: 非数值/空 -> None(不猜、不补 0, 见模块 docstring)。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_time_ms(value: str) -> int | None:
    """``20261009161459``(北京时间) -> epoch 毫秒; 解析失败返回 None。"""
    text = (value or "").strip()
    if len(text) < 14 or not text[:14].isdigit():
        return None
    try:
        dt = datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=_TZ_CN)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


@dataclass
class _Config:
    """占位配置对象 — loader 会读 .name / .display_name / .datasets, 保持接口一致。

    ``datasets`` 的键必须是**数据集名**(这里只有 ``depth5``): 路由门控
    ``provider_has_dataset(provider, "depth5")`` 直接查这个键, 且
    ``tickflow.policy._augment_custom_sources`` 据此补授 ``DEPTH5_BATCH`` 能力。
    """

    name: str = "tx_depth"
    display_name: str = "腾讯五档盘口"
    path: object = None
    datasets: dict[str, dict] = field(default_factory=lambda: {"depth5": {}})


class TxDepthProvider:
    """腾讯五档盘口 provider — 只实现 depth5 数据集所需的方法。"""

    name = "tx_depth"
    builtin = True

    def __init__(self) -> None:
        self.config = _Config()
        self._client = httpx.Client(
            timeout=_TIMEOUT_S, headers=_HEADERS, follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- 取数
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """批量拉五档盘口, 返回 ``{symbol: {"bid_volumes","ask_volumes","timestamp"}}``。

        量单位为手(与项目 canonical 一致, 见模块 docstring); 时间为快照
        毫秒 epoch(北京时间), 消费方以其为「数据时刻」, 缺省回退 wall-clock。
        """
        mapping: dict[str, str] = {}   # tencent code -> TSP symbol
        for sym in _norm_symbols(symbols):
            code = _to_tencent_code(sym)
            if code:
                mapping[code] = sym
        if not mapping:
            return {}

        items = list(mapping.items())
        out: dict[str, dict] = {}
        for i in range(0, len(items), _BATCH):
            chunk = items[i:i + _BATCH]
            try:
                out.update(self._fetch_chunk(chunk))
            except Exception as e:  # noqa: BLE001 — 单批失败只隔离该批(消费方有同语义兜底)
                logger.warning(
                    "tx_depth 第 %d/%d 批失败(%d 只), 已跳过: %s",
                    i // _BATCH + 1, (len(items) + _BATCH - 1) // _BATCH, len(chunk), e,
                )
            if i + _BATCH < len(items):
                time.sleep(_INTERVAL_S)
        return out

    def _fetch_chunk(self, chunk: list[tuple[str, str]]) -> dict[str, dict]:
        """取一批(≤_BATCH 只)并解析; 返回 {TSP symbol: 盘口 dict}。"""
        codes = [code for code, _ in chunk]
        resp = self._client.get(_API + ",".join(codes))
        resp.raise_for_status()
        # 腾讯响应是 GBK; content 手工解码, 不依赖响应头 charset
        text = resp.content.decode("gbk", "replace")

        lookup = dict(chunk)
        out: dict[str, dict] = {}
        for line in text.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            var, _, raw = line.partition("=")
            m = _LINE_RE.match(var.strip())
            if not m:
                continue  # v_pv_none_match 等控制行
            code = m.group(1) + m.group(2)
            symbol = lookup.get(code)
            if symbol is None:
                continue
            fields = raw.strip().strip('"').split("~")
            if len(fields) < _MIN_FIELDS:
                logger.debug("tx_depth %s 字段不足(%d), 跳过", symbol, len(fields))
                continue
            out[symbol] = {
                "bid_volumes": [_parse_int(fields[i]) for i in _BID_VOL_IDX],
                "ask_volumes": [_parse_int(fields[i]) for i in _ASK_VOL_IDX],
                "timestamp": _parse_time_ms(fields[_TIME_IDX]),
            }
        return out

    # ---------------------------------------------------------------- 试拉
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」。depth5 单一配置, 返回盘口快照预览。"""
        if dataset != "depth5":
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": f"tx_depth 只提供 depth5 数据集; '{dataset}' 请继续使用原数据源",
            }

        probe = _norm_symbols(symbols) or ["600519.SH", "000001.SZ", "688553.SH"]
        try:
            data = self.get_depth_batch(probe)
        except Exception as e:  # noqa: BLE001
            return {
                "provider": self.name,
                "dataset": "depth5",
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": str(e),
            }

        columns = [
            "symbol", "bid1_vol", "bid2_vol", "bid3_vol", "bid4_vol", "bid5_vol",
            "ask1_vol", "ask2_vol", "ask3_vol", "ask4_vol", "ask5_vol",
            "snapshot_time",
        ]
        preview: list[dict] = []
        for sym in probe:
            d = data.get(sym)
            if not d:
                continue
            row: dict = {"symbol": sym}
            for key, prefix in (("bid_volumes", "bid"), ("ask_volumes", "ask")):
                for level, vol in enumerate(d.get(key) or [], start=1):
                    row[f"{prefix}{level}_vol"] = vol
            ts = d.get("timestamp")
            if ts:
                row["snapshot_time"] = (
                    datetime.fromtimestamp(ts / 1000, _TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
                )
            preview.append(row)

        return {
            "provider": self.name,
            "dataset": "depth5",
            "rows": len(data),
            "columns": columns,
            "preview": preview,
            "symbols": probe,
        }
