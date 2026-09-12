"""vibe-astock 子应用挂载适配 —— 让 TSP 后端同一进程、同一端口(3018)承载 vibe 的全部路由。

用法: TSP main.py 里 `import app.vibe.embed as vibe_embed` 后
      `app.mount("/api/vibe", vibe_embed.app)`。

效果:
  - vibe 自己的路由 `/api/review/latest` 等 → 实际路径 `/api/vibe/api/review/latest`
  - 与 TSP 前端 vibe.ts 里 `VIBE = '/api/vibe/api'` 完全吻合，前端零改动
  - vibe-astock 原项目(8910)完全不动
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 把本目录(backend/app/vibe)加入 sys.path，使 `import duanxian` / `import main` 解析到本地副本
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import server as vibe_server  # noqa: E402  顶层 sys.path 就绪后才 import

# vibe 的 FastAPI 子应用
app = vibe_server.app
