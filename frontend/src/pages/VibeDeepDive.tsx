import { useCallback, useEffect, useState } from 'react'
import { Loader2, Play, RefreshCw } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { vibeApi, type DeepDiveData } from '@/lib/vibe'

/** 把 markdown 段粗略渲染（与复盘页共用风格） */
function MarkdownLite({ md }: { md: string }) {
  const blocks = md.split(/\n{2,}/).filter(Boolean)
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      {blocks.map((b, i) => {
        if (/^#{1,3}\s/.test(b)) return <h3 key={i} className="text-base font-semibold text-foreground">{b.replace(/^#{1,3}\s/, '')}</h3>
        if (b.split('\n').every(l => /^[-*•]\s/.test(l.trim()))) {
          return (
            <ul key={i} className="list-disc space-y-1 pl-5">
              {b.split('\n').map((l, j) => <li key={j} className="text-secondary">{l.replace(/^[-*•]\s/, '')}</li>)}
            </ul>
          )
        }
        return <p key={i} className="text-secondary">{b}</p>
      })}
    </div>
  )
}

export function VibeDeepDivePage() {
  const [data, setData] = useState<DeepDiveData | null>(null)
  const [stock, setStock] = useState('')
  const [running, setRunning] = useState(false)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState('')

  const loadLatest = useCallback(() => {
    setLoading(true)
    setMsg('')
    vibeApi.deepdiveLatest()
      .then(d => { if (d?.code) setData(d) })
      .catch(e => setMsg(`读取上次深挖失败: ${e.message}`))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { loadLatest() }, [loadLatest])

  async function run() {
    const s = stock.trim()
    if (!s) { setMsg('请输入股票代码'); return }
    setRunning(true)
    setMsg('')
    try {
      const st = await vibeApi.deepdiveRun(s)
      if (st.running) {
        setMsg(`深挖已启动（约 2-3 分钟），后台轮询结果…`)
        const timer = setInterval(async () => {
          try {
            const js = await vibeApi.deepdiveStatus()
            if (!js.running) {
              clearInterval(timer)
              setRunning(false)
              loadLatest()
            }
          } catch { /* ignore */ }
        }, 10000)
      } else {
        setRunning(false)
        loadLatest()
      }
    } catch (e) {
      setRunning(false)
      setMsg(`启动深挖失败: ${(e as Error).message}`)
    }
  }

  const v = data?.verdict

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <PageHeader
        title="个股深挖"
        subtitle="多 agent 深度分析（题材/资金/技术/风险 + 正反辩论），仅客观陈述"
        right={
          <div className="flex items-center gap-2">
            <input
              value={stock}
              onChange={e => setStock(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && run()}
              placeholder="股票代码，如 600519"
              className="h-8 w-32 rounded-btn border border-border bg-elevated px-2 font-mono text-xs text-secondary placeholder:text-muted"
            />
            <button
              type="button"
              onClick={run}
              disabled={running}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-2.5 text-xs text-white hover:bg-accent/90"
            >
              {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              {running ? '深挖中…' : '深挖'}
            </button>
            <button
              type="button"
              onClick={loadLatest}
              disabled={loading}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} /> 刷新
            </button>
          </div>
        }
      />

      {msg && <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{msg}</div>}

      {loading ? (
        <div className="flex items-center justify-center py-10 text-muted"><Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载中…</div>
      ) : data?.code ? (
        <>
          {/* 标题 */}
          <div className="rounded-btn border border-border bg-elevated p-4">
            <div className="flex items-baseline gap-3">
              <span className="text-lg font-semibold text-foreground">{data.name}</span>
              <span className="font-mono text-xs text-muted">{data.code}</span>
              {data.trade_date && <span className="text-xs text-muted">{data.trade_date}</span>}
            </div>
            {v?.one_liner && <p className="mt-2 text-sm text-secondary">{v.one_liner}</p>}
          </div>

          {/* 四维结论 */}
          <div className="grid gap-3 md:grid-cols-2">
            <AnalysisCard title="题材" text={v?.theme} />
            <AnalysisCard title="资金" text={v?.capital} />
            <AnalysisCard title="技术" text={v?.technical} />
            <div className="rounded-btn border border-border bg-elevated p-4">
              <div className="mb-2 text-sm font-semibold text-foreground">风险</div>
              {(v?.risks?.length ?? 0) > 0 ? (
                <ul className="list-disc space-y-1 pl-5 text-sm text-warning">
                  {v!.risks!.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              ) : <div className="text-sm text-muted">—</div>}
            </div>
          </div>

          {/* 完整报告 */}
          {data.verdict_md && (
            <div className="rounded-btn border border-border bg-elevated p-4">
              <div className="mb-2 text-sm font-semibold text-foreground">完整研判</div>
              <MarkdownLite md={data.verdict_md} />
            </div>
          )}
        </>
      ) : (
        <div className="rounded-btn border border-border bg-elevated py-10 text-center text-sm text-muted">
          暂无深挖结果。输入代码点击「深挖」（约 2-3 分钟）。
        </div>
      )}

      <p className="text-center text-[11px] text-muted">
        AI 结论仅供参考，不构成投资建议 · 数据来自 vibe-astock
      </p>
    </div>
  )
}

function AnalysisCard({ title, text }: { title: string; text?: string }) {
  return (
    <div className="rounded-btn border border-border bg-elevated p-4">
      <div className="mb-2 text-sm font-semibold text-foreground">{title}</div>
      {text ? <p className="text-sm leading-relaxed text-secondary">{text}</p> : <div className="text-sm text-muted">—</div>}
    </div>
  )
}
