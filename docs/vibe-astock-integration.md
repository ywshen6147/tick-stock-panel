# TSP × vibe-astock 整合蓝图（方案 C：入口统一 + 数据打通）

> 状态：设计稿（待确认后分阶段实施）
> 目标：一个入口打开两个工具，自选/持仓互通，复盘产物在 TSP 可查，TSP 实时行情喂给盯盘

---

## 一、架构总览

```
┌─────────────────────────────────────────────┐
│            TSP 主工作台（3011/3018）          │
│  侧栏(扩展系统 custom/*/extension.tsx)       │
│   ┌───────────┐  ┌───────────────────────┐  │
│   │ 既有功能   │  │ ★ 复盘看板(iframe)    │  │
│   │ 自选/策略/  │  │ ★ 盯盘(iframe)       │  │
│   │ 数据/设置…  │  │   (内嵌 vibe 页面)    │  │
│   └───────────┘  └───────────────────────┘  │
└───────────────┬─────────────────┬───────────┘
                │ 同源代理(3018)   │ 跨端口直连(CORS已放开)
                ▼                 ▼
        ┌──────────────┐   ┌──────────────┐
        │ TSP 后端 API  │   │ vibe 后端 API │
        │ /api/watchlist│   │ /api/positions│
        │ /api/overview │   │ /api/review/  │
        │ /api/quote…   │   │ /api/journal/ │
        └──────────────┘   └──────┬───────┘
                                  │ 数据打通桥
                                  ▼
                      ┌───────────────────────┐
                      │   共享数据同步层        │
                      │ (自选↔、持仓↔、产物)    │
                      └───────────────────────┘
```

## 二、分模块设计

### 模块 1：入口统一（TSP 侧栏内嵌 vibe）
- **做法**：TSP `frontend/src/custom/vibe/extension.tsx` 注册自定义路由 + 侧栏导航
  - 路由 `/vibe/review` → iframe `http://127.0.0.1:8910/`（复盘看板）
  - 路由 `/vibe/watch` → iframe `http://127.0.0.1:8910/#watch`（盯盘/自选）
  - 侧栏导航项："复盘看板"（icon: 图表）、"盯盘"（icon: 雷达）
- **iframe 关键处理**：
  - vibe 页面默认跳转 `#` 路由，需确认其路由模式（hash 或 history）
  - TSP 侧栏若折叠，iframe 区域自适应
- **依赖**：vibe-astock 服务（8910）需保持运行 → 合并启动脚本

### 模块 2：合并一键启动
- **现状**：TSP-静默启动.bat（起 3018/3011）+ Vibe-Astock-静默启动.bat（起 8910）
- **改造**：一个 `TSP-整合启动.bat`：
  1. 起 TSP 后端/前端（现有逻辑）
  2. 起 vibe-astock（8910，若未运行）
  3. 等 TSP 就绪 → 开 TSP 窗口（内含复盘入口）
- 依赖：两个服务都活着，iframe 才能加载

### 模块 3：数据打通桥（核心，双向往来）

#### 3a. TSP 自选 → vibe 自选
- **数据**：TSP `/api/watchlist`（symbols + 分组）
- **做法**：vibe 侧加一个"从 TSP 导入自选"入口（或自动同步）
  - 轮询 TSP `/api/watchlist` → 写入 vibe 自选存储
  - 或 vibe 自选页按钮触发导入
- **方向**：TSP → vibe（TSP 是主维护方）

#### 3b. TSP 持仓 → vibe 持仓
- **数据**：TSP 目前**没有**持仓管理（只有回测 positions），vibe 有 `/api/positions` + 交易日志
- **现状**：持仓数据只在 vibe 侧维护（`~/.duanxian-agents/`）
- **建议**：**vibe 仍是持仓唯一权威**；TSP 侧（若有持仓展示）读 vibe 的 `/api/positions`
- **方向**：vibe → TSP（vibe 是持仓权威）

#### 3c. vibe 复盘产物 → TSP 可查
- **数据**：vibe `/api/review/latest`（JSON 产物）
- **做法**：TSP 复盘入口页（iframe 内）即可看；若要在 TSP 原生 UI 展示，需 TSP 前端组件解析 vibe 产物格式
- **建议**：先依赖 iframe（零成本），TSP 原生展示留作二期

