import { useEffect, useState } from 'react'
import {
  BarChart3, Loader2, RefreshCw, Globe, Activity, Wallet, Layers3, Flame, Plus, X,
  TrendingUp, TrendingDown,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { vibeApi } from '@/lib/vibe'
import { EChartsCandlestick, type OHLC } from '@/components/EChartsCandlestick'

const pctColor = (v: number | null | undefined) => {
  if (v == null) return 'text-muted'
  if (v > 0) return 'text-bull'
  if (v < 0) return 'text-bear'
  return 'text-muted'
}
const signed = (v: number | null | undefined, digits = 2) =>
  v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
const yi = (v: number | null | undefined) => v == null ? '—' : `${(v / 1e8).toFixed(2)} 亿`
// sectors 的 net/inflow/outflow 单位已是「亿元」，直接显示
const yiv = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(2)} 亿`

type Rec = Record<string, any>

// 名称 → 雅虎 symbol（隔夜外围指数）
const OVERSEA_YAHOO: Record<string, string> = {
  '道琼斯': '^DJI', '标普500': '^GSPC', '纳斯达克': '^IXIC', '恒生指数': '^HSI',
}
// 大盘指数名称 → TSP kline symbol
const INDEX_SYMBOL: Record<string, string> = {
  '上证指数': '000001.SH', '深证成指': '399001.SZ', '创业板指': '399006.SZ', '沪深300': '000300.SH',
}
// 商品 label → 雅虎 symbol
const COMMODITY_YAHOO: Record<string, string> = {
  '黄金': 'GC=F', '白银': 'SI=F', '铜': 'HG=F', '原油': 'CL=F', '天然气': 'NG=F',
  '大豆': 'ZS=F', '玉米': 'ZC=F', '小麦': 'ZW=F',
}
// 汇率 name → 雅虎 symbol
const FX_YAHOO: Record<string, string> = {
  '美元指数': 'DX-Y.NYB', '美元/人民币': 'CNY=X', '美元/日元': 'JPY=X',
}

// 自选本地存储（与 vibe 同 key 前缀，避免和 TSP 自选冲突）
const WATCH_KEY = 'vibe-astock-watch'
const AUTO_KEY = 'vibe-astock-auto-refresh'
const loadWatch = (): string[] => {
  try { return JSON.parse(localStorage.getItem(WATCH_KEY) || '[]') } catch { return [] }
}
const saveWatch = (codes: string[]) => localStorage.setItem(WATCH_KEY, JSON.stringify(codes))

export function VibeMarketPage() {
  const [aIdx, setAIdx] = useState<Rec[]>([])
  const [commodityRows, setCommodityRows] = useState<Rec[]>([])
  const [fxRows, setFxRows] = useState<Rec[]>([])
  const [oversea, setOversea] = useState<Rec | null>(null)
  const [liveEmo, setLiveEmo] = useState<Rec | null>(null)
  const [overview, setOverview] = useState<Rec | null>(null)
  const [turnover, setTurnover] = useState<Rec[]>([])
  const [emotion, setEmotion] = useState<Rec | null>(null)
  const [positions, setPositions] = useState<Rec | null>(null)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState('')
  // 自选
  const [watchCodes, setWatchCodes] = useState<string[]>(loadWatch)
  const [watchQuotes, setWatchQuotes] = useState<Rec>({})
  const [watchInput, setWatchInput] = useState('')
  const [watchLoading, setWatchLoading] = useState(false)
  // 自动刷新
  const [autoRefresh, setAutoRefresh] = useState<boolean>(() => localStorage.getItem(AUTO_KEY) === '1')
  // K 线弹窗：{title, symbol, kind} — kind: 'index' (TSP) / 'yahoo'
  const [kline, setKline] = useState<{ title: string; symbol: string; kind: 'index' | 'yahoo' } | null>(null)

  async function loadHeavy() {
    const [ai, co, fx, o, l, ov, tt, em, p] = await Promise.all([
      vibeApi.aIndices(), vibeApi.commodityPrices().catch(() => ({ rows: [] })), vibeApi.commodityFx().catch(() => ({ rows: [] })),
      vibeApi.overseas(), vibeApi.liveEmotion(),
      vibeApi.marketOverview(), vibeApi.turnoverTop(), vibeApi.emotion(), vibeApi.positions(),
    ])
    setAIdx((ai?.data ?? ai ?? []) as Rec[])
    setCommodityRows(co?.rows ?? [])
    setFxRows(fx?.rows ?? [])
    setOversea(o)
    setLiveEmo(l)
    setOverview(ov?.data ?? ov ?? null)
    setTurnover((tt?.data?.stocks ?? tt?.stocks ?? []) as Rec[])
    setEmotion((em?.data ?? em) as Rec | null)
    setPositions(p)
  }

  async function refreshWatch(codes: string[]) {
    if (!codes.length) { setWatchQuotes({}); return }
    setWatchLoading(true)
    try {
      const r = await vibeApi.quote(codes.join(','))
      setWatchQuotes(r?.data ?? r ?? {})
    } catch { /* 行情取不到忽略 */ }
    finally { setWatchLoading(false) }
  }

  async function load() {
    setLoading(true)
    setMsg('')
    try {
      await loadHeavy()
    } catch (e) {
      setMsg(`加载失败: ${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    refreshWatch(loadWatch())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 连板股实时行情（昨日连板名单的今日实时价）
  useEffect(() => {
    const codes = (emotion?.lianban_stocks ?? []).map((s: Rec) => s.code)
    if (codes.length) refreshWatch(codes)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [emotion?.date, emotion?.lianban_stocks?.length])

  // 自动刷新（仅盘中）
  useEffect(() => {
    if (!autoRefresh) return
    const liveTimer = setInterval(() => {
      loadHeavy().catch(() => {})
      if (watchCodes.length) refreshWatch(watchCodes)
    }, 5000)
    const heavyTimer = setInterval(() => loadHeavy().catch(() => {}), 60000)
    return () => { clearInterval(liveTimer); clearInterval(heavyTimer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, watchCodes])

  const toggleAuto = () => {
    const next = !autoRefresh
    setAutoRefresh(next)
    localStorage.setItem(AUTO_KEY, next ? '1' : '0')
  }

  const addWatch = () => {
    const codes = watchInput.split(/[\s,，]+/).filter(Boolean).map(c => c.replace(/\D/g, '').slice(0, 6)).filter(c => c)
    const next = Array.from(new Set([...watchCodes, ...codes]))
    setWatchInput('')
    if (next.length === watchCodes.length) return
    setWatchCodes(next); saveWatch(next); refreshWatch(next)
  }

  const removeWatch = (c: string) => {
    const next = watchCodes.filter(x => x !== c)
    setWatchCodes(next); saveWatch(next); refreshWatch(next)
  }

  const holdings = (positions?.holdings ?? []) as Rec[]
  const overseaIndices = ((oversea ?? {})?.indices ?? []) as Rec[]
  const mag7 = ((oversea ?? {})?.mag7 ?? []) as Rec[]
  const sectors = (overview?.sectors ?? []) as Rec[]
  const sentiment = overview?.sentiment as Rec | undefined
  const ladder = (emotion?.ladder ?? []) as Rec[]
  const lianban = (emotion?.lianban_stocks ?? []) as Rec[]

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <PageHeader
        title="盘面数据"
        subtitle="指数 / 板块资金 / 成交额榜 / 打板情绪 / 外围 / 持仓一屏看全"
        right={
          <div className="flex items-center gap-2">
            <button type="button" onClick={toggleAuto}
              className={cn('inline-flex h-8 items-center gap-1.5 rounded-btn px-2.5 text-xs transition-colors',
                autoRefresh ? 'bg-accent/15 text-accent hover:bg-accent/25' : 'text-muted hover:text-accent')}
              title="开启后在交易时段自动刷新行情">
              <RefreshCw className={cn('h-3.5 w-3.5', autoRefresh && 'animate-spin')} />
              {autoRefresh ? '自动刷新中' : '自动刷新'}
            </button>
            <button type="button" onClick={load} disabled={loading}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground">
              <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} /> 刷新
            </button>
          </div>
        }
      />

      {msg && <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{msg}</div>}
      {loading && <div className="flex items-center justify-center py-12 text-muted"><Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载中…</div>}

      {/* ===== 1. A股大盘指数 ===== */}
      {aIdx.length > 0 && (
        <Section title="大盘指数" icon={BarChart3}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {aIdx.map((g, i) => {
              const sym = INDEX_SYMBOL[g.name] || ''
              return (
                <button key={i} type="button"
                  onClick={() => sym && setKline({ title: `${g.name} · 120日K`, symbol: sym, kind: 'index' })}
                  className={cn('rounded-btn border border-border/60 bg-elevated px-3 py-2.5 text-left transition-colors hover:border-accent/50 hover:bg-elevated/70',
                    !sym && 'cursor-default')}>
                  <div className="text-xs text-muted">{g.name}</div>
                  <div className="mt-1 font-mono text-base font-semibold text-foreground">{g.price?.toLocaleString?.() ?? '—'}</div>
                  <div className={cn('font-mono text-xs font-semibold', pctColor(g.change_pct))}>{signed(g.change_pct)}</div>
                </button>
              )
            })}
          </div>
        </Section>
      )}

      {/* ===== 2. 隔夜外围（含美股七姐妹） ===== */}
      {overseaIndices.length > 0 && (
        <Section title="隔夜外围" icon={Globe} badge={oversea?.us_label || oversea?.date || ''}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            {overseaIndices.map((o: Rec, i: number) => {
              const sym = OVERSEA_YAHOO[o.name] || ''
              return (
                <button key={i} type="button"
                  onClick={() => sym && setKline({ title: `${o.name} · 120日K`, symbol: sym, kind: 'yahoo' })}
                  className={cn('rounded-btn border border-border/60 bg-elevated px-3 py-2.5 text-left transition-colors hover:border-accent/50 hover:bg-elevated/70',
                    !sym && 'cursor-default')}>
                  <div className="text-xs text-muted">{o.name}</div>
                  <div className="mt-1 font-mono text-base font-semibold text-foreground">{o.price ?? '—'}</div>
                  <div className={cn('font-mono text-xs', pctColor(o.change_pct))}>{signed(o.change_pct)}</div>
                  {o.session && <div className="text-[10px] text-muted">{o.session}</div>}
                </button>
              )
            })}
          </div>
          {mag7.length > 0 && (
            <>
              <p className="mb-2 mt-4 text-[11px] text-muted">美股七姐妹 · 权重股带指数走，看它们比看指数细</p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
                {mag7.map((r: Rec) => (
                  <button key={r.ticker} type="button"
                    onClick={() => r.ticker && setKline({ title: `${r.name} · 120日K`, symbol: r.ticker, kind: 'yahoo' })}
                    className="rounded-btn border border-border/60 bg-elevated px-3 py-2.5 text-left transition-colors hover:border-accent/50 hover:bg-elevated/70">
                    <div className="truncate text-xs text-muted">{r.name} <span className="font-mono text-[10px] text-muted/60">{r.ticker}</span></div>
                    <div className="mt-1 font-mono text-base font-semibold text-foreground">{r.price ?? '—'}</div>
                    <div className={cn('font-mono text-xs', pctColor(r.change_pct))}>{signed(r.change_pct)}</div>
                  </button>
                ))}
              </div>
            </>
          )}
        </Section>
      )}

      {/* ===== 3. 商品与汇率 ===== */}
      {(commodityRows.length > 0 || fxRows.length > 0) && (
        <Section title="商品与汇率" icon={Globe}>
          {/* 商品 */}
          {commodityRows.length > 0 && (
            <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
              {commodityRows.map((c: Rec, i: number) => {
                const sym = COMMODITY_YAHOO[c.label] || ''
                return (
                  <button key={c.code || i} type="button"
                    onClick={() => sym && setKline({ title: `${c.label} · 120日K`, symbol: sym, kind: 'yahoo' })}
                    className={cn('rounded-btn border border-border/60 bg-elevated px-3 py-2.5 text-left transition-colors hover:border-accent/50 hover:bg-elevated/70',
                      !sym && 'cursor-default')}>
                    <div className="text-xs text-muted">{c.label}</div>
                    <div className="mt-1 font-mono text-base font-semibold text-foreground">{c.price ?? '—'}</div>
                    <div className={cn('font-mono text-xs font-semibold', pctColor(c.change_pct))}>{signed(c.change_pct)}</div>
                  </button>
                )
              })}
            </div>
          )}
          {/* 汇率 */}
          {fxRows.length > 0 && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {fxRows.map((r: Rec, i: number) => {
                const sym = FX_YAHOO[r.name] || ''
                return (
                  <button key={r.code || i} type="button"
                    onClick={() => sym && setKline({ title: `${r.name} · 120日K`, symbol: sym, kind: 'yahoo' })}
                    className={cn('rounded-btn border border-border/60 bg-elevated px-3 py-2.5 text-left transition-colors hover:border-accent/50 hover:bg-elevated/70',
                      !sym && 'cursor-default')}>
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-muted">{r.name}</span>
                      <span className="text-[10px] text-muted">{r.time}</span>
                    </div>
                    <div className="mt-1 font-mono text-base font-semibold text-foreground">{r.price ?? '—'}</div>
                    <div className={cn('font-mono text-xs font-semibold', pctColor(r.change_pct))}>{signed(r.change_pct)}</div>
                  </button>
                )
              })}
            </div>
          )}
        </Section>
      )}

      {/* ===== 4. 市场情绪 ===== */}
      {sentiment && (
        <Section title="市场情绪" icon={Activity} badge={sentiment.date}>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-btn bg-elevated border border-border/60 p-4">
              <p className="text-xs text-muted">大盘宽度</p>
              <p className="mt-1 text-2xl font-bold text-accent">{sentiment.breadth ?? '—'}</p>
              <p className="mt-1 text-[11px] text-muted">冰点 / 偏弱 / 中性 / 偏强 / 普涨</p>
            </div>
            <div className="rounded-btn bg-elevated border border-border/60 p-4">
              <p className="text-xs text-muted">题材投机</p>
              <p className="mt-1 text-2xl font-bold text-accent">{sentiment.speculation ?? '—'}</p>
              <p className="mt-1 text-[11px] text-muted">冰点 / 普通 / 活跃 / 亢奋</p>
            </div>
          </div>
          <div className="mt-3 grid grid-cols-4 gap-2">
            {[
              { k: '上涨', v: sentiment.up, up: true },
              { k: '下跌', v: sentiment.down, up: false },
              { k: '平盘', v: sentiment.flat, up: null },
              { k: '涨停', v: sentiment.zt, up: true },
              { k: '真实涨停', v: sentiment.zt_real, up: true },
              { k: '跌停', v: sentiment.dt, up: false },
              { k: '真实跌停', v: sentiment.dt_real, up: false },
              { k: '活跃度', v: sentiment.active, up: null },
            ].map(c => (
              <div key={c.k} className="rounded-btn bg-elevated border border-border/40 p-2 text-center">
                <p className="truncate text-[11px] text-muted">{c.k}</p>
                <p className={cn('mt-0.5 font-mono text-sm font-bold', c.up === null ? 'text-foreground' : c.up ? 'text-bull' : 'text-bear')}>{c.v ?? '—'}</p>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* ===== 5. 实时打板情绪 ===== */}
      {liveEmo?.available && (
        <Section title="今日实时打板情绪" icon={Flame} badge={`${liveEmo.date} ${liveEmo.as_of} · ${liveEmo.phase}（随盘变化）`}>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              { k: '涨停', v: liveEmo.zt_count, unit: '', up: true },
              { k: '跌停', v: liveEmo.dt_count, unit: '', up: false },
              { k: '最高连板', v: liveEmo.max_boards, unit: ' 板', up: true },
              { k: '连板(2板+)', v: liveEmo.lianban_count, unit: ' 家', up: true },
            ].map(c => (
              <div key={c.k} className="rounded-btn bg-elevated border border-border/40 p-3 text-center">
                <p className="text-xs text-muted">{c.k}</p>
                <p className={cn('mt-1 font-mono text-xl font-bold', c.v == null ? 'text-muted' : c.up ? 'text-bull' : 'text-bear')}>
                  {c.v ?? '—'}{c.v != null && c.unit}
                </p>
              </div>
            ))}
            {[
              { k: '封板率', v: liveEmo.seal_rate, sub: '封住/尝试涨停', up: true, isRate: true },
              { k: '炸板率', v: liveEmo.break_rate, sub: '炸板/尝试涨停', up: false, isRate: true },
              { k: '晋级率', v: liveEmo.promotion_rate, sub: '上一场涨停今天又封住', up: true, isRate: true },
              { k: '炸板家数', raw: liveEmo.zb_count, sub: '炸板未回封', up: false },
            ].map(c => (
              <div key={c.k} className="rounded-btn bg-elevated border border-border/40 p-3 text-center">
                <p className="text-xs text-muted">{c.k}</p>
                <p className={cn('mt-1 font-mono text-xl font-bold', (c.raw ?? c.v) == null ? 'text-muted' : c.up ? 'text-bull' : 'text-bear')}>
                  {c.raw != null ? c.raw : c.v != null ? `${(c.v * 100).toFixed(1)}%` : '—'}
                </p>
                <p className="mt-0.5 text-[10px] text-muted">{c.sub}</p>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* ===== 6. 昨日短线情绪 ===== */}
      {emotion && (
        <Section title="昨日短线情绪" icon={Flame} badge={`${emotion.date ?? ''} 收盘定稿 · 只有表格里标(实时)的两列随盘刷新`}>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {[
              { k: '涨停', v: `${emotion.zt_count ?? '—'}`, cls: 'text-bull' },
              { k: '跌停', v: `${emotion.dt_count ?? '—'}`, cls: 'text-bear' },
              { k: '最高连板', v: `${emotion.max_boards ?? '—'} 板`, cls: 'text-accent' },
              { k: '连板(2板+)', v: `${emotion.lianban_count ?? '—'} 家`, cls: 'text-accent' },
            ].map(c => (
              <div key={c.k} className="rounded-btn bg-elevated border border-border/40 p-3 text-center">
                <p className="text-[11px] text-muted">{c.k}</p>
                <p className={cn('mt-0.5 font-mono text-xl font-bold', c.cls)}>{c.v}</p>
              </div>
            ))}
          </div>
          <div className="mt-2 grid grid-cols-3 gap-2">
            {[
              { k: '封板率', v: emotion.seal_rate, hint: '封住/尝试涨停', strong: true },
              { k: '炸板率', v: emotion.break_rate, hint: '炸板/尝试涨停', strong: false },
              { k: '晋级率', v: emotion.promotion_rate, hint: '前一交易日涨停今天又封住', strong: true },
            ].map(c => (
              <div key={c.k} className="rounded-btn bg-elevated border border-border/40 p-2.5 text-center">
                <p className="text-[11px] text-muted">{c.k}</p>
                <p className={cn('mt-0.5 font-mono text-sm font-bold', c.strong ? 'text-bull' : 'text-bear')}>
                  {c.v == null ? '—' : `${(c.v * 100).toFixed(1)}%`}
                </p>
                <p className="mt-0.5 text-[10px] text-muted">{c.hint}</p>
              </div>
            ))}
          </div>
          <div className="mt-3">
            <div className="mb-1.5 flex flex-wrap gap-2">
              {ladder.map((l: Rec, i: number) => (
                <span key={i} className="rounded bg-accent/10 px-2.5 py-1 text-[13px] font-semibold text-accent">{l.boards}板 × {l.count}{l.plus ? ' +' : ''}</span>
              ))}
            </div>
            {lianban.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted">
                      {['名称', '连板', '现价(实时)', '今日涨跌(实时)', '昨日成交额', '流通市值', '涨停原因', '行业'].map(h => (
                        <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {lianban.map((s: Rec) => {
                      const q = watchQuotes[s.code] as Rec | undefined
                      const livePct = q?.change_pct
                      return (
                        <tr key={s.code} className="border-b border-border/30">
                          <td className="px-2 py-2"><span className="font-medium text-foreground">{s.name}</span> <span className="text-xs text-muted">{s.code}</span></td>
                          <td className="whitespace-nowrap px-2 py-2 font-mono font-bold text-accent">{s.boards} 板</td>
                          <td className="px-2 py-2 font-mono">{q?.price ?? <span className="text-muted">{s.price}</span>}</td>
                          <td className={cn('px-2 py-2 font-mono', pctColor(livePct))}>
                            {livePct != null ? signed(livePct) : <span className="text-muted">—</span>}
                          </td>
                          <td className="whitespace-nowrap px-2 py-2 font-mono text-muted">{yi(s.amount)}</td>
                          <td className="whitespace-nowrap px-2 py-2 font-mono text-muted">{yi(s.float_cap)}</td>
                          <td className="max-w-56 px-2 py-2 text-xs">
                            {s.reason ? <span className="text-foreground">{s.reason}</span> : <span className="text-muted">—</span>}
                          </td>
                          <td className="whitespace-nowrap px-2 py-2 text-xs text-muted">{s.industry}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </Section>
      )}

      {/* ===== 7. 板块资金流 ===== */}
      {sectors.length > 0 && (
        <Section title="板块资金流" icon={Layers3} badge={`${sectors.length} 个板块`}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="py-1.5 pr-3">板块</th><th className="py-1.5 pr-3">涨幅</th>
                  <th className="py-1.5 pr-3">净流入</th><th className="py-1.5 pr-3">流入</th>
                  <th className="py-1.5 pr-3">流出</th><th className="py-1.5 pr-3">家数</th>
                </tr>
              </thead>
              <tbody>
                {sectors.slice(0, 20).map((s: Rec) => (
                  <tr key={s.name} className="border-b border-border/50">
                    <td className="py-1.5 pr-3 font-semibold text-foreground">{s.name}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(s.pct))}>{signed(s.pct)}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(s.net))}>{yiv(s.net)}</td>
                    <td className="py-1.5 pr-3 font-mono text-bull">{yiv(s.inflow)}</td>
                    <td className="py-1.5 pr-3 font-mono text-bear">{yiv(s.outflow)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{s.firms}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {/* ===== 7b. 资金轮动（流入/流出 Top） ===== */}
      {sectors.length > 0 && (
        <Section title="资金轮动" icon={Layers3} hint="板块级净流入 / 流出 —— 流入榜只放净流入为正、流出榜只放净流出，各取前六。普跌日往往只有两三个行业真净流入。">
          <div className="mb-2 grid gap-4 md:grid-cols-2">
            <div className="rounded-btn border border-border/60 bg-elevated p-3">
              <h4 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-bull">
                <TrendingUp className="h-4 w-4" /> 流入 Top
              </h4>
              {sectors.filter(s => s.net > 0).slice(0, 6).length === 0 ? (
                <p className="py-4 text-center text-sm text-muted">今日没有行业净流入</p>
              ) : (
                <div className="space-y-1.5">
                  {sectors.filter(s => s.net > 0).slice(0, 6).map((s, i) => (
                    <div key={s.name} className="flex items-center gap-3 border-b border-border/30 pb-1.5 text-sm last:border-0">
                      <span className="w-5 text-xs text-muted">{i + 1}</span>
                      <span className="flex-1 truncate">{s.name}</span>
                      <span className={cn('font-mono text-xs', pctColor(s.pct))}>{signed(s.pct)}</span>
                      <span className={cn('w-20 text-right font-mono text-xs', pctColor(s.net))}>{s.net > 0 ? '+' : ''}{yiv(s.net)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="rounded-btn border border-border/60 bg-elevated p-3">
              <h4 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-bear">
                <TrendingDown className="h-4 w-4" /> 流出 Top
              </h4>
              {sectors.filter(s => s.net < 0).sort((a, b) => a.net - b.net).slice(0, 6).length === 0 ? (
                <p className="py-4 text-center text-sm text-muted">今日没有行业净流出</p>
              ) : (
                <div className="space-y-1.5">
                  {sectors.filter(s => s.net < 0).sort((a, b) => a.net - b.net).slice(0, 6).map((s, i) => (
                    <div key={s.name} className="flex items-center gap-3 border-b border-border/30 pb-1.5 text-sm last:border-0">
                      <span className="w-5 text-xs text-muted">{i + 1}</span>
                      <span className="flex-1 truncate">{s.name}</span>
                      <span className={cn('font-mono text-xs', pctColor(s.pct))}>{signed(s.pct)}</span>
                      <span className={cn('w-20 text-right font-mono text-xs', pctColor(s.net))}>{yiv(s.net)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </Section>
      )}

      {/* ===== 8. 成交额榜 ===== */}
      {turnover.length > 0 && (
        <Section title="成交额榜" icon={BarChart3} badge="Top 成交额">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="py-1.5 pr-3">代码</th><th className="py-1.5 pr-3">名称</th><th className="py-1.5 pr-3">现价</th>
                  <th className="py-1.5 pr-3">涨幅</th><th className="py-1.5 pr-3">成交额</th><th className="py-1.5 pr-3">流通市值</th>
                  <th className="py-1.5 pr-3">行业</th>
                </tr>
              </thead>
              <tbody>
                {turnover.slice(0, 15).map((s: Rec) => (
                  <tr key={s.code} className="border-b border-border/50">
                    <td className="py-1.5 pr-3 font-mono text-muted">{s.code}</td>
                    <td className="py-1.5 pr-3 font-semibold text-foreground">{s.name}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{s.price?.toFixed?.(2)}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(s.pct))}>{signed(s.pct)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{yi(s.amount)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{yi(s.float_cap)}</td>
                    <td className="py-1.5 pr-3 text-muted">{s.industry || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {/* ===== 9. 关注股票（自选） ===== */}
      <div className="rounded-btn border border-border bg-elevated p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-foreground">关注股票</h3>
          {watchCodes.length > 0 && (
            <button type="button" onClick={() => refreshWatch(watchCodes)} className="text-muted hover:text-accent" title="刷新价格">
              {watchLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            </button>
          )}
        </div>
        <div className="mb-3 flex gap-2">
          <input
            value={watchInput}
            onChange={e => setWatchInput(e.target.value.replace(/[^\d,\s，]/g, '').slice(0, 80))}
            onKeyDown={e => e.key === 'Enter' && addWatch()}
            placeholder="加自选：可批量，如 600519 000858"
            className="h-8 w-60 rounded-btn border border-border bg-elevated px-3 text-sm text-secondary outline-none focus:border-accent/50"
          />
          <button type="button" onClick={addWatch}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent/15 px-4 text-sm font-medium text-accent hover:bg-accent/25">
            <Plus className="h-4 w-4" /> 增加
          </button>
        </div>
        {watchCodes.length === 0 ? (
          <p className="text-sm text-muted">加上你关注的股票，随时看实时价格与涨跌。数据存本地，不上传。</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {watchCodes.map(c => {
              const q = watchQuotes[c] as Rec | undefined
              const ok = q != null && Number.isFinite(q.price) && q.price > 0
              return (
                <div key={c} className="group relative rounded-btn bg-elevated border border-border/40 p-3">
                  <button type="button" onClick={() => removeWatch(c)} title="移除"
                    className="absolute right-1.5 top-1.5 text-muted opacity-0 transition-opacity hover:text-bear group-hover:opacity-100">
                    <X className="h-3.5 w-3.5" />
                  </button>
                  <p className="truncate text-xs text-muted">{q?.name || c}</p>
                  <p className={cn('mt-1 font-mono text-lg font-bold', ok ? pctColor(q!.change_pct) : 'text-muted')}>
                    {ok ? q!.price : '—'}
                  </p>
                  <p className={cn('text-xs', ok ? pctColor(q!.change_pct) : 'text-muted')}>
                    {ok ? signed(q!.change_pct) : '行情不可用'}
                  </p>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ===== 10. 持仓 ===== */}
      <div className="rounded-btn border border-border bg-elevated p-4">
        <div className="mb-3 flex items-center gap-2">
          <Wallet className="h-4 w-4 text-accent" />
          <span className="text-sm font-semibold text-foreground">当前持仓</span>
          {positions?.total?.complete === false && <span className="text-[10px] text-warning">行情部分不可用（非归零）</span>}
        </div>
        {holdings.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted">暂无持仓记录</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="py-1.5 pr-3">代码</th><th className="py-1.5 pr-3">名称</th><th className="py-1.5 pr-3">持股</th>
                  <th className="py-1.5 pr-3">成本</th><th className="py-1.5 pr-3">现价</th><th className="py-1.5 pr-3">市值</th>
                  <th className="py-1.5 pr-3">盈亏</th><th className="py-1.5 pr-3">盈亏%</th>
                </tr>
              </thead>
              <tbody>
                {holdings.map(h => (
                  <tr key={h.code} className="border-b border-border/50">
                    <td className="py-1.5 pr-3 font-mono text-muted">{h.code}</td>
                    <td className="py-1.5 pr-3 font-semibold text-foreground">{h.name}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{h.shares}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{h.cost?.toFixed?.(2)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{h.quote_ok ? h.price?.toFixed?.(2) : <span className="text-warning">不可用</span>}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{h.market_value != null ? h.market_value.toLocaleString() : '—'}</td>
                    <td className={cn('py-1.5 pr-3 font-mono font-semibold', pctColor(h.pnl))}>{h.pnl != null ? (h.pnl > 0 ? '+' : '') + h.pnl.toLocaleString() : '—'}</td>
                    <td className={cn('py-1.5 pr-3 font-mono font-semibold', pctColor(h.pnl_pct))}>{signed(h.pnl_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="text-center text-[11px] text-muted">
        数据来自 vibe-astock（http://127.0.0.1:8910）· 点击卡片可查看 120 日 K 线
      </p>

      {kline && <KlineModal sel={kline} onClose={() => setKline(null)} />}
    </div>
  )
}

function Section({ title, hint, icon: Icon = BarChart3, badge, children }: {
  title: string; hint?: string; icon?: any; badge?: string; children: React.ReactNode
}) {
  return (
    <div className="rounded-btn border border-border bg-elevated p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon className="h-4 w-4 text-accent" />
        <span className="text-sm font-semibold text-foreground">{title}</span>
        {badge && <span className="text-[10px] text-muted">{badge}</span>}
      </div>
      {hint && <p className="mb-3 text-[11px] text-muted">{hint}</p>}
      {children}
    </div>
  )
}

/** K 线弹窗：按 kind 加载数据（index 走 TSP /api/kline/daily，yahoo 走 /api/yahoo/kline）*/
function KlineModal({ sel, onClose }: {
  sel: { title: string; symbol: string; kind: 'index' | 'yahoo' }
  onClose: () => void
}) {
  const [data, setData] = useState<OHLC[] | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    setData(null)
    setErr('')
    const loadData = async () => {
      try {
        let rows: any[] = []
        if (sel.kind === 'index') {
          const r = await vibeApi.indexKline(sel.symbol, 120)
          rows = r?.rows ?? []
        } else {
          const r = await vibeApi.yahooKline(sel.symbol, 120)
          rows = r?.rows ?? []
        }
        if (!alive) return
        const ohlc: OHLC[] = rows.map((x: any) => ({
          date: x.date,
          open: x.open, high: x.high, low: x.low, close: x.close,
          volume: x.volume ?? 0,
        }))
        setData(ohlc)
        if (!ohlc.length) setErr('无 K 线数据')
      } catch (e) {
        if (alive) setErr(`K 线加载失败: ${(e as Error).message}`)
      }
    }
    loadData()
    return () => { alive = false }
  }, [sel.symbol, sel.kind])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div className="w-full max-w-4xl rounded-xl border border-border bg-surface p-4 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-foreground">{sel.title}</h3>
          <button type="button" onClick={onClose} className="rounded-btn border border-border px-2 py-1 text-xs text-muted hover:text-foreground">
            ✕ 关闭
          </button>
        </div>
        {err ? (
          <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-6 text-center text-xs text-warning">{err}</div>
        ) : !data ? (
          <div className="flex items-center justify-center py-20 text-muted"><Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载 K 线…</div>
        ) : (
          <EChartsCandlestick data={data} height={460} showMA showInfoBar visibleBars={120} />
        )}
      </div>
    </div>
  )
}
