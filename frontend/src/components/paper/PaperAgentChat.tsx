/**
 * 模拟盘智能体「对话与提问」面板。
 *
 * 两个 Tab:
 *   1. 对话记录 —— 智能体与 LLM 的每次往返（盘中决策 / 收盘复盘 / 五派辩论），
 *      可展开看「发给模型的完整请求」与「模型回复原文」，支持渠道筛选、复制、清空。
 *   2. 我问智能体 —— 直接向该账户的智能体提问（流式回答），
 *      回答基于同一套方法论 + 该账户的记忆/知识库/持仓现状；**只做问答，不会下单**。
 *
 * 另有「召集五派辩论」按钮: 四派顾问各 3 轮讨论（12 次模型调用, 约 2~3 分钟）只出结论、
 * 不下单, 结论摘要折叠展示; 反对开新仓达阈值时后端风控闸门会禁止开仓。
 *
 * 数据源: GET /api/paper/conversation、POST /api/paper/chat/stream、
 *        GET /api/paper/llm_status（辩论状态）、POST /api/paper/debate（手动召集）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, Check, Copy, Eraser, Gavel, Loader2, MessagesSquare, RefreshCw, Send, Swords, User, ChevronDown, ChevronRight, ArrowDownToLine, ArrowUpFromLine } from 'lucide-react'
import { api, type PaperChatEvent, type PaperConversationTurn, type PaperDebateResult } from '@/lib/api'
import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { DEBATE_ANALYSTS } from '@/lib/aiModels'
import { ModelSelect } from '@/components/ModelSelect'
import { toast } from '@/components/Toast'

type Channel = 'all' | 'decision' | 'review' | 'risk' | 'chat' | 'debate'

const CHANNEL_TONE: Record<string, string> = {
  decision: 'bg-accent/10 text-accent',
  review: 'bg-amber-500/10 text-amber-500',
  // 代码兜底风控: 与"模型决策"刻意区分 — 这次没有问模型, 是代码按纪律直接清仓
  risk: 'bg-danger/10 text-danger',
  chat: 'bg-bull/10 text-bull',
  // 五派辩论: 靛紫系, 与上面的 accent/amber/danger/bull 都不同色
  debate: 'bg-indigo-500/10 text-indigo-400',
}

const CHANNEL_TEXT: Record<string, string> = {
  decision: '盘中决策',
  review: '收盘复盘',
  risk: '风控兜底',
  chat: '我的提问',
  debate: '五派辩论',
}

/** 单派动作倾向的中文口径（仅用于结论块展示，对话摘要保留 meta 原值） */
const BIAS_TEXT: Record<string, string> = {
  buy: '买入', sell: '卖出', reduce: '减仓', stop: '止损', hold: '观望',
}

function pretty(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') {
    const s = v.trim()
    if (s.startsWith('{') || s.startsWith('[')) {
      try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return v }
    }
    return v
  }
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}

function fmtTime(ts?: string): string {
  return (ts || '').slice(11, 19) || (ts || '').slice(5, 16)
}

