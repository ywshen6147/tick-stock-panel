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
- preferences 的进程级缓存在 fixture 前后各失效一次, 防跨测试泄漏;
- 文件日志同步隔离 (2026-10-15 发现): app/main.py 在 import 时把
  RotatingFileHandler 绑定到真实 data/backend.log, data_dir 补丁拦不住
  (logging 全局, handler 路径 import 时已定死) → 测试日志会漏写真实
  backend.log。此处把「指向真实 data_dir 的 FileHandler」整体重定向到
  系统临时目录, 并 patch Logger.addHandler 拦截后续新增 (含
  basicConfig(filename=...) 与 desktop.py 的 FileHandler)。
  仅替换 handler 落盘路径, 级别/格式原样保留; 生产代码零改动。
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 文件日志隔离 (模块级, 幂等; pytest_configure 与 autouse fixture 双保险调用)
# ---------------------------------------------------------------------------
_LOG_ISOLATE_DONE = False


def _isolate_file_logging() -> None:
    """把指向真实 data_dir 的 FileHandler 重定向到系统临时目录 (幂等)。"""
    global _LOG_ISOLATE_DONE

    from app.config import settings

    real_data_dir = Path(settings.data_dir).resolve()
    test_log_dir = Path(tempfile.gettempdir()) / "tsp-test-logs"

    def _retarget(handler: logging.Handler) -> logging.Handler:
        base = getattr(handler, "baseFilename", None)
        if not base:
            return handler
        try:
            under_real = Path(base).resolve().is_relative_to(real_data_dir)
        except (ValueError, OSError):
            return handler
        if not under_real:
            return handler
        new_path = test_log_dir / Path(base).name
        new_path.parent.mkdir(parents=True, exist_ok=True)
        level, formatter = handler.level, handler.formatter
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
        new_handler = logging.FileHandler(
            new_path, mode="a", encoding="utf-8", errors="replace"
        )
        new_handler.setLevel(level)
        if formatter:
            new_handler.setFormatter(formatter)
        return new_handler

    def _purge(logger: logging.Logger) -> None:
        if not any(getattr(h, "baseFilename", None) for h in logger.handlers):
            return
        logger.handlers = [_retarget(h) for h in logger.handlers]

    # 1) 已存在的 handler: root + 所有具名 logger 全扫一遍
    _purge(logging.getLogger())
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger):
            _purge(lg)

    # 2) 拦截后续 addHandler (app.main 在测试中途才被 import 的情形)
    if not getattr(logging.Logger.addHandler, "_tsp_test_patched", False):
        _orig_add_handler = logging.Logger.addHandler

        def _patched_add_handler(self, handler):  # type: ignore[no-untyped-def]
            if isinstance(handler, logging.FileHandler):
                handler = _retarget(handler)
            _orig_add_handler(self, handler)

        _patched_add_handler._tsp_test_patched = True  # type: ignore[attr-defined]
        logging.Logger.addHandler = _patched_add_handler  # type: ignore[method-assign]

    _LOG_ISOLATE_DONE = True


def pytest_configure(config) -> None:  # noqa: ARG001
    try:
        _isolate_file_logging()
    except Exception:  # noqa: BLE001  # app 尚不可导入等情形, fixture 阶段再兜底
        pass


@pytest.fixture(autouse=True)
def _isolate_real_data_dir(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import preferences

    if not _LOG_ISOLATE_DONE:
        _isolate_file_logging()

    fake_data_dir = tmp_path / "data"  # 不预创建: 不少测试自己 mkdir()(无 exist_ok), 需要的代码路径会自建
    monkeypatch.setattr(settings, "data_dir", fake_data_dir)
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()
