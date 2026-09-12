import { useEffect, useState } from 'react'
import { Flame, Loader2, RefreshCw, Microscope } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { vibeApi } from '@/lib/vibe'

const yi = (v: number | null | undefined) => v == null ? '—' : `${(v / 1e8).toFixed(2)} 亿`
type Rec = Record<string, any>

export function VibeFirstBoardPage() {
  const [data, setData] = useState<Rec | null>(null)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState('')

  async function load() {
    setLoading(true)
    setMsg('')
    try {
      const r = await vibeApi.firstBoard()
      setData(r?.data ?? r)
    } catch (e) {
      setMsg(`加载失败: ${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const stocks = (data?.stocks ?? []) as Rec[]
  const totalZt = data?.total_zt
  const firstCount = data?.first_count

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <PageHeader
        title="首板分析"
        subtitle={data?.date ? `首板池（${data.date}）· 共 ${firstCount ?? '—'} 只首板 / 全市场涨停 ${totalZt ?? '—'} 家` : '首板池'}
        right={
          <button type="button" onClick={load} disabled={loading}
            className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground">
            <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} /> 刷新
          </button>
        }
      />

      {msg && <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{msg}</div>}
      {loading && <div className="flex items-center justify-center py-12 text-muted"><Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载中…</div>}

      {data?.reason_note && (
        <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{data.reason_note}</div>
      )}

      {/* 封板时间分布（粗分档） */}
      {stocks.length > 0 && (
        <SealTimeBreakdown stocks={stocks} />
      )}

      {/* 首板池表格 */}
      {stocks.length === 0 ? (
        !loading && <div className="rounded-btn border border-border bg-elevated py-10 text-center text-sm text-muted">暂无首板数据</div>
      ) : (
        <div className="rounded-btn border border-border bg-elevated p-4">
          <div className="mb-3 flex items-center gap-2">
            <Flame className="h-4 w-4 text-accent" />
            <span className="text-sm font-semibold text-foreground">首板池</span>
            <span className="text-[10px] text-muted">{stocks.length} 只 · 点击🔬可深挖</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="py-1.5 pr-3">代码</th><th className="py-1.5 pr-3">名称</th><th className="py-1.5 pr-3">现价</th>
                  <th className="py-1.5 pr-3">涨停%</th><th className="py-1.5 pr-3">首封</th><th className="py-1.5 pr-3">炸板</th>
                  <th className="py-1.5 pr-3">成交额</th><th className="py-1.5 pr-3">流通市值</th><th className="py-1.5 pr-3">行业</th>
                  <th className="py-1.5 pr-3">深挖</th>
                </tr>
              </thead>
              <tbody>
                {stocks.map(s => (
                  <tr key={s.code} className="border-b border-border/50">
                    <td className="py-1.5 pr-3 font-mono text-muted">{s.code}</td>
                    <td className="py-1.5 pr-3 font-semibold text-foreground">{s.name}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{s.price?.toFixed?.(2)}</td>
                    <td className="py-1.5 pr-3 font-mono text-bull">+{s.pct?.toFixed?.(2)}%</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{s.seal_time || '—'}</td>
                    <td className={cn('py-1.5 pr-3 font-mono', (s.break_count ?? 0) > 0 ? 'text-warning' : 'text-muted')}>{s.break_count ?? 0}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{yi(s.amount)}</td>
                    <td className="py-1.5 pr-3 font-mono text-secondary">{yi(s.float_cap)}</td>
                    <td className="py-1.5 pr-3 text-muted">{s.industry || '—'}</td>
                    <td className="py-1.5 pr-3">
                      <button
                        type="button"
                        title={`深挖 ${s.name}`}
                        onClick={() => window.open(`/vibe/deepdive?stock=${s.code}`, '_self')}
                        className="rounded p-1 text-muted hover:text-accent"
                      >
                        <Microscope className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <p className="text-center text-[11px] text-muted">
        首板池为客观公开数据，不构成投资建议 · 数据来自 vibe-astock（http://127.0.0.1:8910）
      </p>
    </div>
  )
}

/** 封板时间分档统计 */
function SealTimeBreakdown({ stocks }: { stocks: Rec[] }) {
  const buckets: Record<string, number> = {}
  for (const s of stocks) {
    const t = s.seal_time || '未知'
    const bucket = t <= '09:35' ? '09:30-09:35' : t <= '10:00' ? '09:36-10:00' : t <= '10:30' ? '10:01-10:30' : t <= '11:30' ? '10:31-11:30' : t <= '14:00' ? '13:01-14:00' : '14:01后'
    buckets[bucket] = (buckets[bucket] ?? 0) + 1
  }
  const order = ['09:30-09:35', '09:36-10:00', '10:01-10:30', '10:31-11:30', '13:01-14:00', '14:01后', '未知']
  const items = order.filter(k => buckets[k] != null).map(k => ({ label: k, count: buckets[k] }))
  const total = stocks.length
  return (
    <div className="rounded-btn border border-border bg-elevated p-4">
      <div className="mb-3 text-sm font-semibold text-foreground">封板时间分布</div>
      <div className="flex h-8 items-end gap-1.5">
        {items.map(it => (
          <div key={it.label} className="flex flex-1 flex-col items-center gap-0.5">
            <span className="text-[10px] font-mono text-secondary">{it.count}</span>
            <div className="w-full rounded-t bg-accent/50" style={{ height: `${Math.max(4, (it.count / total) * 56)}px` }} title={`${it.label}: ${it.count} 只`} />
            <span className="text-[9px] text-muted">{it.label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
