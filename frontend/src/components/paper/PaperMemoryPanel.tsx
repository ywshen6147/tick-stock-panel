/**
 * 模拟盘智能体「记忆与复盘」面板。
 *
 * 展示 LLM 跟单智能体（如「海平分时擒牛·模拟盘」）的:
 *   - 自我画像 profile（滚动业绩 / 当前关注点 / 连胜连亏）
 *   - 每日收盘复盘记录（做对了什么 / 做错了什么 / 下一步关注）
 *   - 经验教训 lessons（注入次日决策提示词, 可归档）
 *   - 知识库 knowledge（方法论 + 复盘沉淀规则, 可增删）
 *   - 复盘调度开关 / 立即复盘
 *
 * 数据全部来自 GET /api/paper/memory; 复盘跑一次会调 LLM（消耗 token）, 故按钮带确认语义。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Brain, Plus, RefreshCw, Trash2, Archive, ChevronDown, ChevronRight } from 'lucide-react'
import { api, type PaperMemoryPayload } from '@/lib/api'
import { cn } from '@/lib/cn'
import { toast } from '@/components/Toast'

const PATTERN_LABEL: Record<string, string> = {
  v_shape: 'V型确认',
  ignite: '点火突破',
  no_vol_push: '无量诱多',
  vol_divergence: '量价背离',
}

function fmtMoney(v?: number | null, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

function Chip({ children, tone = 'muted' }: { children: React.ReactNode; tone?: 'muted' | 'accent' | 'bull' | 'bear' | 'warn' }) {
  const cls = {
    muted: 'bg-elevated text-secondary',
    accent: 'bg-accent/10 text-accent',
    bull: 'bg-bull/10 text-bull',
    bear: 'bg-bear/10 text-bear',
    warn: 'bg-amber-500/10 text-amber-500',
  }[tone]
  return <span className={cn('rounded px-1.5 py-0.5 text-[10px]', cls)}>{children}</span>
}

function Section({ title, icon, right, children }: {
  title: string; icon?: React.ReactNode; right?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <div className="mt-3 rounded-btn border border-border/60 bg-elevated/20 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5">
        {icon}
        <span className="text-xs font-medium text-foreground">{title}</span>
        <div className="ml-auto flex items-center gap-1.5">{right}</div>
      </div>
      {children}
    </div>
  )
}

function ReviewCard({ rv }: { rv: PaperMemoryPayload['reviews'][number] }) {
  const [open, setOpen] = useState(false)
  const body = rv.review
  const m = rv.metrics || {}
  const pnl = m.day_pnl
  return (
    <div className="rounded-btn border border-border/60 bg-base/40 p-2">
      <button onClick={() => setOpen(v => !v)} className="flex w-full items-center gap-1.5 text-left">
        {open ? <ChevronDown className="h-3 w-3 text-muted" /> : <ChevronRight className="h-3 w-3 text-muted" />}
        <span className="font-mono text-[11px] text-secondary">{rv.date}</span>
        <span className={cn('font-mono text-[11px]', (pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear')}>
          日盈亏 {fmtMoney(pnl)}
        </span>
        <span className="ml-auto text-[10px] text-muted">
          成交 {m.fills ?? 0} · 平仓 {m.round_trips ?? 0} · 决策 {m.decisions ?? 0}
        </span>
      </button>
      <div className="mt-1 pl-4 text-[11px] leading-relaxed text-secondary">{body.summary || '（无总结）'}</div>
      {open && (
        <div className="mt-1.5 space-y-1.5 pl-4">
          {!!body.what_worked?.length && (
            <div className="text-[11px]">
              <span className="text-bull">做对：</span>
              <span className="text-secondary">{body.what_worked.join('；')}</span>
            </div>
          )}
          {!!body.what_failed?.length && (
            <div className="text-[11px]">
              <span className="text-bear">做错：</span>
              <span className="text-secondary">{body.what_failed.join('；')}</span>
            </div>
          )}
          {!!body.mistakes?.length && (
            <div className="text-[11px]">
              <span className="text-amber-500">失误：</span>
              <span className="text-secondary">{body.mistakes.join('；')}</span>
            </div>
          )}
          {!!body.next_focus?.length && (
            <div className="flex flex-wrap items-center gap-1 text-[11px]">
              <span className="text-accent">明日关注：</span>
              {body.next_focus.map((f, i) => <Chip key={i} tone="accent">{f}</Chip>)}
            </div>
          )}
          {!!body.rule_adjustments?.length && (
            <div className="text-[11px] text-muted">
              规则调整建议：{body.rule_adjustments.map(a => `${a.rule}→${a.change}`).join('；')}
            </div>
          )}
          {!!Object.keys(rv.patterns || {}).length && (
            <div className="flex flex-wrap items-center gap-1 text-[10px] text-muted">
              形态触发：
              {Object.entries(rv.patterns || {}).map(([k, v]) => (
                <Chip key={k} tone="warn">{PATTERN_LABEL[k] ?? k}×{v}</Chip>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function PaperMemoryPanel({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const [kbDraft, setKbDraft] = useState({ title: '', body: '', tags: '' })
  const [showAllLessons, setShowAllLessons] = useState(false)

  const q = useQuery({
    queryKey: ['paper', 'memory', acc],
    queryFn: () => api.paperMemory(acc, 10),
    refetchInterval: 60_000,
  })
  const data = q.data
  const status = data?.status
  const profile = status?.profile
  const refresh = () => qc.invalidateQueries({ queryKey: ['paper', 'memory', acc] })

  const runM = useMutation({
    mutationFn: () => api.paperMemoryReviewRun(acc),
    onSuccess: (res) => {
      if (!res.ok) { toast(res.error || '复盘失败', 'error'); return }
      if (res.skipped) { toast(res.skipped === 'already_reviewed' ? '当日已复盘过（如需重跑请用强制）' : `跳过：${res.skipped}`); return }
      toast(`复盘完成：新增经验 ${res.lessons_saved ?? 0} 条、知识 ${res.knowledge_saved ?? 0} 条`)
      refresh()
    },
  })
  const forceM = useMutation({
    mutationFn: () => api.paperMemoryReviewRun(acc, undefined, true),
    onSuccess: (res) => {
      if (!res.ok) { toast(res.error || '复盘失败', 'error'); return }
      toast(`已重跑复盘：经验 ${res.lessons_saved ?? 0} 条、知识 ${res.knowledge_saved ?? 0} 条`)
      refresh()
    },
  })
  const lessonM = useMutation({
    mutationFn: (id: string) => api.paperMemoryLessonArchive(id, acc),
    onSuccess: () => { toast('已归档该经验，不再注入决策'); refresh() },
  })
  const kbDelM = useMutation({
    mutationFn: (id: string) => api.paperMemoryKnowledgeDelete(id, acc),
    onSuccess: () => { toast('已删除知识条目'); refresh() },
  })
  const kbAddM = useMutation({
    mutationFn: () => api.paperMemoryKnowledgeAdd({
      title: kbDraft.title.trim(),
      body: kbDraft.body.trim(),
      tags: kbDraft.tags.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean),
    }, acc),
    onSuccess: () => { toast('已加入知识库'); setKbDraft({ title: '', body: '', tags: '' }); refresh() },
  })
  const schedM = useMutation({
    mutationFn: (enabled: boolean) => api.paperMemoryScheduleSet({
      enabled, hour: data?.schedule.hour ?? 15, minute: data?.schedule.minute ?? 35,
    }),
    onSuccess: () => { toast('复盘调度已更新'); refresh() },
  })

  const lessons = useMemo(() => {
    const rows = data?.lessons ?? []
    return showAllLessons ? rows : rows.slice(0, 8)
  }, [data?.lessons, showAllLessons])

  const busy = runM.isPending || forceM.isPending
  const inputCls = 'w-full rounded-btn border border-border bg-elevated px-2 py-1 text-xs text-secondary outline-none focus:border-accent'

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Brain className="h-4 w-4 text-accent" />
          <span className="text-sm font-medium">记忆与复盘</span>
          <span className="rounded bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-muted" title="面板内容属于当前选中的虚拟账户">
            账户 {acc}
          </span>
          <span className="text-[10px] text-muted">收盘后自动复盘自己的交易 → 沉淀记忆/知识库 → 反哺次日决策</span>
        </div>
        <div className="flex items-center gap-1.5">
          <label className="flex items-center gap-1 text-[11px] text-muted">
            <input
              type="checkbox"
              checked={data?.schedule.enabled ?? true}
              disabled={schedM.isPending || !data}
              onChange={e => schedM.mutate(e.target.checked)}
            />
            每个交易日 {String(data?.schedule.hour ?? 15).padStart(2, '0')}:{String(data?.schedule.minute ?? 35).padStart(2, '0')} 自动复盘
          </label>
          <button
            onClick={() => runM.mutate()}
            disabled={busy}
            className="flex items-center gap-1 rounded-btn bg-accent/10 px-2.5 py-1 text-xs text-accent transition-colors hover:bg-accent/20 disabled:opacity-50"
          >
            <RefreshCw className={cn('h-3 w-3', busy && 'animate-spin')} />
            {busy ? '复盘进行中…' : '立即复盘'}
          </button>
          <button
            onClick={() => { if (confirm('强制重跑当日复盘？会覆盖当日复盘记录（经验与知识去重合并）。')) forceM.mutate() }}
            disabled={busy}
            className="rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:text-secondary disabled:opacity-50"
          >
            强制重跑
          </button>
        </div>
      </div>

      {status?.last_error && (
        <div className="mt-2 rounded-btn border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-500">
          上次复盘失败：{status.last_error}
        </div>
      )}

      {/* ── 自我画像 ── */}
      <Section title="自我画像" icon={<Brain className="h-3 w-3 text-accent" />}
        right={<span className="text-[10px] text-muted">已复盘 {profile?.days_reviewed ?? 0} 天</span>}>
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-4">
          <div><span className="text-muted">胜率</span> <span className="font-mono text-secondary">{profile?.win_rate ?? '—'}%</span></div>
          <div><span className="text-muted">盈亏比</span> <span className="font-mono text-secondary">{profile?.profit_loss_ratio ?? '—'}</span></div>
          <div><span className="text-muted">已实现盈亏</span>
            <span className={cn('font-mono', (profile?.realized_pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear')}> {fmtMoney(profile?.realized_pnl)}</span>
          </div>
          <div><span className="text-muted">最大回撤</span> <span className="font-mono text-secondary">{profile?.max_drawdown ?? '—'}%</span></div>
          <div><span className="text-muted">回合数</span> <span className="font-mono text-secondary">{profile?.rounds ?? 0}</span></div>
          <div><span className="text-muted">平均持有</span> <span className="font-mono text-secondary">{profile?.avg_holding_days ?? 0}天</span></div>
          <div className="col-span-2">
            <span className="text-muted">连胜/连亏</span>{' '}
            <span className={cn('font-mono', (profile?.streak?.pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear')}>
              {profile?.streak?.days ?? 0} 天 {fmtMoney(profile?.streak?.pnl)}
            </span>
          </div>
        </div>
        {(profile?.focus?.length ?? 0) > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            <span className="text-[10px] text-muted">当前关注</span>
            {profile!.focus.map((f, i) => <Chip key={i} tone="accent">{f}</Chip>)}
          </div>
        )}
      </Section>

      {/* ── 每日复盘 ── */}
      <Section title="每日收盘复盘" icon={<RefreshCw className="h-3 w-3 text-accent" />}
        right={<span className="text-[10px] text-muted">{status?.counts?.reviews ?? 0} 天</span>}>
        {(data?.reviews?.length ?? 0) === 0 ? (
          <div className="py-2 text-center text-[11px] text-muted">
            还没有复盘记录。收盘后会自动生成；也可点「立即复盘」手动跑一次（需 AI 已配置）。
          </div>
        ) : (
          <div className="space-y-1.5">
            {data!.reviews.map(rv => <ReviewCard key={rv.date} rv={rv} />)}
          </div>
        )}
      </Section>

      {/* ── 经验教训 ── */}
      <Section title="经验教训（注入决策提示词）" icon={<BookOpen className="h-3 w-3 text-accent" />}
        right={<span className="text-[10px] text-muted">{data?.lessons?.length ?? 0} 条</span>}>
        {(data?.lessons?.length ?? 0) === 0 ? (
          <div className="py-1 text-center text-[11px] text-muted">暂无。复盘后会自动沉淀。</div>
        ) : (
          <div className="space-y-1">
            {lessons.map(l => (
              <div key={l.id} className="flex items-start gap-1.5 rounded border border-border/40 bg-base/30 px-2 py-1">
                <span className="mt-0.5 shrink-0 font-mono text-[10px] text-muted">{l.date?.slice(5)}</span>
                <span className="flex-1 text-[11px] text-secondary">{l.text}</span>
                <span className="shrink-0 font-mono text-[10px] text-muted" title="置信度">{(l.confidence ?? 0).toFixed(2)}</span>
                {!!l.hits && <Chip tone="warn">×{l.hits}</Chip>}
                {!!l.tags?.length && <Chip tone="accent">{l.tags[0]}</Chip>}
                <button
                  onClick={() => lessonM.mutate(l.id)}
                  title="归档（不再注入决策）"
                  className="shrink-0 text-muted transition-colors hover:text-bear"
                >
                  <Archive className="h-3 w-3" />
                </button>
              </div>
            ))}
            {(data?.lessons?.length ?? 0) > 8 && (
              <button onClick={() => setShowAllLessons(v => !v)} className="w-full pt-0.5 text-center text-[10px] text-muted hover:text-accent">
                {showAllLessons ? '收起' : `展开全部 ${data!.lessons.length} 条`}
              </button>
            )}
          </div>
        )}
      </Section>

      {/* ── 知识库 ── */}
      <Section title="知识库" icon={<BookOpen className="h-3 w-3 text-accent" />}
        right={<span className="text-[10px] text-muted">{status?.counts?.knowledge ?? 0} 条</span>}>
        <div className="max-h-56 space-y-1 overflow-y-auto pr-0.5">
          {(data?.knowledge ?? []).map(k => (
            <div key={k.id} className="group flex items-start gap-1.5 rounded border border-border/40 bg-base/30 px-2 py-1">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-[11px] font-medium text-secondary">{k.title}</span>
                  {k.source === 'methodology' && <Chip tone="accent">方法论</Chip>}
                  {k.source === 'review' && <Chip tone="warn">复盘沉淀</Chip>}
                </div>
                {k.body && <div className="mt-0.5 text-[10px] leading-relaxed text-muted">{k.body}</div>}
              </div>
              <button
                onClick={() => kbDelM.mutate(k.id)}
                title="删除"
                className="shrink-0 text-muted opacity-0 transition-opacity hover:text-bear group-hover:opacity-100"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))}
          {(data?.knowledge?.length ?? 0) === 0 && (
            <div className="py-1 text-center text-[11px] text-muted">暂无条目</div>
          )}
        </div>
        <div className="mt-2 space-y-1">
          <input
            className={inputCls}
            placeholder="新增知识标题（如：破线3分钟未收回即减仓）"
            value={kbDraft.title}
            onChange={e => setKbDraft(d => ({ ...d, title: e.target.value }))}
          />
          <textarea
            className={cn(inputCls, 'resize-y')}
            rows={2}
            placeholder="规则内容（条件 → 动作 → 验证方式）"
            value={kbDraft.body}
            onChange={e => setKbDraft(d => ({ ...d, body: e.target.value }))}
          />
          <div className="flex items-center gap-1.5">
            <input
              className={inputCls}
              placeholder="标签（逗号分隔，如 卖点,纪律）"
              value={kbDraft.tags}
              onChange={e => setKbDraft(d => ({ ...d, tags: e.target.value }))}
            />
            <button
              onClick={() => kbAddM.mutate()}
              disabled={!kbDraft.title.trim() || kbAddM.isPending}
              className="flex shrink-0 items-center gap-1 rounded-btn bg-accent/10 px-2.5 py-1 text-xs text-accent transition-colors hover:bg-accent/20 disabled:opacity-50"
            >
              <Plus className="h-3 w-3" /> 加入
            </button>
          </div>
        </div>
      </Section>
    </div>
  )
}
