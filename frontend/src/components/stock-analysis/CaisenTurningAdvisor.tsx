import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bell, Check, Loader2, TrendingUp, TrendingDown, Minus, ArrowUp,
  Waves, Layers, ShieldAlert,
} from 'lucide-react'
import { toast } from '@/components/Toast'
import { api, genRuleId, type MonitorRule, type TurningAnalysis } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { LEVEL_GROUPS } from './AnalysisKChart'

/**
 * 蔡森·多空转折顾问 —— 个股分析页专用卡片(多形态深度版)。
 *
 * 数据源:GET /api/stock-analysis/turning(后端移植自 EvoFlow 蔡森·多空转折顾问
 * 的蔡森《多空转折一手抓》12型态方法论,确定性算法,不需要大模型)。
 *
 * 展示:
 *  - 主形态 + 多空结论(偏多/偏空/中性) + 置信度
 *  - 波段结构(第一波 → 回踩 → 第二波等幅目标)
 *  - 多形态识别列表(破底翻/W底/M头/假突破/箱体/三角形/趋势,逐条确认状态)
 *  - 关键三价(颈线 / 满足点 / 止损) + 完整关键价位表
 *  - 「点位加入提醒」→ 一键把分析点位创建为点位提醒(type=price 监控规则)
 */

interface Props {
  symbol: string
  name: string
  turning: TurningAnalysis | null | undefined
  loading?: boolean
}

const FORM_META: Record<string, { icon: typeof TrendingUp; cls: string; label: string }> = {
  '破底翻': { icon: TrendingUp, cls: 'text-bull border-bull/30 bg-bull/10', label: '空转多结构' },
  'W底/双底': { icon: TrendingUp, cls: 'text-bull border-bull/30 bg-bull/10', label: '底部结构' },
  '向上假突破': { icon: TrendingDown, cls: 'text-bear border-bear/30 bg-bear/10', label: '诱多陷阱' },
  'M头/双顶': { icon: TrendingDown, cls: 'text-bear border-bear/30 bg-bear/10', label: '顶部结构' },
  '箱体震荡': { icon: Minus, cls: 'text-muted border-border/40 bg-elevated/30', label: '震荡形态' },
  '收敛三角形': { icon: Minus, cls: 'text-amber-300 border-amber-400/30 bg-amber-400/10', label: '收敛待变' },
  '多头趋势': { icon: TrendingUp, cls: 'text-bull border-bull/30 bg-bull/10', label: '趋势形态' },
  '空头趋势': { icon: TrendingDown, cls: 'text-bear border-bear/30 bg-bear/10', label: '趋势形态' },
  '趋势整理': { icon: Minus, cls: 'text-sky-300 border-sky-400/30 bg-sky-400/10', label: '趋势形态' },
}

const VERDICT_META: Record<string, { text: string; cls: string }> = {
  bull: { text: '偏多', cls: 'text-bull border-bull/30 bg-bull/10' },
  bear: { text: '偏空', cls: 'text-bear border-bear/30 bg-bear/10' },
  neutral: { text: '中性', cls: 'text-muted border-border/40 bg-elevated/30' },
}

const CONF_CLS: Record<string, string> = {
  '中高': 'text-emerald-300 border-emerald-400/30 bg-emerald-400/10',
  '中': 'text-amber-300 border-amber-400/30 bg-amber-400/10',
  '低': 'text-muted border-border/40 bg-elevated/30',
}

const PATTERN_ICON: Record<string, typeof TrendingUp> = {
  break_below_reverse: TrendingUp,
  double_bottom: TrendingUp,
  fake_breakout: TrendingDown,
  double_top: TrendingDown,
  box: Minus,
  triangle: Minus,
  trend: Minus,
}

