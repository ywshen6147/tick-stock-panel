"""全局测试隔离: 把 settings.data_dir 重定向到 tmp, 测试永不触碰真实 data/。

背景 (2026-10-15 实锤, 两个污染事故):
1. test_quote_service_boot_check 经 preferences.save() (锁内读-改-写全文件)
   直接改写了真实 data/user_data/preferences.json, 把 realtime_quotes_enabled
   持久化成 false;
2. 某测试运行把真实 data/adj_factor/all.parquet 覆盖成 0 行。

语义:
- 默认每个测试的 settings.data_dir 指向独立 tmp 目录 → 空配置起步,
  所有读写 (preferences/secrets/kline/adj_factor/...) 全部落在 tmp;
- 测试内自行 monkeypatch settings.data_dir 的用例不受影响
  (后设置的 monkeypatch 生效, 本 fixture 只是兜底默认);
- preferences 的进程级缓存在 fixture 前后各失效一次, 防跨测试泄漏。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_real_data_dir(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import preferences

    fake_data_dir = tmp_path / "data"  # 不预创建: 不少测试自己 mkdir()(无 exist_ok), 需要的代码路径会自建
    monkeypatch.setattr(settings, "data_dir", fake_data_dir)
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()
