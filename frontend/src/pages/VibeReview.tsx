import { useCallback, useEffect, useState } from 'react'
import {
  Loader2, Play, RefreshCw, Gauge, Target,
  ArrowLeftRight, ListTree, TrendingUp,
  AlertTriangle, Send, MessageCircle, CheckSquare, Plus, X,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { vibeApi, type ReviewData } from '@/lib/vibe'

// ===== 工具函数 =====
const pct = (v: number | null | undefined, digits = 0) =>
  v == null ? '—' : `${(v * 100).toFixed(digits)}%`
const signed = (v: number | null | undefined, digits = 2) =>
  v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
const pctColor = (v: number | null | undefined) => {
  if (v == null) return 'text-muted'
  if (v > 0) return 'text-bull'
  if (v < 0) return 'text-bear'
  return 'text-muted'
}
const hhmm = (t?: string | null) => t && t.length >= 4 ? `${t.slice(0, 2)}:${t.slice(2, 4)}` : '—'
const yi = (v: number | null | undefined) => v == null ? '—' : `${Math.round(v).toLocaleString()} 亿`

type Rec = Record<string, any>

// ===== 通用组件 =====
function Panel({ title, hint, children, icon: Icon = Gauge, badge }: {
  title: string; hint?: string; children: React.ReactNode; icon?: any; badge?: string
}) {
  return (
    <div className="rounded-btn border border-border bg-elevated p-4">
      <div className="mb-1 flex items-center gap-1.5">
        <Icon className="h-4 w-4 text-accent" />
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {badge && <span className="text-[10px] text-muted">{badge}</span>}
      </div>
      {hint && <p className="mb-3 text-[11px] leading-relaxed text-muted">{hint}</p>}
      {children}
    </div>
  )
}

function Stat({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className={cn('font-mono text-xl font-bold tabular-nums', valueClass ?? 'text-foreground')}>{value}</div>
      <div className="text-[11px] text-muted">{label}</div>
    </div>
  )
}

function Mini({ label, value, valueClass, sub }: { label: string; value: string; valueClass?: string; sub?: string }) {
  return (
    <div className="rounded-btn border border-border/60 bg-elevated px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cn('mt-0.5 font-mono text-base font-semibold', valueClass ?? 'text-foreground')}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

/** 简单的 markdown 渲染（标题/列表/段落/粗体） */
function MarkdownLite({ md }: { md: string }) {
  const blocks = md.split(/\n{2,}/).filter(Boolean)
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      {blocks.map((b, i) => {
        if (/^#{1,3}\s/.test(b)) return <h3 key={i} className="text-base font-semibold text-foreground">{b.replace(/^#{1,3}\s/, '')}</h3>
        if (b.split('\n').every(l => /^[-*•]\s/.test(l.trim()))) {
          return (
            <ul key={i} className="list-disc space-y-1 pl-5">
              {b.split('\n').map((l, j) => <li key={j} className="text-secondary" dangerouslySetInnerHTML={{ __html: mdInline(l.replace(/^[-*•]\s/, '')) }} />)}
            </ul>
          )
        }
        return <p key={i} className="text-secondary" dangerouslySetInnerHTML={{ __html: mdInline(b) }} />
      })}
    </div>
  )
}

function mdInline(s: string): string {
  return s
    .replace(/\*\*(.+?)\*\*/g, '<b class="text-foreground">$1</b>')
    .replace(/\*(.+?)\*/g, '<i>$1</i>')
    .replace(/`(.+?)`/g, '<code class="font-mono text-accent">$1</code>')
}

/** 分区小节标题（对应 VIBE 原版 8 大板块的英文分区头） */
function SectionTitle({ en, children }: { en: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="text-[11px] font-bold uppercase tracking-[0.2em] text-accent">{en}</span>
      {children && <span className="text-[11px] text-muted">{children}</span>}
    </div>
  )
}

export function VibeReviewPage() {
  const [dates, setDates] = useState<string[]>([])
  const [date, setDate] = useState<string>('')
  const [data, setData] = useState<ReviewData | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    let alive = true
    vibeApi.reviewDates().then(d => {
      if (!alive) return
      setDates(d.dates ?? [])
      if (d.dates?.length) setDate(d.dates[d.dates.length - 1])
    }).catch(e => setMsg(`读取复盘日期失败: ${e.message}`))
    return () => { alive = false }
  }, [])

  const load = useCallback((d: string, opts?: { feedback?: string; doneMsg?: string }) => {
    if (!d) return
    setLoading(true)
    setMsg(opts?.feedback || '')
    vibeApi.reviewLatest(d)
      .then(r => {
        // 未跑过复盘：返回只有 requested_date，没有 trade_date/review_id
        if (!r?.trade_date && !r?.review_id) {
          setData(null)
          setMsg(`${d} 还没跑过复盘，点「生成复盘」补跑，或换一个日期`)
          return
        }
        setData(r)
        if (!r?.focus_md) setMsg('该日复盘内容为空（可能 AI 环节未生成）')
        else if (opts?.doneMsg) setMsg(opts.doneMsg)
      })
      .catch(e => setMsg(`读取复盘失败: ${e.message}`))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { if (date) load(date) }, [date, load])

  async function runReview() {
    setRunning(true)
    setMsg('')
    try {
      const st = await vibeApi.reviewRun(date)
      if (st.running) {
        setMsg('复盘已在后台运行（约 5-8 分钟，Kimi 推理模型较慢），完成后自动刷新')
        const timer = setInterval(async () => {
          try {
            const s = await vibeApi.reviewStatus()
            if (!s.running) {
              clearInterval(timer)
              setRunning(false)
              load(date)
            }
          } catch { /* ignore */ }
        }, 15000)
      } else if ((st as Rec).already_done) {
        // 该日已复盘：告知并加载当天结果（vibe-astock 原版行为）
        setRunning(false)
        load(date, { feedback: '正在加载…', doneMsg: (st as Rec).message || `${date} 已复盘，下面就是那天的结果` })
      } else {
        setRunning(false)
        load(date)
      }
    } catch (e) {
      setRunning(false)
      setMsg(`触发复盘失败: ${(e as Error).message}`)
    }
  }

  const em = (data?.emotion_metrics ?? {}) as Rec
  const mf = (data?.market_facts ?? {}) as Rec
  const focus = (data?.focus ?? {}) as Rec
  const analysts = (data?.analysts ?? []) as Rec[]

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <PageHeader
        title="短线复盘看板"
        subtitle={`${data?.trade_date ?? ''} · 硬指标 + AI 研判`}
        right={
          <div className="flex items-center gap-2">
            <div className="flex flex-col items-start">
              <input
                type="date"
                value={date}
                list="review-dates"
                onChange={e => { const v = e.target.value; setDate(v); setMsg(''); if (v) load(v) }}
                className="h-8 rounded-btn border border-border bg-elevated px-2 text-xs text-secondary"
              />
              {/* 跑过的日子列出来，方便直接选 */}
              <datalist id="review-dates">
                {dates.map(d => <option key={d} value={d} />)}
              </datalist>
              {dates.length > 1 && (
                <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-muted">
                  {dates.slice(0, 5).map(d => (
                    <button key={d} type="button" onClick={() => { setDate(d); setMsg(''); load(d) }}
                      className={`rounded px-1.5 py-0.5 transition-colors hover:text-accent ${date === d ? 'bg-accent/15 text-accent' : 'bg-muted/40'}`}>
                      {d.slice(5)}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button type="button" onClick={() => load(date, { feedback: '正在刷新…', doneMsg: '刷新完成' })} disabled={loading}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground">
              <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} /> 刷新
            </button>
            <button type="button" onClick={runReview} disabled={running}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-2.5 text-xs text-white hover:bg-accent/90">
              {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              {running ? '生成中…' : '生成复盘'}
            </button>
          </div>
        }
      />

      {msg && <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{msg}</div>}

      {loading ? (
        <div className="flex items-center justify-center py-16 text-muted"><Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载中…</div>
      ) : (
        <>
          {/* ===== ① 今天好不好做 · BREADTH ===== */}
          <section className="space-y-4">
            <SectionTitle en="今天好不好做 · BREADTH">市场宽度是下面所有涨停池数字的分母</SectionTitle>
            <BreadthPanel b={mf.breadth} />
          </section>

          {/* ===== ② 昨天进去的人赚不赚钱 · FEEDBACK ===== */}
          <section className="space-y-4">
            <SectionTitle en="昨天进去的人赚不赚钱 · FEEDBACK">昨日涨停/炸板股今天的落点，亏钱集中在哪一档</SectionTitle>
            <FeedbackMatrixPanel fm={mf.feedback_matrix} />
            <div className="grid gap-3 lg:grid-cols-2">
              <MoneyEffectCard me={em.money_effect} />
              <LossEffectCard le={mf.loss_effect} />
            </div>
          </section>

          {/* ===== ③ 接力生态 · LADDER ===== */}
          <section className="space-y-4">
            <SectionTitle en="接力生态 · LADDER">
              {em.prev_date && em.promotion?.limit_up_count != null && em.promotion?.prev_limit_up_count != null && (
                <>对照 {em.prev_date} · 涨停家数 {em.promotion.prev_limit_up_count} → <b className="tabular-nums text-foreground">{em.promotion.limit_up_count}</b></>
              )}
            </SectionTitle>
            <div className="grid gap-3 lg:grid-cols-2">
              <PromotionCard pr={em.promotion} />
              <ConsecLadder cp={em.consec_premium} lg={em.ladder_gap} />
            </div>
            <SealQualityCard sq={mf.seal_quality} />
          </section>

          {/* ===== ④ 钱往哪去 · THEMES ===== */}
          <section className="space-y-4">
            <SectionTitle en="钱往哪去 · THEMES">涨停按题材聚类，钱集中在哪些方向</SectionTitle>
            <ThemePanel ts={mf.theme_structure} tree={mf.theme_tree} />
          </section>

          {/* ===== ⑤ 今天风险在哪 · RISK ===== */}
          <section className="space-y-4">
            <SectionTitle en="今天风险在哪 · RISK">亏钱方向 + 分板位断档</SectionTitle>
            <EventLedgerPanel el={mf.event_ledger} />
            <ByBoardPanel bb={mf.by_board} />
          </section>

          {/* ===== ⑥ 跟前几天比 · TREND ===== */}
          <section className="space-y-4">
            <SectionTitle en="跟前几天比 · TREND">趋势 / 历史位置 / 日变化 / 情绪周期</SectionTitle>
            <TrendPanel tr={mf.trend} />
            <div className="grid gap-3 lg:grid-cols-2">
              <StatsContextPanel sc={mf.stats_context} />
              <DayDiffPanel dd={mf.day_diff} />
            </div>
            <CycleCard cy={em.cycle} />
          </section>

          {/* ===== ⑦ 明天关注点 · TOMORROW ===== */}
          <section className="space-y-4">
            <SectionTitle en="明天关注点 · TOMORROW">AI 多 agent 收敛的方向 + 验证条件 + 风险</SectionTitle>

            {/* 情绪档位 + 一句话 */}
            {(focus.emotion_phase || focus.market_oneliner) && (
              <div className="rounded-btn border border-border bg-elevated p-4">
                {focus.emotion_phase && (
                  <div className="mb-1 flex items-center gap-2">
                    <span className="rounded bg-accent/10 px-2 py-0.5 text-xs font-semibold text-accent">情绪档位</span>
                    <span className="text-base font-bold text-foreground">{focus.emotion_phase}</span>
                  </div>
                )}
                {focus.market_oneliner && <p className="text-sm text-secondary">{focus.market_oneliner}</p>}
              </div>
            )}

            {/* AI 关注方向（focus_directions） */}
            {focus.focus_directions?.length > 0 && (
              <Panel title="关注方向" badge="AI 判断，仅供参考" icon={Target}>
                <div className="grid gap-3 md:grid-cols-2">
                  {(focus.focus_directions as Rec[]).map((d, i) => (
                    <div key={i} className="rounded-btn border border-border/60 bg-elevated p-3">
                      <div className="text-sm font-bold text-foreground">{d.direction}</div>
                      <p className="mt-1.5 text-xs leading-relaxed text-secondary">{d.logic}</p>
                      {(d.leader_candidates?.length ?? 0) > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {(d.leader_candidates as string[]).map((l, j) => (
                            <span key={j} className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">{l}</span>
                          ))}
                        </div>
                      )}
                      {d.risk && (
                        <div className="mt-2 border-t border-dashed border-border pt-1.5 text-[11px] text-bear">
                          <span className="font-semibold">风险</span> {d.risk}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </Panel>
            )}

            {/* 明日验证条件 */}
            <VerificationPanel items={focus.verification_items} />

            {/* 用户自定义验证条件 */}
            {data?.trade_date && <UserConditionsPanel date={data.trade_date} />}

            {/* AI 风险提示（risk_alerts） */}
            {focus.risk_alerts?.length > 0 && (
              <Panel title="风险提示" badge="AI 识别的风险点，仅供参考" icon={AlertTriangle}>
                <ul className="space-y-1.5 text-xs leading-relaxed text-secondary">
                  {(focus.risk_alerts as string[]).map((r, i) => (
                    <li key={i} className="flex gap-1.5"><span className="text-bear">●</span><span>{r}</span></li>
                  ))}
                </ul>
              </Panel>
            )}

            {/* 5 个 AI 分析师报告 */}
            {analysts.length > 0 && (
              <Panel title="分析师视角" hint="5 个角度的 AI 研判（情绪/资金/题材/龙虎榜/龙头），仅供参考">
                <div className="grid gap-3 lg:grid-cols-2">
                  {analysts.map((a) => (
                    <div key={a.key} className="rounded-btn border border-border/60 bg-elevated p-3">
                      <div className="mb-1 text-xs font-semibold text-accent">{a.title}</div>
                      <div className="text-xs leading-relaxed text-secondary" dangerouslySetInnerHTML={{ __html: a.html || '' }} />
                    </div>
                  ))}
                </div>
              </Panel>
            )}

            {/* AI 盘面研判（focus_md） */}
            {data?.focus_md && (
              <Panel title="AI 盘面研判" badge="多 agent 生成，仅供参考">
                <MarkdownLite md={data.focus_md} />
              </Panel>
            )}
          </section>

          {/* ===== ⑧ 追问 · ASK ===== */}
          <section className="space-y-4">
            <SectionTitle en="追问 · ASK">就今天的复盘对话</SectionTitle>
            {data?.trade_date && <ReviewChatPanel />}
          </section>

          {!data?.focus_md && !mf.breadth?.available && !em.money_effect?.available && (
            <div className="rounded-btn border border-border bg-elevated py-10 text-center text-sm text-muted">
              暂无数据。点击「生成复盘」触发（需已收盘的交易日）。
            </div>
          )}
        </>
      )}

      <p className="text-center text-[11px] text-muted">
        AI 结论仅供参考，不构成投资建议 · 数据来自 vibe-astock（http://127.0.0.1:8910）
      </p>
    </div>
  )
}

// ===== 子面板 =====

/** 市场宽度：涨跌宽度带 + 上涨占比 + 成交额 + 涨跌超5% */
function BreadthPanel({ b }: { b?: Rec }) {
  if (!b || !b.available) return null
  const up = b.up ?? 0, down = b.down ?? 0, flat = b.flat ?? 0
  const tot = up + down + flat
  const upRatio = tot ? up / tot : null
  return (
    <Panel title="今天好不好做 · 市场宽度" hint="下面所有涨停池的数字，都要拿这一行当分母来读">
      <div className="flex h-7 overflow-hidden rounded-lg text-[11px] font-bold text-white">
        <div className="flex items-center justify-center bg-bull" style={{ width: `${tot ? (up / tot) * 100 : 0}%` }}>{tot && up / tot > 0.08 ? `${up} 涨` : ''}</div>
        <div className="flex items-center justify-center bg-muted-foreground/40" style={{ width: `${tot ? (flat / tot) * 100 : 0}%` }}>{flat && flat / tot > 0.08 ? `${flat} 平` : ''}</div>
        <div className="flex items-center justify-center bg-bear" style={{ width: `${tot ? (down / tot) * 100 : 0}%` }}>{tot && down / tot > 0.08 ? `${down} 跌` : ''}</div>
      </div>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-5 gap-y-1 text-[13px]">
        <span>上涨占比 <b className={cn('text-base tabular-nums', upRatio == null ? '' : pctColor(upRatio - 0.5))}>{upRatio == null ? '—' : pct(upRatio)}</b></span>
        <span className="text-muted"><b className="text-bull">{up}</b> 涨 / <b className="text-bear">{down}</b> 跌 / {flat} 平（{b.up_down_scope}）</span>
      </div>
      <div className="mt-3 grid gap-3 border-t border-dashed border-border pt-3 sm:grid-cols-3">
        <Stat label="两市成交额" value={yi(b.amount_yi)} />
        <Stat label="跌超 5%" value={b.deep_down_5 != null ? `${b.deep_down_5} 家` : '—'} valueClass="text-bear" />
        <Stat label="涨幅 ≥5%" value={b.deep_up_5_incl != null ? `${b.deep_up_5_incl} 家` : '—'} valueClass="text-bull" />
      </div>
    </Panel>
  )
}

/** 赚钱效应 */
function MoneyEffectCard({ me }: { me?: Rec }) {
  if (!me || !me.available) return null
  return (
    <Panel title="赚钱效应" hint="昨日涨停股今天的表现。中位数代表多数人体感，均值会被少数大涨拉高">
      <div className="flex items-end gap-6">
        <Stat label="中位数" value={signed(me.median)} valueClass={pctColor(me.median)} />
        <Stat label="均值" value={signed(me.avg)} valueClass={pctColor(me.avg)} />
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-dashed border-border pt-2 text-xs text-muted">
        <span>样本 {me.sample ?? '—'} 只</span>
        <span>翻红率 <b className="text-foreground">{pct(me.positive_rate)}</b></span>
        <span>再度涨停 <b className="text-foreground">{pct(me.limit_up_again_rate)}</b></span>
      </div>
    </Panel>
  )
}

/** 晋级率 */
function PromotionCard({ pr }: { pr?: Rec }) {
  if (!pr || !pr.available) return null
  const tiers = (pr.tiers ?? {}) as Rec
  const entries = Object.entries(tiers).filter(([, t]) => t && (t.base ?? 0) > 0)
  return (
    <Panel title="晋级率" hint="昨日各档连板今天仍涨停的比例。1进2 最敏感：走低=退潮，回升=修复">
      <Stat label="整体晋级率" value={pct(pr.overall?.rate)} />
      <div className="mt-3 space-y-1.5 border-t border-dashed border-border pt-2">
        {entries.map(([name, t]: [string, Rec]) => (
          <div key={name} className="flex items-center gap-2 text-xs">
            <span className="w-20 shrink-0 text-muted">{name}</span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
              {t.rate != null && <div className="h-full rounded-full bg-accent" style={{ width: `${Math.round(t.rate * 100)}%` }} />}
            </div>
            <span className="w-24 shrink-0 text-right font-mono">{t.promoted}/{t.base}（{pct(t.rate)}）</span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/** 连板溢价 + 梯队结构 */
function ConsecLadder({ cp, lg }: { cp?: Rec; lg?: Rec }) {
  const showCp = cp?.available
  const showLg = lg?.available
  if (!showCp && !showLg) return null
  const tiers = (lg?.tiers ?? {}) as Rec
  return (
    <Panel title="连板承接与梯队" hint="连板溢价=昨日2板以上今天的表现（高标承接度）；梯队=各板位有没有缺档">
      <div className="flex flex-wrap items-end gap-6">
        {showCp && <Stat label="连板溢价中位" value={signed(cp.median)} valueClass={pctColor(cp.median)} />}
        {showCp && <Stat label="均值" value={signed(cp.avg)} valueClass={pctColor(cp.avg)} />}
        {showLg && <Stat label="最高板" value={lg.highest != null ? `${lg.highest} 板` : '—'} />}
      </div>
      {showLg && (
        <>
          <div className="mt-3 flex flex-wrap items-end gap-2">
            {Object.entries(tiers).map(([b, c]) => (
              <span key={b} className="rounded bg-accent/10 px-2.5 py-1 text-[13px] font-semibold text-accent">{b}板 × {c}</span>
            ))}
            {(lg.gaps ?? []).map((g: number) => (
              <span key={`g${g}`} className="rounded border border-dashed border-bear/50 px-2.5 py-1 text-[13px] font-semibold text-bear">{g}板 缺</span>
            ))}
            {Object.keys(tiers).length === 0 && <span className="text-[13px] text-muted">无 2 板以上</span>}
          </div>
          {lg.note && <p className="mt-3 border-t border-dashed border-border pt-2 text-xs text-warning">{lg.note}</p>}
        </>
      )}
    </Panel>
  )
}

/** 情绪周期 */
function CycleCard({ cy }: { cy?: Rec }) {
  if (!cy || !cy.available) return null
  const series = (cy.series ?? []) as Rec[]
  return (
    <Panel title="情绪周期" hint="窗口内情绪分最低的那天视为本轮起点">
      <div className="flex items-end gap-6">
        <Stat label={`起点 ${cy.trough_date ?? '—'}`} value={`第 ${cy.day_n ?? '—'} 天`} />
        <Stat label="当前情绪分" value={cy.current_score != null ? cy.current_score.toFixed(2) : '—'} valueClass={cy.trend?.includes('走强') ? 'text-bull' : cy.trend?.includes('转弱') ? 'text-bear' : undefined} />
      </div>
      {series.length > 0 && (
        <div className="mt-3 border-t border-dashed border-border pt-3">
          <div className="flex h-16 items-end gap-1.5">
            {series.map((d: Rec) => {
              const sc = d.score
              const isTrough = d.date === cy.trough_date
              const isLast = d.date === series[series.length - 1]?.date
              return (
                <div
                  key={d.date}
                  title={`${d.date} 情绪分 ${sc ?? '—'}｜涨停 ${d.limit_up ?? '—'} 家｜最高 ${d.highest_consec ?? '—'} 板`}
                  className={cn(
                    'flex-1 rounded-t transition-colors',
                    sc == null ? 'bg-muted' : isTrough ? 'bg-danger' : isLast ? 'bg-bull' : 'bg-accent',
                  )}
                  style={sc == null ? { height: 8 } : { height: `${Math.max(12, Math.round(sc * 100))}%` }}
                />
              )
            })}
          </div>
          <div className="mt-1 flex gap-1.5">
            {series.map((d: Rec, i: number) => {
              const isLast = i === series.length - 1
              const isTrough = d.date === cy.trough_date
              return (
                <span key={d.date} className={cn('flex-1 text-center text-[9px] leading-tight tabular-nums',
                  isTrough ? 'font-bold text-danger' : isLast ? 'font-bold text-foreground' : 'text-muted')}>
                  {d.date.slice(5).replace('-', '/')}
                </span>
              )
            })}
          </div>
        </div>
      )}
      <p className="mt-1.5 text-xs text-muted">
        近 {cy.window ?? '—'} 个交易日 · 距窗口低点第 {cy.day_n ?? '—'} 天
        {cy.trend && <> · <b className={cy.trend.includes('走强') ? 'text-bull' : cy.trend.includes('转弱') ? 'text-bear' : ''}>{cy.trend}</b></>}
        {cy.pctile != null && ` · 位于十日区间 ${Math.round(cy.pctile * 100)}% 分位`}
        <span className="ml-1.5 inline-block h-2 w-2 rounded-sm bg-danger align-middle" /> 起点
        <span className="ml-2 font-bold text-foreground">加粗</span> 复盘当日
      </p>
    </Panel>
  )
}

/** 封板质量 */
function SealQualityCard({ sq }: { sq?: Rec }) {
  if (!sq || !sq.available) return null
  return (
    <Panel title="封板质量" hint="板的成色：没炸过的才算真封住">
      <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
        <Mini label="涨停" value={String(sq.total ?? '—')} valueClass="text-bull" />
        <Mini label="未炸板" value={String(sq.never_broken ?? '—')} sub={sq.never_broken_rate != null ? pct(sq.never_broken_rate) : undefined} valueClass="text-bull" />
        <Mini label="炸板率" value={pct(sq.broken_rate)} valueClass={sq.broken_rate != null && sq.broken_rate > 0.4 ? 'text-bear' : undefined} />
        <Mini label="炸板池" value={String(sq.broken_pool ?? '—')} />
        <Mini label="回封" value={String(sq.reopened ?? '—')} />
        <Mini label="均炸次数" value={sq.avg_broken_times != null ? String(sq.avg_broken_times) : '—'} />
      </div>
    </Panel>
  )
}

/** 亏钱效应 */
function LossEffectCard({ le }: { le?: Rec }) {
  if (!le || !le.available) return null
  return (
    <Panel title="亏钱效应" hint="昨日强势股今天的跌幅分布——亏钱集中在哪">
      <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
        <Mini label="样本" value={String(le.sample ?? '—')} />
        <Mini label="跌超5%" value={String(le.deep_loss_5_count ?? '—')} valueClass="text-bear" />
        <Mini label="跌超7%" value={String(le.deep_loss_7_count ?? '—')} valueClass="text-bear" />
        <Mini label="跌停" value={String(le.limit_down_count ?? '—')} valueClass="text-bear" />
        <Mini label="最差" value={signed(le.worst)} valueClass="text-bear" />
        <Mini label="跌超5%占比" value={pct(le.deep_loss_5_rate)} valueClass="text-bear" />
      </div>
    </Panel>
  )
}

/** 分板位统计 */
function ByBoardPanel({ bb }: { bb?: Rec }) {
  if (!bb || !bb.available) return null
  const boards = (bb.boards ?? {}) as Rec
  return (
    <Panel title="分板位统计" badge={`前日 ${bb.prev_date ?? ''}`}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-[11px] text-muted">
              <th className="py-1.5 pr-3">板位</th><th className="py-1.5 pr-3">涨停</th><th className="py-1.5 pr-3">最高</th>
              <th className="py-1.5 pr-3">前日基数</th><th className="py-1.5 pr-3">晋级</th><th className="py-1.5 pr-3">晋级率</th>
              <th className="py-1.5 pr-3">炸板</th><th className="py-1.5 pr-3">跌停</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(boards).map(([k, v]) => (
              <tr key={k} className="border-b border-border/50">
                <td className="py-1.5 pr-3 text-secondary">{k}</td>
                <td className="py-1.5 pr-3 font-mono text-bull">{v.limit_up}</td>
                <td className="py-1.5 pr-3 font-mono text-secondary">{v.highest}板</td>
                <td className="py-1.5 pr-3 font-mono text-secondary">{v.prev_base}</td>
                <td className="py-1.5 pr-3 font-mono text-secondary">{v.promoted}</td>
                <td className={cn('py-1.5 pr-3 font-mono', pctColor(v.promotion_rate))}>{pct(v.promotion_rate)}</td>
                <td className="py-1.5 pr-3 font-mono text-bear">{v.broken}</td>
                <td className="py-1.5 pr-3 font-mono text-bear">{v.limit_down}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

/** 昨日强势股反馈矩阵 */
function FeedbackMatrixPanel({ fm }: { fm?: Rec }) {
  if (!fm || !fm.available) return null
  const matrix = (fm.matrix ?? {}) as Rec
  const tierOrder = (t: string) =>
    t === '首板' ? 1 : t === '昨日炸板' ? Number.MAX_SAFE_INTEGER : (parseInt(t, 10) || Number.MAX_SAFE_INTEGER - 1)
  const tiers = Object.keys(matrix).sort((a, b) => tierOrder(a) - tierOrder(b))
  const cols = ['晋级涨停', '收红', '小跌', '跌超5%', '跌停'] as const
  if (tiers.length === 0) return null
  return (
    <Panel title="昨日强势股反馈" hint="昨天涨停/炸板的票今天分别落到哪一档——亏钱集中在哪个板位">
      <div className="overflow-x-auto">
        <table className="w-full text-xs tabular-nums">
          <thead>
            <tr className="border-b border-border text-[11px] text-muted">
              <th className="py-1.5 text-left font-semibold">昨日身份</th>
              {cols.map(c => <th key={c} className="px-1.5 py-1.5 text-right font-semibold">{c}</th>)}
              <th className="px-1.5 py-1.5 text-right font-semibold">晋级率</th>
            </tr>
          </thead>
          <tbody>
            {tiers.map(t => {
              const c = matrix[t]
              return (
                <tr key={t} className="border-b border-border/50 last:border-0">
                  <td className="py-1.5 font-semibold">{t}<span className="ml-1 text-muted">({c.合计})</span></td>
                  <td className="px-1.5 py-1.5 text-right text-bull">{c.晋级涨停}</td>
                  <td className="px-1.5 py-1.5 text-right">{c.收红}</td>
                  <td className="px-1.5 py-1.5 text-right text-muted">{c.小跌}</td>
                  <td className="px-1.5 py-1.5 text-right text-bear">{c['跌超5%']}</td>
                  <td className="px-1.5 py-1.5 text-right font-bold text-bear">{c.跌停}</td>
                  <td className="px-1.5 py-1.5 text-right font-bold">{pct(c.晋级率)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {(fm.details?.length ?? 0) > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-[11px] text-muted hover:text-foreground">
            展开明细（{fm.details.length} 只，按今日表现从差到好）
          </summary>
          <div className="mt-2 max-h-64 overflow-y-auto">
            <table className="w-full text-[11px] tabular-nums">
              <tbody>
                {fm.details.map((d: Rec) => (
                  <tr key={d.code} className="border-b border-border/30 last:border-0">
                    <td className="py-1 pr-2 font-semibold">{d.name}</td>
                    <td className="pr-2 text-muted">{d.code}</td>
                    <td className="pr-2 text-muted">{d.prev_tier}</td>
                    <td className={cn('pr-2 font-bold', pctColor(d.ret))}>{signed(d.ret)}</td>
                    <td className="text-muted">{d.result}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </Panel>
  )
}

/** 事件账本：钱赚/亏在哪 */
function EventLedgerPanel({ el }: { el?: Rec }) {
  if (!el || !el.available) return null
  const groups: Array<[string, string, string]> = [
    ['gain_events', '今日赚钱的票', 'text-bull'],
    ['loss_events', '今日亏钱的票', 'text-bear'],
    ['split_events', '反复开板（分歧）', 'text-warning'],
  ]
  return (
    <Panel title="事件账本" hint="今天钱赚在哪、亏在哪、哪里在分歧（最高标/题材首封 vs 断板/炸板/跌停）">
      <div className="grid gap-3 lg:grid-cols-3">
        {groups.map(([key, label, color]) => {
          const evs = (el[key] ?? []) as Rec[]
          if (evs.length === 0) return <div key={key} className="text-xs text-muted">{label}：—</div>
          return (
            <div key={key} className="rounded-btn border border-border/60 bg-elevated p-3">
              <div className={cn('mb-2 text-xs font-semibold', color)}>{label}（{evs.length}）</div>
              <div className="max-h-48 space-y-1 overflow-y-auto">
                {evs.map((e: Rec, i: number) => (
                  <div key={i} className="flex items-center gap-1.5 text-[11px]">
                    <span className="font-semibold text-foreground">{e.name}</span>
                    <span className="text-muted">{e.board}</span>
                    <span className={cn('ml-auto font-mono', pctColor(e.ret))}>{signed(e.ret)}</span>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </Panel>
  )
}

/** 题材结构（题材树不可用时仍渲染题材结构，树缺失只提示不吞掉结构） */
function ThemePanel({ ts, tree }: { ts?: Rec; tree?: Rec }) {
  return (
    <>
      {/* 题材事件树（问财）——不可用只提示，不拦截下面的题材结构 */}
      {tree && tree.available === false && (
        <Panel title="题材事件树" icon={ListTree}>
          <div className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
            {tree.reason || '题材串不可用'}。未配置问财接口密钥，题材事件树缺失。
          </div>
        </Panel>
      )}
      {ts && ts.available && <ThemeStructurePanel ts={ts} />}
    </>
  )
}

/** 题材结构 · 发酵节奏 */
function ThemeStructurePanel({ ts }: { ts: Rec }) {
  const themes = (ts.themes ?? []) as Rec[]
  const timeline = (ts.timeline ?? []) as Rec[]
  const maxSlot = Math.max(1, ...timeline.map(x => Number(x.count) || 0))
  return (
    <Panel title="题材结构 · 发酵节奏" badge={ts.theme_count != null ? `${ts.theme_count} 个题材` : undefined} hint="行业≠题材：同一行业可能装着完全不同的炒作原因。这里的主要价值在「涨停时间分布」——发酵节奏">
      {themes.length === 0 ? (
        <div className="text-xs text-muted">无题材数据</div>
      ) : (
        <div className="mb-3 space-y-1">
          {themes.map((t: Rec) => (
            <div key={t.sector} className="flex items-center gap-2 text-[12px] tabular-nums">
              <span className="w-20 shrink-0 truncate font-semibold text-foreground" title={t.sector}>{t.sector}</span>
              {/* 小竖条：每只涨停一根 */}
              <span className="flex gap-0.5">
                {Array.from({ length: Math.min(t.limit_up ?? 0, 12) }).map((_, i) => (
                  <span key={i} className="h-3 w-1.5 rounded-sm bg-accent/70" />
                ))}
              </span>
              <span className="text-muted">
                {t.limit_up}只 · 最高{t.highest}板
                {(t.broken ?? 0) > 0 && <span className="text-bear"> · 炸{t.broken}</span>}
                {t.first_seal && ` · 首封 ${hhmm(t.first_seal)}`}
              </span>
              {/* 成分票 */}
              {(t.names?.length ?? 0) > 0 && (
                <span className="truncate text-[10px] text-muted" title={(t.names as Rec[]).map((x: Rec) => `${x.name}(${x.boards}板)`).join('、')}>
                  {(t.names as Rec[]).slice(0, 3).map((x: Rec) => `${x.name}${x.boards}板`).join(' ')}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
      {/* 涨停时间分布（发酵节奏） */}
      {timeline.length > 0 && (
        <div className="border-t border-border pt-2.5">
          <div className="mb-1.5 text-[11px] font-semibold text-muted">涨停时间分布（按首次封板）</div>
          <div className="flex items-end gap-1.5">
            {timeline.map((x: Rec) => (
              <div key={x.slot} className="flex-1 text-center">
                <div className="mx-auto w-full rounded-t bg-accent/60" style={{ height: `${Math.max(3, (Number(x.count) || 0) / maxSlot * 44)}px` }} />
                <div className="mt-1 text-[10px] tabular-nums text-foreground">{x.count}</div>
                <div className="text-[9px] leading-tight text-muted">{x.slot}</div>
              </div>
            ))}
          </div>
        </div>
      )}
      {/* 头部集中度 */}
      {ts.concentration != null && (
        <p className="mt-2 text-[11px] text-muted">
          共 {ts.theme_count} 个方向，头部占全市场涨停 <b className="text-foreground">{pct(ts.concentration)}</b>
          {ts.concentration >= 0.3 ? '（一枝独秀）' : ts.concentration <= 0.15 ? '（高度分散）' : ''}
        </p>
      )}
    </Panel>
  )
}

/** 折线小图：干净折线 + 末点高亮（VIBE 原版 spark 风格） */
function SparkVals({ vals }: { vals: Array<number | null> }) {
  const known = vals.filter((v): v is number => v != null && Number.isFinite(v))
  if (known.length < 2) {
    return <div className="h-8 pt-2 text-[10px] text-muted">样本不足</div>
  }
  const min = Math.min(...known)
  const max = Math.max(...known)
  const rng = (max - min) || 1
  const W = 100, H = 28
  const last = vals[vals.length - 1]
  const mid = (min + max) / 2
  const hot = last == null ? null : last >= mid
  // 注意：TSP 主题把 bull/bear 定义成 HSL 分量（--bull: 4 87% 60%），
  // 必须用 hsl(var(--bull)) 包裹才是有效颜色；直接 var(--bull, #xxx) 会拿到裸分量导致无色。
  const color = hot == null ? 'currentColor' : hot ? 'hsl(var(--bull))' : 'hsl(var(--bear))'

  const x = (i: number) => (i / (vals.length - 1)) * W
  const y = (v: number) => H - ((v - min) / rng) * H

  // 折线段（null 处断开）
  const segs: string[] = []
  let cur: string[] = []
  vals.forEach((v, i) => {
    if (v == null) { if (cur.length > 1) segs.push(cur.join(' ')); cur = []; return }
    cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  })
  if (cur.length > 1) segs.push(cur.join(' '))

  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-8 w-full">
      {segs.map((pts, i) => (
        <polyline key={i} points={pts} fill="none" strokeWidth="2"
          stroke={color} strokeLinecap="round" strokeLinejoin="round"
          vectorEffect="non-scaling-stroke" />
      ))}
      {/* 末点高亮 */}
      {last != null && (
        <circle cx={x(vals.length - 1)} cy={y(last)} r="2.5" fill={color}
          vectorEffect="non-scaling-stroke" />
      )}
    </svg>
  )
}

/** 近5日趋势：每个指标一张折线小图 */
function TrendPanel({ tr }: { tr?: Rec }) {
  if (!tr || !tr.available) return null
  const days = (tr.days ?? []) as string[]
  const metrics = (tr.metrics ?? []) as Rec[]
  if (days.length === 0 || metrics.length === 0) return null
  const fmtVal = (v: number | null, m: Rec) =>
    v == null ? '—' : m.kind === 'rate' ? pct(v) : m.kind === 'pct' ? signed(v) : String(v)
  return (
    <Panel title="最近几天怎么走的" icon={TrendingUp} hint={`近 ${days.length} 个交易日 · ${days[0]?.slice(5).replace('-', '/')} → ${days[days.length - 1]?.slice(5).replace('-', '/')} · 线只画走势，不做预测`}>
      <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
        {metrics.slice(0, 6).map((m: Rec) => {
          const vals = (m.values ?? []) as Array<number | null>
          const last = vals[vals.length - 1] ?? null
          const prev = vals.length > 1 ? vals[vals.length - 2] : null
          const delta = last != null && prev != null ? last - prev : null
          return (
            <div key={m.key} className="rounded-btn border border-border/60 bg-elevated p-2.5">
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <span className="truncate text-[12px] font-medium text-secondary">{m.label}</span>
                <span className="shrink-0 text-[13px] font-bold tabular-nums text-foreground">
                  {fmtVal(last, m)}
                  {delta != null && Math.abs(delta) > 1e-9 && (
                    <span className="ml-1 text-[10px] font-normal text-muted">{delta > 0 ? '↑' : '↓'}</span>
                  )}
                </span>
              </div>
              <div className="text-secondary"><SparkVals vals={vals} /></div>
            </div>
          )
        })}
      </div>
    </Panel>
  )
}

/** 历史统计位置 */
function StatsContextPanel({ sc }: { sc?: Rec }) {
  if (!sc || !sc.available) return null
  const items = (sc.items ?? []) as Rec[]
  return (
    <Panel title="历史统计位置" hint="读数在全市场的分位——位置比数字直观，数字谁都能看">
      <div className="space-y-1.5">
        {items.slice(0, 8).map((it: Rec) => (
          <div key={it.key} className="flex flex-wrap items-center gap-2 text-[12px] tabular-nums">
            <span className="w-24 shrink-0 text-muted">{it.label}</span>
            <span className="w-14 shrink-0 font-bold text-secondary">{it.value_text}</span>
            {it.percentile != null ? (
              <>
                {/* 分位条：位置比数字直观 */}
                <span className="relative h-1.5 w-24 overflow-hidden rounded-full bg-muted">
                  <span className={cn('absolute inset-y-0 left-0 rounded-full',
                    it.extreme === '偏热' ? 'bg-bull' : it.extreme === '偏冷' ? 'bg-bear' : 'bg-accent/60')}
                    style={{ width: `${Math.max(2, it.percentile * 100)}%` }} />
                </span>
                <span className="w-10 text-muted">{Math.round(it.percentile * 100)}%</span>
                {it.extreme && (
                  <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-bold',
                    it.extreme === '偏热' ? 'bg-bull/15 text-bull' : 'bg-bear/15 text-bear')}>
                    {it.extreme}
                  </span>
                )}
                {it.hist_median != null && (
                  <span className="text-[10px] text-muted">历史中位 {it.hist_median}</span>
                )}
              </>
            ) : (
              <span className="text-[10px] text-muted">无分位</span>
            )}
          </div>
        ))}
      </div>
    </Panel>
  )
}

/** 与昨日对比 */
function DayDiffPanel({ dd }: { dd?: Rec }) {
  if (!dd || !dd.available) return null
  const changes = (dd.changes ?? []) as Rec[]
  return (
    <Panel title={`较昨日（${dd.prev_date ?? ''}）`} icon={ArrowLeftRight}>
      <div className="space-y-1.5">
        {changes.slice(0, 8).map((c: Rec) => (
          <div key={c.key} className="flex items-center justify-between text-[11px]">
            <span className="text-muted">{c.label}</span>
            <span className="font-mono text-secondary">
              <span className="text-muted">{c.prev_text}</span>
              <span className="mx-1 text-muted">→</span>
              <b className={c.hotter ? 'text-bull' : 'text-bear'}>{c.cur_text}</b>
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/** 明日验证条件 */
function VerificationPanel({ items }: { items?: Rec[] }) {
  if (!items || items.length === 0) return null
  return (
    <Panel title="明日验证条件" icon={Target} hint="把今晚的判断变成明天能对账的东西——每条带今日基准值和变动阈值">
      <div className="grid gap-2 md:grid-cols-2">
        {items.map((v: Rec, i: number) => (
          <div key={i} className="rounded-btn border border-border/60 bg-elevated p-3">
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-semibold text-foreground">{v.label || v.metric}</span>
              <span className="text-[10px] text-muted">{v.unit ? (v.unit === '%' ? '百分数' : '') : '比率'}</span>
            </div>
            <div className="mt-1 text-[11px] text-muted">{v.reason || v.metric}</div>
            <div className="mt-1.5 flex items-center gap-2 text-[11px]">
              <span className="text-muted">基准</span>
              <span className="font-mono text-secondary">
                {v.base_value == null ? '—' : v.unit === '%' ? `${v.base_value > 0 ? '+' : ''}${v.base_value.toFixed(2)}%` : !v.unit ? pct(v.base_value) : v.base_value}
              </span>
              <span className="text-muted">· 变动&gt;{v.eps == null ? '—' : v.unit === '%' ? `${v.eps}pp` : pct(v.eps)} 才算数</span>
            </div>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/** 用户自定义验证条件（自己写下的，次日才想回来看对没对）*/
function UserConditionsPanel({ date }: { date: string }) {
  const [open, setOpen] = useState(false)
  const [menu, setMenu] = useState<{ metrics: Rec[]; directions: string[] } | null>(null)
  const [items, setItems] = useState<Rec[]>([])
  const [msg, setMsg] = useState('')

  useEffect(() => {
    if (!open || !date) return
    let alive = true
    Promise.all([
      vibeApi.verificationMenu(),
      vibeApi.verificationItems(date),
    ]).then(([m, cur]) => {
      if (!alive) return
      setMenu(m)
      setItems(cur?.items ?? [])
    }).catch(e => setMsg(`读取失败: ${(e as Error).message}`))
    return () => { alive = false }
  }, [open, date])

  async function save() {
    try {
      const r = await vibeApi.verificationSave(date, items)
      setMsg(r?.error ? r.error : r?.ok ? '已保存，次日自动核验' : '保存失败')
    } catch (e) {
      setMsg(`保存失败: ${(e as Error).message}`)
    }
  }

  if (!open) {
    return (
      <Panel title="自定义验证条件" icon={CheckSquare} badge="选填">
        <button onClick={() => setOpen(true)} className="text-xs text-accent underline-offset-2 hover:underline">
          + 自己加一条验证条件（自己写下的，明天才会想回来看对没对）
        </button>
      </Panel>
    )
  }
  return (
    <Panel title="自定义验证条件" icon={CheckSquare} hint="同一指标以你写的为准，次日与 AI 的一起核验">
      {items.map((it, i) => (
        <div key={i} className="mb-1.5 flex flex-wrap items-center gap-1.5">
          <select value={it.metric}
            onChange={e => setItems(items.map((x, j) => j === i ? { ...x, metric: e.target.value } : x))}
            className="h-8 rounded-btn border border-border bg-elevated px-2 text-xs text-secondary">
            {menu?.metrics.map(m => <option key={m.key} value={m.key} title={m.hint}>{m.label}</option>)}
          </select>
          <select value={it.direction}
            onChange={e => setItems(items.map((x, j) => j === i ? { ...x, direction: e.target.value } : x))}
            className="h-8 rounded-btn border border-border bg-elevated px-2 text-xs text-secondary">
            {menu?.directions.map(d => <option key={d} value={d}>预期{d}</option>)}
          </select>
          <input value={it.reason} placeholder="为什么（可选）"
            onChange={e => setItems(items.map((x, j) => j === i ? { ...x, reason: e.target.value } : x))}
            className="h-8 min-w-[160px] flex-1 rounded-btn border border-border bg-elevated px-2 text-xs text-secondary" />
          <button onClick={() => setItems(items.filter((_, j) => j !== i))} className="text-muted hover:text-bear">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          onClick={() => setItems([...items, { metric: menu?.metrics?.[0]?.key || 'limit_up_count', direction: '上升', reason: '' }])}
          disabled={items.length >= 8}
          className="inline-flex h-8 items-center gap-1 rounded-btn border border-border px-2.5 text-xs text-secondary hover:bg-elevated disabled:opacity-40">
          <Plus className="h-3.5 w-3.5" /> 加一条
        </button>
        <button onClick={save}
          className="inline-flex h-8 items-center rounded-btn bg-accent px-3 text-xs font-semibold text-white hover:bg-accent/90">
          保存
        </button>
        {msg && <span className="text-[11px] text-muted">{msg}</span>}
      </div>
    </Panel>
  )
}

/** 追问 AI：就今天的复盘对话（走 /api/review/chat）*/
function ReviewChatPanel() {
  const [msgs, setMsgs] = useState<Array<{ role: string; content: string }>>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  const suggestions = [
    '最强主线今天的梯队结构是怎样的',
    '当前情绪处在周期哪个阶段',
    '各方向的龙头晋级还是断板了',
    '判断退潮要盯哪些数据信号',
  ]

  async function send(q?: string) {
    const text = (q ?? input).trim()
    if (!text || loading) return
    const next = [...msgs, { role: 'user', content: text }]
    setMsgs(next)
    setInput('')
    setLoading(true)
    try {
      const r = await vibeApi.reviewChat(next)
      setMsgs([...next, { role: 'assistant', content: r.answer || `出错：${r.error || '未知'}` }])
    } catch (e) {
      setMsgs([...next, { role: 'assistant', content: `请求失败：${(e as Error).message}` }])
    }
    setLoading(false)
  }

  return (
    <Panel title="追问 AI" icon={MessageCircle} badge="就今天的复盘追问，仅供参考">
      {msgs.length === 0 && (
        <div className="mb-3 flex flex-wrap gap-2">
          {suggestions.map((s, i) => (
            <button key={i} onClick={() => send(s)}
              className="rounded-full border border-border bg-elevated px-3 py-1 text-xs text-muted hover:text-foreground">
              {s}
            </button>
          ))}
        </div>
      )}
      {msgs.length > 0 && (
        <div className="mb-3 space-y-3">
          {msgs.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-sm leading-relaxed ${m.role === 'user' ? 'bg-accent text-white' : 'bg-elevated text-foreground'}`}>
                {m.content}
              </div>
            </div>
          ))}
          {loading && (
            <div className="flex items-center gap-2 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> 思考中…
            </div>
          )}
        </div>
      )}
      <div className="flex items-center gap-2">
        <input value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === 'Enter' && send()}
          placeholder="就今天的复盘追问，如：展开电网设备的接力逻辑"
          className="h-9 flex-1 rounded-btn border border-border bg-elevated px-3 text-sm text-secondary outline-none focus:border-accent/50" />
        <button onClick={() => send()} disabled={loading}
          className="inline-flex h-9 items-center gap-1 rounded-btn bg-accent px-3.5 text-sm font-semibold text-white hover:bg-accent/90 disabled:opacity-50">
          <Send className="h-4 w-4" />
        </button>
      </div>
    </Panel>
  )
}
