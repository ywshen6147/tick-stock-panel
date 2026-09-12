import { useEffect, useState } from 'react'
import { RefreshCw, Star } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { vibeApi, type MonitorSnapshot } from '@/lib/vibe'

const fmtPct = (v: number | null | undefined) =>
  v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

function pctColor(v: number | null | undefined) {
  if (v == null) return 'text-muted'
  if (v > 0) return 'text-bull'
  if (v < 0) return 'text-bear'
  return 'text-muted'
}

const KIND_STYLE: Record<string, string> = {
  急拉: 'border-bull/50 bg-bull/10 text-bull',
  急跌: 'border-bear/50 bg-bear/10 text-bear',
  封板: 'border-accent/50 bg-accent/10 text-accent',
  开板: 'border-secondary/50 bg-secondary/10 text-secondary',
}

export function VibeWatchPage() {
  const [snap, setSnap] = useState<MonitorSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState('')
  const [auto, setAuto] = useState(true)

  async function load() {
    setLoading(true)
    try {
      setSnap(await vibeApi.monitorSnapshot())
      setMsg('')
    } catch (e) {
      setMsg(`加载失败: ${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    if (!auto) return
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto])

  const rows = snap?.rows ?? []
  const watch = snap?.watch ?? []

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <PageHeader
        title="盯盘"
        subtitle="vibe-astock 实时异动流（急拉急跌/封板开板，客观事件）"
        right={
          <>
            <label className="inline-flex items-center gap-1.5 text-xs text-muted">
              <input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} className="accent-accent" />
              自动刷新(10s)
            </label>
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

      {/* 异动流 */}
      <div className="rounded-btn border border-border bg-elevated p-4">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-sm font-semibold text-foreground">异动流</span>
          <span className="text-[11px] text-muted">{rows.length} 条</span>
        </div>
        {rows.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted">
            {loading ? '加载中…' : '暂无异动（非交易时段或数据源未就绪）'}
          </div>
        ) : (
          <div className="max-h-[420px] space-y-1.5 overflow-y-auto pr-1">
            {rows.map((r, i) => (
              <div key={i} className="flex items-center gap-2 rounded-btn border border-border/50 bg-elevated px-3 py-2 text-sm">
                <span className={cn('rounded px-1.5 py-0.5 text-[11px] font-medium', KIND_STYLE[r.kind] ?? 'bg-muted text-muted')}>{r.kind}</span>
                <span className="font-semibold text-foreground">{r.name}</span>
                <span className="font-mono text-[11px] text-muted">{r.code}</span>
                <span className="font-mono text-secondary">{fmtPct(r.pct)}</span>
                <span className="ml-auto font-mono text-[11px] text-muted">{r.time}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 自选实时 */}
      {watch.length > 0 && (
        <div className="rounded-btn border border-border bg-elevated p-4">
          <div className="mb-3 flex items-center gap-1.5">
            <Star className="h-3.5 w-3.5 text-accent" />
            <span className="text-sm font-semibold text-foreground">自选实时</span>
          </div>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            {watch.map(w => (
              <div key={w.code} className="rounded-btn border border-border/60 bg-elevated px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold text-foreground">{w.name}</span>
                  <span className="font-mono text-[10px] text-muted">{w.code}</span>
                </div>
                <div className={cn('mt-1 font-mono text-base font-semibold', pctColor(w.pct))}>{fmtPct(w.pct)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <p className="text-center text-[11px] text-muted">
        仅列客观事件，不构成投资建议 · 数据来自 vibe-astock（http://127.0.0.1:8910）
      </p>
    </div>
  )
}