export function CaisenTurningAdvisor({ symbol, name, turning, loading }: Props) {
  const qc = useQueryClient()
  const [createdCount, setCreatedCount] = useState<number | null>(null)

  const form = turning?.form
  const meta = form ? FORM_META[form] ?? FORM_META['趋势整理'] : null
  const FormIcon = meta?.icon ?? Minus
  const verdictMeta = turning ? VERDICT_META[turning.verdict] : null

  // 关键三价:颈线 / 满足点 / 止损
  const keyPrices = useMemo(() => {
    if (!turning) return []
    const items: { label: string; value: number; tone: 'neck' | 'target' | 'stop' }[] = []
    if (turning.neckline != null) items.push({ label: '颈线', value: turning.neckline, tone: 'neck' })
    if (turning.target != null) items.push({ label: '满足点', value: turning.target, tone: 'target' })
    if (turning.stop != null) items.push({ label: '止损', value: turning.stop, tone: 'stop' })
    return items
  }, [turning])

  // 点位列表(排除 weak 的 MA 参考,聚焦形态点位 + 波段结构位)
  const alertPoints = useMemo(() => {
    const pts = turning?.points ?? []
    return pts.filter(p => p.strength !== 'weak')
  }, [turning])

  // 波段结构展示项
  const waveItems = useMemo(() => {
    const w = turning?.waves
    if (!w?.available) return []
    const items: { label: string; value: number; tone: 'bottom' | 'pullback' | 'first' | 'target' }[] = []
    if (w.bottom != null) items.push({ label: '波段底部', value: w.bottom, tone: 'bottom' })
    if (w.first_wave_high != null) items.push({ label: '第一波高', value: w.first_wave_high, tone: 'first' })
    if (w.pullback_low != null) items.push({ label: '回踩低点', value: w.pullback_low, tone: 'pullback' })
    if (w.second_wave_target != null) items.push({ label: '第二波目标', value: w.second_wave_target, tone: 'target' })
    return items
  }, [turning])

  // 收敛三角形信息
  const triangle = useMemo(() => turning?.patterns.find(p => p.key === 'triangle') ?? null, [turning])

  // 操作纪律分组
  const disciplineGroups = useMemo(() => {
    const d = turning?.discipline
    if (!d) return []
    return [
      { key: 'entry', label: '观察区', cls: 'text-sky-300 border-sky-400/25 bg-sky-400/[0.06]', items: d.entry },
      { key: 'add', label: '确认/加码', cls: 'text-bull border-bull/25 bg-bull/[0.06]', items: d.add },
      { key: 'reduce', label: '减码区', cls: 'text-purple-300 border-purple-400/25 bg-purple-400/[0.06]', items: d.reduce },
      { key: 'stop', label: '防守位', cls: 'text-bear border-bear/25 bg-bear/[0.06]', items: d.stop },
    ].filter(g => g.items && g.items.length > 0)
  }, [turning])

  const addAlerts = useMutation({
    mutationFn: async () => {
      const existingRes = await api.monitorRulesList()
      const existing = existingRes.rules ?? []
      let created = 0
      let skipped = 0
      for (const level of alertPoints) {
        const isResistance = level.side === 'resistance'
        const target = level.value
        const dup = existing.some(rule =>
          rule.type === 'price'
          && rule.scope === 'symbols'
          && rule.symbols.length === 1
          && rule.symbols[0] === symbol
          && rule.conditions.length === 1
          && rule.conditions[0].field === 'close'
          && rule.conditions[0].value != null
          && Math.abs(Number(rule.conditions[0].value) - target) < 0.005
        )
        if (dup) { skipped++; continue }
        const rule: MonitorRule = {
          id: genRuleId(),
          name: `蔡森形态 · ${name || symbol} · ${level.label}${isResistance ? '上穿' : '下穿'}${target.toFixed(2)}`,
          enabled: true,
          type: 'price',
          asset_type: 'stock',
          scope: 'symbols',
          symbols: [symbol],
          sector: null,
          strategy_id: null,
          direction: 'entry',
          conditions: [{ field: 'close', op: isResistance ? '>=' : '<=', value: target }],
          logic: 'and',
          cooldown_seconds: 3600,
          severity: 'warn',
          message: `${name || symbol} 蔡森形态「${form}」${level.label} ${isResistance ? '上穿' : '下穿'} ${target.toFixed(2)}`,
          webhook_channels: [],
        }
        await api.monitorRuleSave(rule)
        created++
      }
      return { created, skipped }
    },
    onSuccess: ({ created, skipped }) => {
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      setCreatedCount(created)
      toast(
        created > 0
          ? `已创建 ${created} 条点位提醒${skipped > 0 ? `(跳过 ${skipped} 条重复)` : ''}`
          : '点位提醒均已存在,无需重复创建',
        created > 0 ? 'success' : undefined,
      )
    },
    onError: error => toast(String((error as Error)?.message || '创建失败'), 'error'),
  })

  if (loading) {
    return (
      <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-4 py-3 border-b border-border/40 flex items-center gap-2">
          <Bell className="h-4 w-4 text-fuchsia-400 shrink-0" />
          <span className="text-sm font-medium text-foreground">蔡森·多空转折顾问</span>
        </div>
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </div>
      </div>
    )
  }

  if (!turning) {
    return (
      <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-4 py-3 border-b border-border/40 flex items-center gap-2">
          <Bell className="h-4 w-4 text-fuchsia-400 shrink-0" />
          <span className="text-sm font-medium text-foreground">蔡森·多空转折顾问</span>
        </div>
        <div className="px-4 py-8 text-center">
          <p className="text-xs text-muted">暂无蔡森形态分析数据</p>
        </div>
      </div>
    )
  }

  const color = LEVEL_GROUPS.find(g => g.key === 'turning')?.color ?? '#E879F9'

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      {/* 头部:主形态 + 多空结论 + 置信度 */}
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2 min-w-0">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-md" style={{ backgroundColor: color + '1a', color }}>
              <Bell className="h-3.5 w-3.5" />
            </span>
            <span className="text-sm font-medium text-foreground">蔡森·多空转折顾问</span>
            <span className="text-[10px] text-muted">《多空转折一手抓》12型态</span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {meta && (
              <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[10px] font-medium ${meta.cls}`}>
                <FormIcon className="h-3 w-3" />
                {form}
              </span>
            )}
            {verdictMeta && (
              <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[10px] font-medium ${verdictMeta.cls}`}>
                {verdictMeta.text}
              </span>
            )}
            <span className={`px-1.5 py-0.5 rounded border text-[10px] ${CONF_CLS[turning.confidence] ?? CONF_CLS['低']}`}>
              置信 {turning.confidence}
            </span>
          </div>
        </div>
        {turning.summary && (
          <div className="mt-1.5 text-[11px] text-muted truncate">{turning.summary}</div>
        )}
      </div>

      <div className="p-3 space-y-3">
        {/* 形态研判 */}
        {turning.desc && (
          <div className="rounded-lg border border-border/40 bg-base/30 px-3 py-2">
            <p className="text-[11px] text-secondary leading-relaxed">{turning.desc}</p>
          </div>
        )}

        {/* 波段结构 */}
        {waveItems.length > 0 && (
          <div className="rounded-lg border border-border/40 bg-base/20 px-3 py-2">
            <div className="flex items-center gap-1.5 text-[10px] text-muted mb-1.5">
              <Waves className="h-3 w-3" />
              波段结构(第一波 → 回踩 → 第二波等幅)
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
              {waveItems.map(w => (
                <div key={w.label} className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">{w.label}</div>
                  <div className={`font-mono text-xs font-bold ${
                    w.tone === 'target' ? 'text-purple-300' : w.tone === 'bottom' ? 'text-bull' : 'text-foreground'
                  }`}>
                    {w.value.toFixed(2)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 收敛三角形(斜线 + 动态/静态口径) */}
        {triangle && (
          <div className="rounded-lg border border-border/40 bg-base/20 px-3 py-2">
            <div className="flex items-center gap-1.5 text-[10px] text-muted mb-1.5">
              <Layers className="h-3 w-3" />
              收敛三角形
              {triangle.confirmed && <span className="px-1 py-0.5 rounded border text-[9px] text-bull border-bull/30 bg-bull/10">已突破上缘</span>}
            </div>
            <p className="text-[10px] text-secondary leading-relaxed mb-1.5">{triangle.desc}</p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
              {triangle.dyn_up != null && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">动态上缘</div>
                  <div className="font-mono text-xs font-bold text-orange-300">{triangle.dyn_up.toFixed(2)}</div>
                </div>
              )}
              {triangle.dyn_lo != null && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">动态下缘</div>
                  <div className="font-mono text-xs font-bold text-emerald-300">{triangle.dyn_lo.toFixed(2)}</div>
                </div>
              )}
              {triangle.height != null && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">当日高度</div>
                  <div className="font-mono text-xs font-bold text-foreground">{triangle.height.toFixed(2)}</div>
                </div>
              )}
              {triangle.dyn_target != null && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">动态满足(近)</div>
                  <div className="font-mono text-xs font-bold text-purple-300">{triangle.dyn_target.toFixed(2)}</div>
                </div>
              )}
              {triangle.static_target != null && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">静态满足(远)</div>
                  <div className="font-mono text-xs font-bold text-indigo-300">{triangle.static_target.toFixed(2)}</div>
                </div>
              )}
              {triangle.apex_val != null && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] text-muted">收敛尖点</div>
                  <div className="font-mono text-xs font-bold text-amber-300">{triangle.apex_val.toFixed(2)}</div>
                </div>
              )}
            </div>
            <p className="mt-1.5 text-[9px] text-muted/70">
              上缘/下缘斜线已画在 K 线图(蔡森形态组)。动态口径满足 = 突破位 + 当日高度;静态口径 = 站上颈线后等幅,递进参考。
            </p>
          </div>
        )}

        {/* 多形态识别列表 */}
        {turning.patterns.length > 0 && (
          <div className="rounded-lg border border-border/40 bg-base/20 overflow-hidden">
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-border/30">
              <Layers className="h-3 w-3 text-muted" />
              <span className="text-[10px] text-muted">12型态识别({turning.patterns.length})</span>
            </div>
            <div className="divide-y divide-border/30">
              {turning.patterns.map((p, i) => {
                const Icon = PATTERN_ICON[p.key] ?? Minus
                const dirCls = p.direction === 'bull' ? 'text-bull'
                  : p.direction === 'bear' ? 'text-bear' : 'text-muted'
                // v3.1 三态徽章: 有效 / 已完成 / 已失效
                const statusMeta = p.status === 'done'
                  ? { label: '已完成', cls: 'text-amber-300 border-amber-400/40 bg-amber-400/10' }
                  : p.status === 'invalid'
                    ? { label: '已失效', cls: 'text-bear border-bear/40 bg-bear/10' }
                    : null
                const detailLines: string[] = []
                if (p.break_position) detailLines.push(p.break_position)
                if (p.key === 'break_below_reverse') {
                  if (p.time_wave_status) detailLines.push(`时间波:${p.time_wave_status}`)
                  if (p.vol_note) detailLines.push(p.vol_note)
                  if (p.target_basis) detailLines.push(p.target_basis)
                }
                return (
                  <div key={i} className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <Icon className={`h-3 w-3 shrink-0 ${dirCls}`} />
                      <span className="text-[11px] font-medium text-foreground">{p.name}</span>
                      <span className={`px-1.5 py-0.5 rounded border text-[9px] shrink-0 ${
                        p.confirmed
                          ? p.direction === 'bull' ? 'text-bull border-bull/30 bg-bull/10'
                            : p.direction === 'bear' ? 'text-bear border-bear/30 bg-bear/10'
                            : 'text-muted border-border/40 bg-elevated/30'
                          : 'text-muted border-border/40 bg-elevated/30'
                      }`}>
                        {p.confirmed ? '✓ 确认' : '观察中'}
                      </span>
                      {statusMeta && (
                        <span className={`px-1.5 py-0.5 rounded border text-[9px] shrink-0 ${statusMeta.cls}`}>
                          {statusMeta.label}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[10px] text-secondary leading-relaxed">{p.desc}</p>
                    {detailLines.length > 0 && (
                      <div className="mt-1 space-y-0.5">
                        {detailLines.map((line, j) => (
                          <p key={j} className="text-[9px] text-muted leading-relaxed">{line}</p>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* 关键三价:颈线 / 满足点 / 止损 */}
        {keyPrices.length > 0 && (
          <div className="grid grid-cols-3 gap-2">
            {keyPrices.map(kp => (
              <div key={kp.label} className="rounded-lg border border-border/40 bg-base/20 px-2.5 py-2">
                <div className="flex items-center gap-1 text-[10px] text-muted">
                  {kp.tone === 'neck' && <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />}
                  {kp.tone === 'target' && <ArrowUp className="h-2.5 w-2.5 text-purple-400" />}
                  {kp.tone === 'stop' && <ShieldAlert className="h-2.5 w-2.5 text-emerald-400" />}
                  {kp.label}
                </div>
                <div className={`mt-0.5 font-mono text-sm font-bold ${
                  kp.tone === 'neck' ? 'text-foreground'
                    : kp.tone === 'target' ? 'text-purple-300'
                    : 'text-emerald-300'
                }`}>
                  {kp.value.toFixed(2)}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* 操作纪律(确定性规则式:观察区/加码/减码/防守位 + 铁律) */}
        {(disciplineGroups.length > 0 || (turning.discipline?.rules?.length ?? 0) > 0) && (
          <div className="rounded-lg border border-border/40 bg-base/20 overflow-hidden">
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-border/30">
              <ShieldAlert className="h-3 w-3 text-muted" />
              <span className="text-[10px] text-muted">操作纪律(结构位 · 非投资建议)</span>
            </div>
            <div className="p-2 space-y-1.5">
              {disciplineGroups.map(g => (
                <div key={g.key} className={`rounded-md border px-2 py-1.5 ${g.cls}`}>
                  <div className="text-[9px] font-medium mb-1">{g.label}</div>
                  <div className="space-y-0.5">
                    {g.items.map((it, i) => (
                      <div key={i} className="flex items-start gap-2">
                        <span className="text-[10px] text-secondary shrink-0 w-28 truncate">{it.label}</span>
                        <span className="font-mono text-[10px] font-bold text-foreground shrink-0">{it.value.toFixed(2)}</span>
                        <span className="text-[9px] text-muted truncate flex-1">{it.note}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {(turning.discipline?.rules?.length ?? 0) > 0 && (
                <div className="rounded-md border border-border/30 bg-base/40 px-2 py-1.5">
                  <div className="text-[9px] font-medium text-amber-300 mb-1">铁律</div>
                  <ul className="space-y-0.5">
                    {turning.discipline!.rules.map((r, i) => (
                      <li key={i} className="text-[9px] text-secondary leading-relaxed">· {r}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>
        )}

        {/* 分析点位列表 + 加入提醒 */}
        {alertPoints.length > 0 && (
          <div className="rounded-lg border border-border/40 bg-base/20 overflow-hidden">
            <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/30">
              <span className="text-[10px] text-muted">分析点位({alertPoints.length})</span>
              <button
                onClick={() => addAlerts.mutate()}
                disabled={addAlerts.isPending}
                className="inline-flex items-center gap-1 h-6 px-2 rounded-md bg-fuchsia-500/15 border border-fuchsia-400/30 text-[10px] font-medium text-fuchsia-300 hover:bg-fuchsia-500/25 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                title="把分析点位(颈线/满足点/止损等)加入点位提醒"
              >
                {addAlerts.isPending ? <Loader2 className="h-3 w-3 animate-spin" />
                  : createdCount != null ? <Check className="h-3 w-3" /> : <Bell className="h-3 w-3" />}
                {createdCount != null ? `已加入 ${createdCount} 条` : '点位加入提醒'}
              </button>
            </div>
            <div className="divide-y divide-border/30">
              {alertPoints.map((p, i) => (
                <div key={i} className="flex items-center gap-2 px-3 py-1.5">
                  <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
                  <span className="text-[11px] text-secondary flex-1 min-w-0 truncate">{p.label}</span>
                  <span className={`text-[11px] font-mono ${p.side === 'resistance' ? 'text-bear' : p.side === 'support' ? 'text-bull' : 'text-foreground'}`}>
                    {p.value.toFixed(2)}
                  </span>
                  <span className="text-[9px] text-muted shrink-0 w-8 text-right">
                    {p.side === 'resistance' ? '压力' : p.side === 'support' ? '支撑' : '中性'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        <p className="text-[10px] text-muted/60 leading-relaxed">
          形态识别移植自《多空转折一手抓》(蔡森)12型态方法,确定性算法即时计算,无需大模型;
          点位仅供技术研究参考,不构成投资建议。
        </p>
      </div>
    </div>
  )
}