function fmtDur(ms?: number | null): string {
  if (ms == null) return ''
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

/** 一行摘要：用于列表折叠态。 */
function summarize(turn: PaperConversationTurn): string {
  if (turn.channel === 'chat') return turn.request || '(提问)'
  const m = turn.meta || {}
  if (turn.channel === 'risk') {
    const sym = String(m.symbol || '')
    const qty = m.qty == null ? '' : `${m.qty}股`
    const reason = String(m.reason || '')
    return ['代码兜底·未调用模型', sym, qty, reason].filter(Boolean).join(' · ') || '(风控兜底)'
  }
  if (turn.channel === 'decision') {
    const act = String(m.action || '')
    const sym = String(m.symbol || '')
    const reason = String(m.reason || '')
    return [act && `动作 ${act}`, sym, reason].filter(Boolean).join(' · ') || '(决策)'
  }
  if (turn.channel === 'debate') {
    // 五派辩论: 每派每轮一条, 摘要按「第N轮 · 顾问 · 立场 · 倾向」给出
    const parts = [
      `第${m.round ?? '?'}轮`,
      String(m.analyst_name || m.analyst || '(未知顾问)'),
      String(m.stance || ''),
      String(m.action_bias || ''),
    ].filter(Boolean)
    if (m.oppose_new_position) parts.push('反对开新仓')
    if (m.degraded) parts.push('未给结论')
    return parts.join(' · ')
  }
  const parsed = (turn.parsed || {}) as Record<string, unknown>
  return String(parsed.summary || m.date || '(复盘)')
}

function CopyBtn({ text, label = '复制' }: { text: string; label?: string }) {
  return (
    <button
      onClick={async () => { if (await copyText(text)) toast(`${label}成功`) }}
      className="flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:text-accent"
    >
      <Copy className="h-2.5 w-2.5" /> {label}
    </button>
  )
}

function TurnRow({ acc, turn }: { acc: string; turn: PaperConversationTurn }) {
  const [open, setOpen] = useState(false)
  const full = useQuery({
    queryKey: ['paper', 'conversation', acc, 'turn', turn.id],
    queryFn: () => api.paperConversationTurn(turn.id, acc),
    enabled: open && turn.request == null,   // 列表里没带请求体 → 展开时再取全文
    staleTime: 300_000,
  })
  const detail: PaperConversationTurn = full.data?.turn ?? turn
  const failed = !!turn.error
  /** 风控兜底: 没调用模型, 分区标题与字数说明都要改口径, 不能写"发给模型/模型回复" */
  const isRisk = turn.channel === 'risk'
  /** 五派辩论: 同样是"给了模型一份事实 + 一个顾问发言", 但语义不是"发给模型/模型回复" */
  const isDebate = turn.channel === 'debate'
  return (
    <div className={cn('rounded-btn border bg-base/30', failed ? 'border-bear/30' : 'border-border/50')}>
      <button onClick={() => setOpen(v => !v)} className="flex w-full items-center gap-1.5 px-2 py-1 text-left">
        {open ? <ChevronDown className="h-3 w-3 shrink-0 text-muted" /> : <ChevronRight className="h-3 w-3 shrink-0 text-muted" />}
        <span className="shrink-0 font-mono text-[10px] text-muted">{fmtTime(turn.ts)}</span>
        <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[10px]', CHANNEL_TONE[turn.channel] ?? 'bg-elevated text-secondary')}>
          {CHANNEL_TEXT[turn.channel] ?? turn.channel}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-secondary">{summarize(turn)}</span>
        {turn.elapsed_ms != null && <span className="shrink-0 font-mono text-[10px] text-muted">{fmtDur(turn.elapsed_ms)}</span>}
        {failed && <span className="shrink-0 rounded bg-bear/10 px-1 py-0.5 text-[10px] text-bear">失败</span>}
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-border/40 px-2 py-1.5">
          {full.isLoading && <div className="flex items-center gap-1 text-[10px] text-muted"><Loader2 className="h-3 w-3 animate-spin" /> 加载完整请求…</div>}
          <div>
            <div className="mb-0.5 flex items-center gap-1.5">
              <ArrowUpFromLine className="h-3 w-3 text-accent" />
              <span className="text-[10px] font-medium text-accent">
                {isRisk ? '触发时的盘口快照' : isDebate ? '该派看到的事实' : '发给模型'}
              </span>
              <span className="text-[10px] text-muted">
                {isRisk
                  ? '本次未调用模型（代码按纪律直接清仓）'
                  : detail.request
                    ? `${(detail.request as string).length.toLocaleString()} 字符`
                    : `${(detail.request_chars ?? 0).toLocaleString()} 字符`}
                {!isRisk && detail.system_chars ? ` · 系统提示词 ${detail.system_chars.toLocaleString()} 字符（固定方法论）` : ''}
              </span>
              {detail.request && <span className="ml-auto"><CopyBtn text={detail.request} /></span>}
            </div>
            <pre className="max-h-56 overflow-auto rounded border border-border/40 bg-base/60 p-1.5 text-[10px] leading-relaxed text-muted whitespace-pre-wrap break-all">
              {detail.request ? pretty(detail.request) : '（列表视图未包含，点开本条会自动加载）'}
            </pre>
          </div>
          <div>
            <div className="mb-0.5 flex items-center gap-1.5">
              <ArrowDownToLine className="h-3 w-3 text-bull" />
              <span className="text-[10px] font-medium text-bull">
                {isRisk ? '代码判定理由' : isDebate ? '该派发言' : '模型回复'}
              </span>
              {turn.error && <span className="text-[10px] text-bear">错误：{turn.error}</span>}
              {!!detail.response && <span className="ml-auto"><CopyBtn text={detail.response} /></span>}
            </div>
            <pre className="max-h-56 overflow-auto rounded border border-border/40 bg-base/60 p-1.5 text-[10px] leading-relaxed text-secondary whitespace-pre-wrap break-words">
              {detail.response ? pretty(detail.response) : '（空响应）'}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

interface ChatMsg { role: 'user' | 'assistant'; content: string; model?: string; error?: boolean }

function ChatTab({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const [input, setInput] = useState('')
  const [msgs, setMsgs] = useState<ChatMsg[]>([])
  const [busy, setBusy] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => { setMsgs([]); setInput('') }, [acc])
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [msgs])

  const send = useCallback(async () => {
    const q = input.trim()
    if (!q || busy) return
    setInput('')
    setBusy(true)
    setMsgs(m => [...m, { role: 'user', content: q }, { role: 'assistant', content: '' }])
    try {
      for await (const ev of api.paperChatStream(acc, q) as AsyncGenerator<PaperChatEvent>) {
        if (ev.type === 'meta') {
          setMsgs(m => {
            const next = [...m]
            next[next.length - 1] = { ...next[next.length - 1], model: ev.model }
            return next
          })
        } else if (ev.type === 'delta') {
          setMsgs(m => {
            const next = [...m]
            const last = next[next.length - 1]
            next[next.length - 1] = { ...last, content: last.content + (ev.content ?? '') }
            return next
          })
        } else if (ev.type === 'error') {
          setMsgs(m => {
            const next = [...m]
            const last = next[next.length - 1]
            next[next.length - 1] = { ...last, content: last.content || ev.message || '调用失败', error: true }
            return next
          })
          toast(ev.message || 'AI 调用失败', 'error')
        }
      }
    } catch (e) {
      setMsgs(m => {
        const next = [...m]
        const last = next[next.length - 1]
        next[next.length - 1] = { ...last, content: last.content || String(e), error: true }
        return next
      })
      toast(String(e), 'error')
    } finally {
      setBusy(false)
      // 提问后刷新对话记录，让「看他们对话」Tab 立刻能看到这一问一答
      qc.invalidateQueries({ queryKey: ['paper', 'conversation', acc] })
    }
  }, [acc, busy, input, qc])

  return (
    <div className="mt-2">
      <div
        ref={scrollRef}
        className="max-h-72 min-h-24 space-y-2 overflow-y-auto rounded-btn border border-border/50 bg-base/30 p-2"
      >
        {msgs.length === 0 && (
          <div className="py-6 text-center text-[11px] leading-relaxed text-muted">
            直接问它：<br />
            「今天为什么没买？」「688553 现在这个位置怎么看？」「我现在的持仓风险大吗？」
            <div className="mt-1 text-[10px] text-muted/70">
              回答会结合方法论、你的记忆与持仓现状；只做分析问答，不会自动下单。
            </div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={cn('flex gap-1.5', m.role === 'user' ? 'justify-end' : 'justify-start')}>
            {m.role === 'assistant' && <Bot className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />}
            <div className={cn(
              'max-w-[85%] rounded-btn px-2 py-1.5 text-[11px] leading-relaxed whitespace-pre-wrap break-words',
              m.role === 'user' ? 'bg-accent/15 text-foreground'
                : m.error ? 'border border-bear/30 bg-bear/10 text-bear'
                  : 'border border-border/50 bg-elevated/40 text-secondary',
            )}>
              {m.content || (busy && m.role === 'assistant' ? <Loader2 className="h-3 w-3 animate-spin" /> : '')}
              {m.role === 'assistant' && m.model && (
                <div className="mt-1 text-[9px] text-muted/70">{m.model}</div>
              )}
            </div>
            {m.role === 'user' && <User className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />}
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex items-end gap-1.5">
        <textarea
          rows={2}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() }
          }}
          placeholder="问智能体… （Enter 发送，Shift+Enter 换行）"
          className="w-full resize-y rounded-btn border border-border bg-elevated px-2 py-1 text-xs text-secondary outline-none focus:border-accent"
        />
        <button
          onClick={() => void send()}
          disabled={busy || !input.trim()}
          className="flex shrink-0 items-center gap-1 rounded-btn bg-accent/10 px-3 py-1.5 text-xs text-accent transition-colors hover:bg-accent/20 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
          {busy ? '思考中' : '发送'}
        </button>
      </div>
    </div>
  )
}

/** 五派辩论结论块（手动召集后展示）: 共识 + 闸门 + 各派立场 + 纪要全文，可折叠。 */
function DebateResult({ result }: { result: PaperDebateResult }) {
  const [open, setOpen] = useState(true)
  const [full, setFull] = useState(false)
  const gate = result.gate
  const blocked = !!gate && !gate.allow_open
  return (
    <div className={cn('mt-2 rounded-btn border', blocked ? 'border-danger/40 bg-danger/5' : 'border-indigo-500/30 bg-indigo-500/5')}>
      <div className="flex items-center gap-1.5 px-2 py-1.5">
        <button onClick={() => setOpen(v => !v)} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
          {open ? <ChevronDown className="h-3 w-3 shrink-0 text-muted" /> : <ChevronRight className="h-3 w-3 shrink-0 text-muted" />}
          <Gavel className="h-3 w-3 shrink-0 text-indigo-400" />
          <span className="shrink-0 text-[10px] font-medium text-indigo-400">五派辩论结论</span>
          <span className="min-w-0 flex-1 truncate text-[11px] text-secondary">{result.consensus || '（无汇总）'}</span>
        </button>
        {!!result.degraded && (
          <span className="shrink-0 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-500">
            {result.degraded} 派未给结论
          </span>
        )}
        {gate && (
          <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[10px]',
            gate.allow_open ? 'bg-bull/10 text-bull' : 'bg-danger/10 text-danger')}>
            {gate.allow_open
              ? `闸门未触发（反对 ${gate.oppose_count}/${gate.threshold}）`
              : `禁止开新仓（反对 ${gate.oppose_count}/${gate.threshold}）`}
          </span>
        )}
      </div>
      {open && (
        <div className="space-y-1.5 border-t border-border/40 px-2 py-1.5">
          {gate && !gate.allow_open && gate.reason && (
            <div className="rounded border border-danger/30 bg-danger/10 px-1.5 py-1 text-[10px] leading-relaxed text-danger">
              {gate.reason}
            </div>
          )}
          <div className="flex flex-wrap gap-1">
            {(result.statements ?? []).map((s, i) => (
              <span key={i} className={cn('rounded border px-1.5 py-0.5 text-[10px]',
                s.oppose_new_position ? 'border-danger/30 bg-danger/10 text-danger' : 'border-border/50 bg-elevated/40 text-secondary')}
                title={(s.points ?? []).join('\n') || s.error || ''}>
                {s.name}
                {s.stance ? ` · ${s.stance}` : ''}
                {s.action_bias ? ` · ${BIAS_TEXT[s.action_bias] || s.action_bias}` : ''}
                {s.oppose_new_position ? ' · 反对开新仓' : ''}
                {s.error ? ' · 未给结论' : ''}
              </span>
            ))}
          </div>
          {!!result.summary && (
            <div>
              <div className="mb-0.5 flex items-center gap-1.5">
                <span className="text-[10px] text-muted">辩论纪要（纯文本）</span>
                <span className="ml-auto"><CopyBtn text={result.summary} label="复制纪要" /></span>
              </div>
              <pre className={cn('overflow-auto rounded border border-border/40 bg-base/60 p-1.5 text-[10px] leading-relaxed text-muted whitespace-pre-wrap break-words',
                full ? 'max-h-96' : 'max-h-28')}>
                {result.summary}
              </pre>
              <button onClick={() => setFull(v => !v)} className="mt-0.5 text-[10px] text-muted transition-colors hover:text-accent">
                {full ? '收起纪要' : '展开全文'}
              </button>
            </div>
          )}
          <div className="text-[10px] text-muted/70">
            仅出结论、不会下单；轮数 {result.rounds}，本次记录的每派每轮发言已写入下方「五派辩论」档。
          </div>
        </div>
      )}
    </div>
  )
}

