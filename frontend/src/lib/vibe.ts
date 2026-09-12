// vibe-astock 数据访问层 — 通过 TSP 后端代理 /api/vibe/* 调 vibe 后端 (:8910)
// 页面组件只依赖这里，不直接碰 vibe 地址。

const VIBE = '/api/vibe/api'

async function vget<T>(path: string): Promise<T> {
  const res = await fetch(`${VIBE}${path}`)
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      msg = j.error || j.detail || msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

async function vpost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${VIBE}${path}`, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      msg = j.error || j.detail || msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// ===== 类型（与 vibe 后端契约对齐） =====

export interface ReviewData {
  schema_version?: number
  run_type?: string
  review_id?: string
  target_date?: string
  trade_date?: string
  data_as_of?: string
  generated_at?: string
  warnings?: string[]
  focus?: unknown
  focus_md?: string
  emotion_metrics?: Record<string, unknown>
  market_facts?: Record<string, unknown>
  macro_sector?: Record<string, unknown>
  reflection?: Record<string, unknown>
  analysts?: Record<string, unknown>
}

export interface ReviewDates { dates: string[] }

export interface JobStatus {
  running: boolean
  date?: string | null
  job_id?: string | null
  error?: string | null
  elapsed?: number
  finished_at?: string | null
}

export interface DeepDiveData {
  schema_version?: number
  run_type?: string
  code: string
  name?: string
  trade_date?: string
  generated_at?: string
  verdict?: {
    one_liner?: string
    theme?: string
    capital?: string
    technical?: string
    risks?: string[]
  }
  verdict_md?: string
  reports?: Record<string, unknown>
  debate?: Record<string, unknown>
}

export interface AuctionData {
  available: boolean
  reason?: string
  date?: string
  prev_date?: string
  captured_at?: string
  overall?: {
    sample: number
    avg: number
    median: number
    up_count: number
    down_count: number
    up_rate: number
    deep_loss_5: number
  }
  by_tier?: Record<string, { sample: number; avg: number; median: number; up_rate: number }>
  top_boards?: Array<{ code: string; name: string; boards: number; sector: string; pct: number }>
  top_board_level?: number
  verification_early?: Array<{
    metric: string
    label: string
    expect: string
    early_value: number | null
    unit: string
    note: string
  }>
}

export interface DayPathData {
  available: boolean
  reason?: string
  date?: string
  points?: Array<{
    slot: string
    median: number
    avg: number
    up_rate: number
    deep_loss_5: number
    live?: { limit_up: number; broken: number; limit_down: number; highest_board: number; sectors: number } | null
    coverage_rate?: number | null
  }>
}

export interface MonitorRow {
  code: string
  name: string
  kind: string
  time: string
  price: number
  pct: number
  text: string
}

export interface MonitorSnapshot {
  rows?: MonitorRow[]
  watch?: Array<{ code: string; name: string; price: number; pct: number }>
}

// ===== API =====

export const vibeApi = {
  reviewDates: () => vget<ReviewDates>('/review/dates'),
  reviewLatest: (date?: string) => vget<ReviewData>(`/review/latest${date ? `?date=${date}` : ''}`),
  reviewStatus: () => vget<JobStatus>('/review/status'),
  reviewRun: (date?: string) => vpost<JobStatus>(`/review/run${date ? `?date=${date}` : ''}`),
  deepdiveLatest: () => vget<DeepDiveData>('/deepdive/latest'),
  deepdiveStatus: () => vget<JobStatus>('/deepdive/status'),
  deepdiveRun: (stock: string) => vpost<JobStatus>(`/deepdive/run?stock=${encodeURIComponent(stock)}`),
  auction: () => vget<AuctionData>('/intraday/auction'),
  intradayPath: () => vget<DayPathData>('/intraday/path'),
  intradayCapture: () => vpost<{ ok?: boolean }>('/intraday/capture'),
  monitorSnapshot: () => vget<MonitorSnapshot>('/monitor/snapshot'),
  // 盘面数据 / 首板分析
  marketSession: () => vget<any>('/market/session'),
  globalIndices: () => vget<any>('/global/indices'),
  overseas: () => vget<any>('/market/overseas'),
  liveEmotion: () => vget<any>('/market/live-emotion'),
  positions: () => vget<any>('/positions'),
  firstBoard: () => vget<any>('/market/first-board'),
  // 盘面数据补充
  aIndices: () => vget<any>('/indices'),
  marketOverview: () => vget<any>('/market/overview'),
  turnoverTop: () => vget<any>('/market/turnover-top'),
  emotion: () => vget<any>('/market/emotion'),
  quote: (codes: string) => vget<any>(`/quote?codes=${encodeURIComponent(codes)}`),
  // 商品 + 汇率（TSP 后端自有接口，非 vibe 代理）
  commodityPrices: () => fetch('/api/commodity/prices').then(r => r.json()) as Promise<{ rows: any[] }>,
  commodityFx: () => fetch('/api/commodity/fx').then(r => r.json()) as Promise<{ rows: any[] }>,
  // K 线：大盘指数走 TSP /api/kline/daily；美股/商品/汇率走 /api/yahoo/kline
  indexKline: (symbol: string, days = 120) =>
    fetch(`/api/kline/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`).then(r => r.json()) as Promise<any>,
  yahooKline: (symbol: string, days = 120) =>
    fetch(`/api/yahoo/kline?symbol=${encodeURIComponent(symbol)}&days=${days}`).then(r => r.json()) as Promise<{ symbol?: string; name?: string; rows?: any[] }>,
  // 复盘追问（AgentChat）
  reviewChat: (messages: Array<{ role: string; content: string }>) =>
    vpost<{ answer?: string; error?: string }>('/review/chat', { messages }),
  // 用户自定义验证条件
  verificationMenu: () => vget<{
    directions: string[];
    metrics: Array<{ key: string; label: string; hint: string; unit: string; higher_is_hotter: boolean }>
  }>('/verification/menu'),
  verificationItems: (date: string) =>
    vget<{ date: string; items: any[] }>(`/verification/items?date=${encodeURIComponent(date)}`),
  verificationSave: (date: string, items: any[]) =>
    vpost<{ ok?: boolean; error?: string }>(`/verification/items?date=${encodeURIComponent(date)}`, { items }),
}
