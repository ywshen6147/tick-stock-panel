"""短线每日复盘 —— 命令行入口。

用法：
    .venv/bin/python main.py [YYYY-MM-DD]
    不带日期则用今天。
"""

from __future__ import annotations

import sys
import time

from duanxian import preflight, reflection, review_store
from duanxian.llm_errors import LlmConfigError
from duanxian.review_graph import build_review_graph
from duanxian.util import china_today, validate_trade_date


def initial_state(trade_date: str) -> dict:
    return {
        "trade_date": trade_date,
        "sentiment_report": "",
        "capital_report": "",
        "theme_report": "",
        "dragon_tiger_report": "",
        "leader_report": "",
        "macro_sector_report": "",
        "emotion_metrics": {}, "market_facts": {},  # 派生情绪指标（情绪面分析师写入）
        "tomorrow_focus": "",
        "focus_struct": None,
        "past_context": reflection.get_past_context(),  # 反思闭环：注入历史命中回看
    }


def run(trade_date: str) -> tuple[dict, dict]:
    """跑一场复盘。返回 (图的产物, 体检结果)。

    ⚠️ 体检结果必须**跟着返回**：落盘要把它的 warnings 带上，而那行在 `main()` 里。
    只在这里赋值给局部变量的话，`main()` 用它就是 NameError —— 而且是在
    整条链跑完（约 6 分钟）之后才炸、一次也存不下来。
    """
    pre = preflight.check(trade_date)
    if not pre["ok"]:
        print(f"\n✗ {preflight.refuse_reason(pre, trade_date)}")
        sys.exit(3)
    for w in pre["warnings"]:
        print(f"⚠️ {w}")

    graph = build_review_graph()
    return graph.invoke(initial_state(trade_date), {"recursion_limit": 50}), pre


def main() -> None:
    raw = sys.argv[1] if len(sys.argv) > 1 else china_today()
    try:
        trade_date = validate_trade_date(raw)
    except ValueError as exc:
        print(f"日期错误：{exc}")
        sys.exit(1)

    print(f"===== 短线每日复盘  交易日 {trade_date} =====\n")
    t0 = time.time()
    try:
        final, pre = run(trade_date)
    except LlmConfigError as exc:
        # 配置/凭据问题给一句人话，别甩 traceback。它是**故意冒泡**上来的
        # （降级会让复盘"跑成功但内容全空"，见 duanxian/llm_errors.py）
        print(f"\n✗ {exc}")
        sys.exit(2)

    print("―― 五分析师报告 ――")
    for label, field in [
        ("① 情绪面", "sentiment_report"),
        ("② 资金面", "capital_report"),
        ("③ 题材热点", "theme_report"),
        ("④ 龙虎榜游资", "dragon_tiger_report"),
        ("⑤ 龙头跟踪", "leader_report"),
    ]:
        print(f"\n【{label}】\n{final.get(field, '')}")

    print("\n―― 明天关注点 ――")
    print(final["tomorrow_focus"])

    reflection.auto_evaluate_prior(trade_date)   # 与 server 同序：先回评上期预测
    res = review_store.save(review_store.serialize(final, trade_date, pre["warnings"]), trade_date)
    if res.written:
        print(f"\n✓ 已写入 {review_store.DIR}/{trade_date}.json（看板可直接看）")
    else:
        print(f"\n⚠️ 未写入：{res.reason}")

    print(f"\n[耗时 {time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
