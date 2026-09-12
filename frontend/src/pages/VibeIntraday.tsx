import { useEffect, useState } from 'react'
import { Loader2, RefreshCw, Camera } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { vibeApi, type AuctionData, type DayPathData } from '@/lib/vibe'

const fmtPct = (v: number | null | undefined) =>
  v == null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(2)}%`

function pctColor(v: number | null | undefined) {
  if (v == null) return 'text-muted'
  if (v > 0) return 'text-bull'
  if (v < 0) return 'text-bear'
  return 'text-muted'
}

const SLOT_LABEL: Record<string, string> = {
  '09:25': '09:25 竞价',
  '09:35': '09:35 开盘',
  '10:00': '10:00 早盘',
  '11:30': '11:30 午休',
  '14:00': '14:00 午后',
  '15:00': '15:00 收盘',
}

export function VibeIntradayPage() {
  const [auction, setAuction] = useState<AuctionData | null>(null)
  const [path, setPath] = useState<DayPathData | null>(null)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState('')

  async function load() {
    setLoading(true)
    setMsg('')
    try {
      const [a, p] = await Promise.all([vibeApi.auction(), vibeApi.intradayPath()])
      setAuction(a)
      setPath(p)
      if (!a.available && a.reason) setMsg(`竞价核验: ${a.reason}`)
      if (!p.available && p.reason) setMsg(m => m ? `${m}；盘中路径: ${p.reason}` : `盘中路径: ${p.reason}`)
    } catch (e) {
      setMsg(`加载失败: ${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function capture() {
    setMsg('')
    try {
      await vibeApi.intradayCapture()
      setMsg('已触发当前时点快照抓取，稍后刷新查看')
    } catch (e) {
      setMsg(`抓取失败: ${(e as Error).message}`)
    }
  }

  const tiers = auction?.by_tier
  const tierEntries = tiers ? Object.entries(tiers).sort() : []
  const points = path?.points ?? []

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <PageHeader
        title="开盘核验"
        subtitle="竞价核验 + 盘中情绪路径（vibe-astock）"
        right={
          <>
            <button
              type="button"
              onClick={capture}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground"
            >
              <Camera className="h-3.5 w-3.5" /> 抓当前快照
            </button>
            <button
              type="button"
              onClick={load}
              disabled={loading}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} /> 刷新
            </button>
          </>
        }
      />

      {msg && <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{msg}</div>}
      {loading && <div className="flex items-center justify-center py-10 text-muted"><Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载中…</div>}

      {/* 竞价总览 */}
      {auction?.overall && (
        <div className="rounded-btn border border-border bg-elevated p-4">
          <div className="mb-3 text-sm font-semibold text-foreground">竞价总览 {auction.date ? `(${auction.date})` : ''}</div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <MiniStat label="样本" value={String(auction.overall.sample)} />
            <MiniStat label="高开率" value={fmtPct(auction.overall.up_rate)} valueClass={pctColor(auction.overall.up_rate)} />
            <MiniStat label="均值" value={fmtPct(auction.overall.avg)} valueClass={pctColor(auction.overall.avg)} />
            <MiniStat label="中位数" value={fmtPct(auction.overall.median)} valueClass={pctColor(auction.overall.median)} />
            <MiniStat label="跌超5%" value={String(auction.overall.deep_loss_5)} />
          </div>
        </div>
      )}

      {/* 分板位竞价 */}
      {tierEntries.length > 0 && (
        <div className="rounded-btn border border-border bg-elevated p-4">
          <div className="mb-3 text-sm font-semibold text-foreground">分板位竞价</div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="py-1.5 pr-3">板位</th>
                  <th className="py-1.5 pr-3">样本</th>
                  <th className="py-1.5 pr-3">均值</th>
                  <th className="py-1.5 pr-3">中位数</th>
                  <th className="py-1.5 pr-3">高开率</th>
                </tr>
              </thead>
              <tbody>
                {tierEntries.map(([k, v]) => (
                  <tr key={k} className="border-b border-border/50">
                    <td className="py-1.5 pr-3 text-secondary">{k}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{v.sample}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(v.avg))}>{fmtPct(v.avg)}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(v.median))}>{fmtPct(v.median)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{fmtPct(v.up_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 盘中情绪路径 */}
      {points.length > 0 && (
        <div className="rounded-btn border border-border bg-elevated p-4">
          <div className="mb-3 text-sm font-semibold text-foreground">盘中情绪路径</div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="py-1.5 pr-3">时点</th>
                  <th className="py-1.5 pr-3">中位数</th>
                  <th className="py-1.5 pr-3">均值</th>
                  <th className="py-1.5 pr-3">高开率</th>
                  <th className="py-1.5 pr-3">跌超5%</th>
                  <th className="py-1.5 pr-3">涨停/炸板/跌停</th>
                </tr>
              </thead>
              <tbody>
                {points.map(p => (
                  <tr key={p.slot} className="border-b border-border/50">
                    <td className="py-1.5 pr-3 text-secondary">{SLOT_LABEL[p.slot] ?? p.slot}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(p.median))}>{fmtPct(p.median)}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', pctColor(p.avg))}>{fmtPct(p.avg)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{fmtPct(p.up_rate)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{p.deep_loss_5}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">
                      {p.live ? `${p.live.limit_up}/${p.live.broken}/${p.live.limit_down}` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 竞价最高标 */}
      {auction?.top_boards && auction.top_boards.length > 0 && (
        <div className="rounded-btn border border-border bg-elevated p-4">
          <div className="mb-3 text-sm font-semibold text-foreground">竞价最强标的（客观读数）</div>
          <div className="flex flex-wrap gap-2">
            {auction.top_boards.map(b => (
              <div key={b.code} className="rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs">
                <span className="font-semibold text-foreground">{b.name}</span>
                <span className="ml-1.5 text-muted">{b.code}</span>
                <span className="ml-1.5 text-secondary">{b.boards}板</span>
                <span className={cn('ml-1.5 font-mono', pctColor(b.pct))}>{fmtPct(b.pct)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function MiniStat({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-btn border border-border/60 bg-elevated px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cn('mt-0.5 font-mono text-base font-semibold', valueClass ?? 'text-foreground')}>{value}</div>
    </div>
  )
}