#### 3d. TSP 实时行情 → vibe 盯盘
- **数据**：TSP `/api/quotes/...`（全市场实时，新浪源 5555 只）
- **现状**：vibe 盯盘自拉腾讯行情（qt.gtimg.cn）
- **价值**：TSP 已有一份全市场实时缓存，vibe 可复用避免双份请求
- **做法**：vibe 盯盘增加"数据源=TSP"选项，轮询 TSP 行情 API
- **注意**：TSP 实时数据在**非交易时段不轮询**（周六 market_phase=closed），vibe 需有兜底（腾讯直连）

### 模块 4：安全与边界
- **认证**：TSP 有密码鉴权，vibe 无。iframe 内嵌 vibe 时，vibe 接口对本机开放（默认安全）
- **CORS**：TSP 已放开（allow_origins=*），vibe 跨端口调用 TSP 无阻碍；反向（TSP 调 vibe）需 vibe 侧确认 CORS
- **数据落盘**：自选/持仓/复盘各自仍存各自本地，通过 API 同步（不共享文件，避免写冲突）

---

## 三、实施阶段（建议分批）

| 阶段 | 内容 | 工作量 | 交付 |
|---|---|---|---|
| **P1** | 入口统一：TSP 侧栏加"复盘看板/盯盘"iframe 入口 | ~0.5 天 | 侧栏可见、可点、iframe 正常加载 |
| **P2** | 合并一键启动：`TSP-整合启动.bat` 同时起两服务 | ~0.5 天 | 双击一个 bat 全起来 |
| **P3** | 自选同步：vibe 自选页可从 TSP 导入 | ~0.5 天 | 自选互通 |
| **P4** | 持仓反向：TSP（若需要展示）读 vibe 持仓 | ~0.5~1 天 | 持仓互通 |
| **P5** | 盯盘数据源切换：vibe 盯盘可选 TSP 实时源 | ~1 天 | 实时行情复用 |
| **P6** | 复盘产物 TSP 原生展示（可选） | ~1 天 | 二期 |

**建议顺序**：P1 → P2 → P3 → P5（P4/P6 视需要）

---

## 四、核实后的关键事实（2026-09-05 实测）

1. **vibe-astock 路由**：history 模式（`createBrowserRouter`），且 server.py 有 SPA fallback（`FileResponse(index.html)`）→ iframe 可用 `http://127.0.0.1:8910/看板路径` 直接嵌入
2. **TSP 已有持仓管理**：`/api/lots`（批次登记：symbol/qty/cost_price/buy_date/止盈止损/提醒）→ **TSP 是持仓权威**，vibe 持仓可反向读 TSP
3. **TSP 实时行情**：`/api/intraday/status`（服务状态）+ quote_service 缓存；全市场实时走新浪源 5555 只 → vibe 盯盘可复用
4. **TSP 扩展**：`frontend/src/custom/*/extension.tsx` 自动加载，支持 routes + navigation + slots
5. **CORS**：TSP 后端 `allow_origins=["*"]` 全放开；vibe 侧跨端口调用需确认（FastAPI 默认不放开，需在 vibe 加 CORSMiddleware 或走 TSP 代理）

## 四-2. 数据同步方向（修正版）

| 数据 | 权威方 | 同步方向 | 实现 |
|---|---|---|---|
| 自选股 | TSP（`/api/watchlist`） | TSP → vibe | vibe 加"从 TSP 导入"或轮询 |
| 持仓 | TSP（`/api/lots`） | TSP → vibe | vibe 持仓/交易日志读 TSP lots（需 symbol 格式归一） |
| 复盘产物 | vibe（`/api/review/latest`） | vibe → TSP | iframe 内看（零成本）；原生展示二期 |
| 实时行情 | TSP（新浪源缓存） | TSP → vibe 盯盘 | vibe 盯盘加"数据源=TSP"，轮询 TSP 行情 |

---

## 五、风险与注意

- **两服务生命周期**：iframe 依赖 vibe 服务存活；P2 合并启动解决，但单独关一个服务会让入口白屏 → 需错误占位提示
- **数据口径差异**：TSP 自选 symbol 格式（600519.SH）与 vibe 可能不同（600519）→ 同步需做代码格式归一
- **vibe 升级覆盖**：vibe-astock 是 git 仓库，改其源码（数据同步层）会被 pull 覆盖 → 同步层尽量做成独立文件/插件
- **实时源冲突**：P5 后 vibe 盯盘用 TSP 源，但 TSP 非交易时段无数据 → 需回退逻辑