export function PaperAgentChat({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'log' | 'ask'>('log')
  const [channel, setChannel] = useState<Channel>('all')
  /** 本次会话里手动召集得到的结论（刷新页面即丢，历史留痕在「五派辩论」档） */
  const [debateResult, setDebateResult] = useState<PaperDebateResult | null>(null)

  const q = useQuery({
    queryKey: ['paper', 'conversation', acc, channel],
    queryFn: () => api.paperConversation(acc, channel === 'all' ? undefined : channel, 60, false),
    refetchInterval: tab === 'log' ? 20_000 : false,
  })
  const turns = q.data?.turns ?? []
  const stats = q.data?.stats
  const clearM = useMutation({
    mutationFn: () => api.paperConversationClear(acc),
    onSuccess: (r) => { toast(`已清空 ${r.removed} 条对话记录`); qc.invalidateQueries({ queryKey: ['paper', 'conversation', acc] }) },
  })

  // 五派辩论状态（开关 / 轮数 / 当日用量）——与模拟盘页面共用 queryKey, 召集后一起刷新
  const llmQ = useQuery({
    queryKey: ['paper', 'llmStatus', acc],
    queryFn: () => api.paperLlmStatus(acc),
  })
  const debate = llmQ.data?.debate
  /** 辩论轮数（1~3）: 每轮每派一次模型调用, 故总调用数 = 轮数 × 4 派 */
  const debateRounds = debate?.rounds ?? 3

  // ── 头部「逐派模型」快捷编辑器 ───────────────────────────────
  // 用 models_set（显式设置的原值）回填, **不能**用 models（生效值）—— 否则保存时会把
  // 4 条都写成显式覆盖, 破坏"留空即跟随统一值"的语义。
  const [modelsOpen, setModelsOpen] = useState(false)
  const [draftModels, setDraftModels] = useState<Record<string, string>>({})
  const modelsSet = debate?.models_set
  useEffect(() => {
    setDraftModels({
      caishen: modelsSet?.caishen ?? '',
      chanlun: modelsSet?.chanlun ?? '',
      canslim: modelsSet?.canslim ?? '',
      kxian: modelsSet?.kxian ?? '',
    })
  }, [modelsSet?.caishen, modelsSet?.chanlun, modelsSet?.canslim, modelsSet?.kxian])

  const unifiedModel = debate?.model || ''
  const globalModel = llmQ.data?.ai_model_default || ''
  const customCount = DEBATE_ANALYSTS.filter(a => (draftModels[a.id] ?? '').trim()).length

  // 传参式 mutation: 「选完即存」和「点保存」共用同一条写入路径
  const modelsM = useMutation({
    mutationFn: (next: Record<string, string>) => api.paperSettings({
      llm_debate_model_caishen: (next.caishen ?? '').trim(),
      llm_debate_model_chanlun: (next.chanlun ?? '').trim(),
      llm_debate_model_canslim: (next.canslim ?? '').trim(),
      llm_debate_model_kxian: (next.kxian ?? '').trim(),
    }, acc),
    onSuccess: () => {
      toast('逐派模型已保存（下一轮辩论即生效）')
      qc.invalidateQueries({ queryKey: ['paper', 'llmStatus', acc] })
    },
    onError: (e) => toast(`保存逐派模型失败: ${String(e)}`, 'error'),
  })
  /** 下拉改一个就立即落盘 —— 避免"改完忘了点保存"以为没生效 */
  const pickModel = (id: string, v: string) => {
    const next = { ...draftModels, [id]: v }
    setDraftModels(next)
    modelsM.mutate(next)
  }

  /** 手动召集五派辩论: 4 派 × 轮数 次模型调用, 约 2~3 分钟 —— 只出结论, 不下单。 */
  const debateM = useMutation({
    mutationFn: () => api.paperDebateRun(acc, debateRounds),
    onSuccess: (r) => {
      setDebateResult(r)
      setTab('log')
      setChannel('debate')   // 直接切到辩论档, 让 12 条新留痕可见
      const gate = r.gate
      const tail = gate
        ? (gate.allow_open
          ? `闸门未触发（反对 ${gate.oppose_count}/${gate.threshold}）`
          : `⚠ ${gate.reason || `禁止开新仓（反对 ${gate.oppose_count}/${gate.threshold}）`}`)
        : ''
      toast([r.consensus || '五派辩论已完成', tail].filter(Boolean).join(' · '),
        gate && !gate.allow_open ? 'error' : 'success')
      qc.invalidateQueries({ queryKey: ['paper', 'conversation', acc] })
      qc.invalidateQueries({ queryKey: ['paper', 'llmStatus', acc] })
    },
    onError: (e) => toast(`召集五派辩论失败: ${String(e)}`, 'error'),
  })

  const channels: { key: Channel; label: string; count?: number }[] = useMemo(() => ([
    { key: 'all', label: '全部', count: stats?.total },
    { key: 'decision', label: '盘中决策', count: stats?.by_channel?.decision },
    { key: 'risk', label: '风控兜底', count: stats?.by_channel?.risk },
    { key: 'review', label: '收盘复盘', count: stats?.by_channel?.review },
    { key: 'debate', label: '五派辩论', count: stats?.by_channel?.debate },
    { key: 'chat', label: '我的提问', count: stats?.by_channel?.chat },
  ]), [stats])

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <MessagesSquare className="h-4 w-4 text-accent" />
        <span className="text-sm font-medium">智能体对话</span>
        <span className="rounded bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-muted" title="面板内容属于当前选中的虚拟账户">
          账户 {acc}
        </span>
        <span className="text-[10px] text-muted">看它和模型说了什么，也能直接问它</span>
        {debate && (
          <button
            onClick={() => setModelsOpen(v => !v)}
            className={cn('rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors',
              modelsOpen ? 'bg-accent/15 text-accent' : 'bg-elevated text-muted hover:text-accent')}
            title="逐派指定模型：留空即跟随「四派统一」，再跟随全局。改完点保存立即生效（下一轮辩论用新模型）。"
          >
            逐派模型 {customCount}/4{customCount ? ' 自定义' : ''}
          </button>
        )}
        {debate && (
          <span
            className={cn('rounded px-1.5 py-0.5 font-mono text-[10px]',
              debate.enabled ? 'bg-indigo-500/10 text-indigo-400' : 'bg-elevated text-muted')}
            title={[
              `顾问: ${(debate.analysts ?? []).map(a => `${a.name}(${a.school ?? ''})`).join('、')}`,
              `风控闸门: ${debate.gate_threshold} 派反对开新仓即禁止开仓`,
              debate.last_at ? `上次召集 ${fmtTime(debate.last_at)}` : '',
              debate.last_oppose != null ? `上次反对 ${debate.last_oppose} 派` : '',
              debate.last_summary ? `上次结论: ${debate.last_summary}` : '',
            ].filter(Boolean).join('\n')}
          >
            五派辩论 {debate.enabled ? '开' : '关'} · {debate.rounds} 轮 · 今日 {debate.runs_today}/{debate.daily_max}
          </span>
        )}
        <div className="ml-auto inline-flex overflow-hidden rounded-btn border border-border">
          {([['log', '对话记录'], ['ask', '我问智能体']] as const).map(([k, label]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={cn('px-2.5 py-1 text-xs transition-colors',
                tab === k ? 'bg-accent/15 font-medium text-accent' : 'text-muted hover:text-secondary')}
            >
              {label}
            </button>
          ))}
        </div>
        {tab === 'log' && (
          <>
            <button
              onClick={() => qc.invalidateQueries({ queryKey: ['paper', 'conversation', acc] })}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:text-accent"
            >
              <RefreshCw className={cn('h-3 w-3', q.isFetching && 'animate-spin')} /> 刷新
            </button>
            <button
              onClick={() => { if (confirm('清空该账户的全部对话记录？（不影响交易与记忆）')) clearM.mutate() }}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:text-bear"
            >
              <Eraser className="h-3 w-3" /> 清空
            </button>
          </>
        )}
        <button
          onClick={() => debateM.mutate()}
          disabled={debateM.isPending || debate?.enabled === false}
          title="四派顾问各 3 轮讨论后交海平分时擒牛分析师终判；只出结论，不会下单"
          className="flex items-center gap-1 rounded-btn border border-indigo-500/40 bg-indigo-500/10 px-2 py-1 text-[11px] text-indigo-400 transition-colors hover:bg-indigo-500/20 disabled:opacity-50"
        >
          {debateM.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Swords className="h-3 w-3" />}
          {debateM.isPending
            ? `五派辩论中…（4 派 × ${debateRounds} 轮，约 2~3 分钟）`
            : '召集五派辩论'}
        </button>
      </div>

      {modelsOpen && debate && (
        <div className="mt-2 rounded-btn border border-accent/25 bg-accent/[0.03] px-3 py-2">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-[11px] font-medium text-accent">四派顾问逐派模型</span>
            <span className="text-[10px] text-muted">
              留空 = 跟随「四派统一」（当前 <span className="font-mono">{unifiedModel || globalModel || '全局'}</span>）；
              选完即自动保存，下一轮辩论立即生效。
            </span>
            <div className="ml-auto flex items-center gap-1">
              <button
                onClick={() => {
                  // 加了确认: 这个按钮会**立即**把四派模型全清成"跟随统一", 误点一次就丢配置
                  if (!confirm('清空四派各自的模型设置，全部改为跟随「四派统一」？')) return
                  const empty = { caishen: '', chanlun: '', canslim: '', kxian: '' }
                  setDraftModels(empty)
                  modelsM.mutate(empty)
                }}
                className="rounded-btn border border-border px-2 py-0.5 text-[10px] text-muted transition-colors hover:text-secondary"
              >
                全部清空（跟随统一）
              </button>
              <button
                onClick={() => modelsM.mutate(draftModels)}
                disabled={modelsM.isPending}
                className="flex items-center gap-1 rounded-btn border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-50"
              >
                {modelsM.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
                保存
              </button>
            </div>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {DEBATE_ANALYSTS.map(a => {
              const eff = debate.models?.[a.id] || unifiedModel || globalModel
              return (
                <label key={a.id} className="block">
                  <span className="text-[10px] text-muted" title={`${a.name} · 生效模型 ${eff || '全局'}`}>
                    {a.short}
                  </span>
                  <ModelSelect
                    value={draftModels[a.id] ?? ''}
                    onChange={v => pickModel(a.id, v)}
                    fallbackLabel={`跟随统一（${unifiedModel || globalModel || '全局'}）`}
                    title={`${a.name} · 生效模型 ${eff || '全局'}`}
                    className="mt-0.5 w-full"
                  />
                </label>
              )
            })}
          </div>
          <div className="mt-1 text-[10px] leading-relaxed text-muted/80">
            建议按"轻重"分派：K线形态识别用 <span className="font-mono">doubao-seed-2.0-lite</span>（快），
            缠论结构推演用 <span className="font-mono">doubao-seed-2.1-pro</span>，
            蔡森 / CAN SLIM 用 <span className="font-mono">deepseek-v4-pro</span>。一轮 3 轮辩论共 12 次调用，差别在耗时与效果上都能看出来。
          </div>
        </div>
      )}

      {tab === 'ask' ? <ChatTab acc={acc} /> : (
        <>
          {debateM.isPending && (
            <div className="mt-2 flex items-center gap-1.5 rounded-btn border border-indigo-500/30 bg-indigo-500/5 px-2 py-1.5 text-[10px] leading-relaxed text-indigo-400">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
              正在依次召集四派顾问做 {debateRounds} 轮讨论（共 {debateRounds * 4} 次模型调用，实测约 2~3 分钟），期间请勿关闭页面；只出结论，不会下单。
            </div>
          )}
          {debateResult && <DebateResult result={debateResult} />}
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {channels.map(c => (
              <button
                key={c.key}
                onClick={() => setChannel(c.key)}
                className={cn('rounded-btn px-2 py-0.5 text-[11px] transition-colors',
                  channel === c.key ? 'bg-accent/15 text-accent' : 'text-muted hover:text-secondary')}
              >
                {c.label}{c.count != null ? ` ${c.count}` : ''}
              </button>
            ))}
            {stats?.last_ts && <span className="ml-auto text-[10px] text-muted">最近 {fmtTime(stats.last_ts)}</span>}
          </div>
          <div className="mt-1.5 max-h-[26rem] space-y-1 overflow-y-auto pr-0.5">
            {q.isLoading && <div className="py-6 text-center text-[11px] text-muted"><Loader2 className="mx-auto h-4 w-4 animate-spin" /></div>}
            {!q.isLoading && turns.length === 0 && (
              <div className="py-6 text-center text-[11px] leading-relaxed text-muted">
                账户 <span className="font-mono text-secondary">{acc}</span> 暂无对话记录。<br />
                <span className="text-[10px] text-muted/70">
                  盘中 LLM 跟单决策与收盘复盘会自动留痕；切到「我问智能体」也能立刻产生对话。<br />
                  「五派辩论」档由右上角「召集五派辩论」按钮触发（只出结论，不会下单）。<br />
                  若你刚在别的账户里生成过记录，请用右上角账户下拉切换到对应账户（如「海平分时擒牛·模拟盘」）。
                </span>
              </div>
            )}
            {turns.map(t => <TurnRow key={t.id} acc={acc} turn={t} />)}
          </div>
        </>
      )}
    </div>
  )
}


