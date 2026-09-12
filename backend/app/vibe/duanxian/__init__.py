"""Vibe-Astock — 短线业务层（借用 TradingAgents 的 LangGraph 编排引擎，角色/数据/schema 全为短线重写）。

每日复盘 Agent：五个短线分析师各读一面，裁判收敛成结构化的盘面研判。
"""

import os as _os

_os.environ.setdefault("TQDM_DISABLE", "1")
try:
    from functools import partialmethod as _pm

    import tqdm as _tqdm

    _tqdm.tqdm.__init__ = _pm(_tqdm.tqdm.__init__, disable=True)
    _tqdm.std.tqdm.__init__ = _pm(_tqdm.std.tqdm.__init__, disable=True)
except Exception:  # noqa: BLE001  tqdm 缺失/结构变化都不该影响主流程
    pass
