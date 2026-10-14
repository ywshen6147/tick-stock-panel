import { useEffect, useMemo, useState, type ComponentType } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart3,
  CalendarDays,
  Database,
  Layers3,
  Search,
  Tags,
  TrendingUp,
  Users,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { api, type AnalysisColumn, type ExtDataConfig, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface DimensionAnalysisProps {
  menuId?: string
  title?: string
  subtitle?: string
  kindLabel?: string
  emptyHint?: string
  accentClass?: string
  keywords?: string[]
  fallbackFieldNames?: string[]
}

interface DimensionGroup {
  key: string
  count: number
  rows: Record<string, any>[]
  metrics: Record<string, number | null>
}

const PAGE_LIMIT = 5000
const SEPARATORS = /[、,，;；|/\s]+/

function normalizeText(v: unknown): string {
  if (v == null) return ''
  return String(v).trim()
}

function fieldLabel(fields: ExtDataField[], name: string): string {
  return fields.find(f => f.name === name)?.label || name
}

function isNumericField(f: ExtDataField) {
  return f.dtype === 'int' || f.dtype === 'float'
}

function scoreConfig(config: ExtDataConfig, keywords: string[]) {
  const haystack = [
    config.id,
    config.label,
    config.description ?? '',
    ...config.fields.flatMap(f => [f.name, f.label]),
  ].join(' ').toLowerCase()
  return keywords.reduce((n, k) => n + (haystack.includes(k.toLowerCase()) ? 1 : 0), 0)
}

function pickDefaultConfig(configs: ExtDataConfig[], keywords: string[]) {
  const ranked = [...configs].sort((a, b) => scoreConfig(b, keywords) - scoreConfig(a, keywords))
  return ranked[0]?.id ?? ''
}

function pickDefaultDimensionField(config: ExtDataConfig | undefined, fallbackFieldNames: string[]) {
  if (!config) return ''
  const fields = config.fields.filter(f => f.name !== 'symbol' && f.name !== 'code')
  for (const name of fallbackFieldNames) {
    const matched = fields.find(f => f.name.toLowerCase().includes(name) || f.label.toLowerCase().includes(name))
    if (matched) return matched.name
  }
  return fields.find(f => !isNumericField(f))?.name ?? fields[0]?.name ?? ''
}

function formatNumber(v: unknown, digits = 2) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function formatValue(v: unknown, col?: AnalysisColumn) {
  if (v == null || v === '') return '—'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v !== 'number') return String(v)

  const digits = col?.precision ?? (col?.type === 'number' || col?.type === 'amount' || col?.type === 'percent' ? 2 : 2)
  if (col?.type === 'percent') return `${formatNumber(v, digits)}%`
  if (col?.type === 'amount' || col?.format === 'amount') return formatNumber(v, digits)
  return formatNumber(v, digits)
}

function splitDimensionValues(value: unknown) {
  const text = normalizeText(value)
  if (!text) return []
  return text.split(SEPARATORS).map(s => s.trim()).filter(Boolean)
}

function StatCard({ label, value, hint, icon: Icon }: {
  label: string
  value: string | number
  hint: string
  icon: ComponentType<{ className?: string }>
}) {
  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted">{label}</span>
        <Icon className="h-4 w-4 text-secondary" />
      </div>
      <div className="mt-3 text-2xl font-semibold tracking-tight text-foreground">{value}</div>
      <div className="mt-1 text-[11px] text-muted">{hint}</div>
    </div>
  )
}

function columnsFromFields(fields: ExtDataField[], dimensionField: string, rows: Record<string, any>[]) {
  const numericFields = fields.filter(f => isNumericField(f) && rows.some(r => typeof r[f.name] === 'number')).slice(0, 4)
  return [
    ...fields.filter(f => ['symbol', 'code', 'name', '股票简称', '股票代码'].includes(f.name)),
    ...fields.filter(f => f.name === dimensionField),
    ...numericFields,
  ]
    .filter((f, i, arr) => arr.findIndex(x => x.name === f.name) === i)
    .map<AnalysisColumn>(f => ({
      field: f.name,
      label: f.label || f.name,
      type: isNumericField(f) ? 'number' : 'string',
      precision: f.dtype === 'float' ? 2 : null,
      sortable: isNumericField(f),
      visible: true,
    }))
}

function aggregate(rows: Record<string, any>[], col: AnalysisColumn) {
  if (col.field === '__count') return rows.length
  const values = rows.map(r => r[col.field]).filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  if (!values.length) return null
  switch (col.aggregate) {
    case 'sum': return values.reduce((a, b) => a + b, 0)
    case 'min': return Math.min(...values)
    case 'max': return Math.max(...values)
    case 'avg':
    default:
      return values.reduce((a, b) => a + b, 0) / values.length
  }
}

export function ExtDimensionAnalysis({
  menuId,
  title,
  subtitle,
  kindLabel = '维度',
  emptyHint = '还没有可用于分析的扩展数据。',
  accentClass = 'bg-[radial-gradient(circle_at_top_right,rgba(59,130,246,0.16),transparent_36%)]',
  keywords = [],
  fallbackFieldNames = [],
}: DimensionAnalysisProps) {
  const configs = useQuery({ queryKey: QK.extData, queryFn: api.extDataList })
  const menuQuery = useQuery({
    queryKey: QK.analysisMenu(menuId ?? ''),
    queryFn: () => api.analysisMenu(menuId!),
    enabled: !!menuId,
  })
  const menu = menuQuery.data

  const [dimensionField, setDimensionField] = useState('')
  const [search, setSearch] = useState('')
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null)
  // 列头点击排序 (覆盖菜单 default_sort); 「分类」选择的分组字段 (table 模式进入分组榜单)。
  const [userSort, setUserSort] = useState<{ field: string; order: 'asc' | 'desc' } | null>(null)
  const [groupBy, setGroupBy] = useState('')

  // 切换页面 (menuId 变化) 时重置本地选择, 避免上一页的选择残留到新菜单。
  useEffect(() => {
    setDimensionField('')
    setSelectedGroup(null)
    setSearch('')
    setUserSort(null)
    setGroupBy('')
  }, [menuId])

  const availableConfigs = configs.data?.items ?? []
  // 数据源由菜单/左侧导航决定 — 不再提供右上角切换器:
  // 每个信息面在侧栏都是独立入口, 切源会造成标题/列配置与实际表不一致的困惑。
  const isTableMode = menu?.template === 'table' || menu?.template === 'ranking'
  const activeConfigId = menu?.data_source || pickDefaultConfig(availableConfigs, keywords)
  const activeConfig = availableConfigs.find(c => c.id === activeConfigId)
  const activeDimensionField = menu?.dimension_field || dimensionField || pickDefaultDimensionField(activeConfig, fallbackFieldNames)
  // 分组字段: 分组模板页沿用菜单维度字段; table/ranking 页由明细列表的「分类」选择器决定 (默认不分组)。
  const activeGroupField = isTableMode
    ? groupBy
    : (menu?.dimension_field || dimensionField || pickDefaultDimensionField(activeConfig, fallbackFieldNames))
  const effectiveSort = userSort ?? menu?.default_sort ?? null
  const activeTitle = menu?.label || title || '扩展分析'
  const activeKindLabel = menu?.template === 'table' ? '数据' : kindLabel

  const configuredColumns = useMemo(() => {
    const cols = menu?.detail_columns?.filter(c => c.visible !== false) ?? []
    return cols.length > 0 ? cols : null
  }, [menu?.detail_columns])
  const requestedColumns = useMemo(() => {
    const cols = [activeDimensionField, ...(configuredColumns?.map(c => c.field) ?? [])].filter(Boolean)
    if (configuredColumns) return Array.from(new Set(cols))
    // 自动列模式 (菜单未配 detail_columns 的无菜单页): 声明字段全量请求,
    // 否则 rows 只有 symbol/code/name/维度字段, 自动列拿不到数据全渲染 "—"。
    return Array.from(new Set([...cols, ...(activeConfig?.fields.map(f => f.name) ?? [])]))
  }, [activeDimensionField, configuredColumns, activeConfig])
  const columnsKey = requestedColumns.join(',')

  const rowsQuery = useQuery({
    queryKey: QK.extDataRows(activeConfigId, undefined, PAGE_LIMIT, columnsKey),
    queryFn: () => api.extDataRows(activeConfigId, { limit: PAGE_LIMIT, columns: requestedColumns }),
    enabled: !!activeConfigId,
  })

  const rows = rowsQuery.data?.rows ?? []
  const baseFields = activeConfig?.fields ?? rowsQuery.data?.fields ?? []
  const fields = rows.some(r => r.name != null) && !baseFields.some(f => f.name === 'name')
    ? [...baseFields, { name: 'name', dtype: 'string', label: '名称' }]
    : baseFields
  const dimensionOptions = fields.filter(f => f.name !== 'symbol' && f.name !== 'code')
  const displayColumns = useMemo(() => {
    const auto = columnsFromFields(fields, activeDimensionField, rows)
    if (configuredColumns) return configuredColumns
    // 自动列只保留数据里真实存在的字段 (声明了但后端裁剪/落盘缺失的不显示 "—" 幽灵列)
    const probe = rows[0]
    return rows.length === 0 || !probe
      ? auto
      : auto.filter(c => ['symbol', 'code', 'name'].includes(c.field) || Object.prototype.hasOwnProperty.call(probe, c.field))
  }, [configuredColumns, fields, activeDimensionField, rows])
  const groupColumns = (menu?.group_columns?.length ? menu.group_columns : [
    { field: '__dimension', label: activeKindLabel },
    { field: '__count', label: '数量', type: 'number' as const, sortable: true },
  ]).filter(c => c.visible !== false)

  const sortedRows = useMemo(() => {
    if (!effectiveSort?.field) return rows
    const factor = effectiveSort.order === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      const av = a[effectiveSort.field]
      const bv = b[effectiveSort.field]
      // 空值 (null/undefined/'') 恒排最后, 与排序方向无关。
      // 此前 `String(av ?? '')` 会把空值排到升序最前 — 双榜并集表
      // (如舆情热度榜: 单边东财的股票同花顺字段为 null) 首屏全是 "—" 的根因。
      const aMissing = av == null || av === ''
      const bMissing = bv == null || bv === ''
      if (aMissing && bMissing) return 0
      if (aMissing) return 1
      if (bMissing) return -1
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * factor
      return String(av).localeCompare(String(bv)) * factor
    })
  }, [rows, effectiveSort])

  // 表格/榜单模式下的行级搜索: 按任意列关键字过滤 (选个股/搜机构/搜评级通用)。
  // 分组模式的 search 沿用原 filteredGroups, 互不影响。
  const filteredTableRows = useMemo(() => {
    if (!isTableMode || activeGroupField) return sortedRows
    const q = search.trim().toLowerCase()
    if (!q) return sortedRows
    return sortedRows.filter(row =>
      Object.values(row).some(v => v != null && String(v).toLowerCase().includes(q)),
    )
  }, [sortedRows, search, isTableMode, activeGroupField])

  const groups = useMemo<DimensionGroup[]>(() => {
    if (!activeGroupField) return []
    const map = new Map<string, Record<string, any>[]>()
    for (const row of sortedRows) {
      const values = splitDimensionValues(row[activeGroupField])
      for (const value of values) {
        const item = map.get(value) ?? []
        item.push(row)
        map.set(value, item)
      }
    }
    return [...map.entries()]
      .map(([key, itemRows]) => ({
        key,
        count: itemRows.length,
        rows: itemRows,
        metrics: Object.fromEntries(groupColumns.map(col => [col.field, aggregate(itemRows, col)])),
      }))
      .sort((a, b) => b.count - a.count)
  }, [sortedRows, activeGroupField, groupColumns])

  const filteredGroups = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return groups
    return groups.filter(g => g.key.toLowerCase().includes(q))
  }, [groups, search])

  const currentGroup = filteredGroups.find(g => g.key === selectedGroup) ?? filteredGroups[0]
  const tableRows = activeGroupField ? (currentGroup?.rows ?? []) : filteredTableRows

  // 列头点击排序: 同列切换方向; 新列数值默认降序、文本默认升序。
  const toggleSort = (field: string) => {
    setUserSort(prev => {
      if (prev?.field === field) return { field, order: prev.order === 'asc' ? 'desc' : 'asc' }
      const col = displayColumns.find(c => c.field === field)
      return { field, order: col?.type === 'number' || col?.sortable ? 'desc' : 'asc' }
    })
  }
  const coveredSymbols = useMemo(() => {
    const set = new Set<string>()
    rows.forEach(r => { if (r.symbol) set.add(String(r.symbol)) })
    return set.size
  }, [rows])
  const missingConfiguredColumns = displayColumns.filter(c => rows.length > 0 && !Object.prototype.hasOwnProperty.call(rows[0], c.field))

  return (
    <>
      <PageHeader
        title={activeTitle}
        subtitle={menu ? `${menu.template} · ${activeConfigId}` : subtitle}
        right={
          // 数据源不再由右上角切换 (侧栏每个信息面都是独立入口);
          // 维度字段仅分组模式有效, table/ranking 模式不渲染。
          !isTableMode ? (
            <select
              value={activeDimensionField}
              onChange={(e) => { setDimensionField(e.target.value); setSelectedGroup(null) }}
              disabled={!activeConfig}
              className="h-8 min-w-36 rounded-btn border border-border bg-surface px-2 text-xs text-foreground disabled:opacity-50 focus:outline-none focus:border-accent/50"
            >
              {dimensionOptions.length === 0 ? (
                <option value="">暂无字段</option>
              ) : dimensionOptions.map(field => (
                <option key={field.name} value={field.name}>{field.label || field.name}</option>
              ))}
            </select>
          ) : null
        }
      />

      <div className="px-8 py-6 space-y-6 max-w-7xl">
        <section className={`relative overflow-hidden rounded-2xl border border-border bg-surface p-6 ${accentClass}`}>
          <div className="relative z-10 max-w-3xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-[11px] text-secondary">
              <Layers3 className="h-3.5 w-3.5" />
              扩展数据驱动 · 菜单可配置 · 列动态渲染
            </div>
            <h2 className="mt-4 text-2xl font-semibold tracking-tight text-foreground">{activeTitle}</h2>
            <p className="mt-2 text-sm leading-6 text-secondary">
              页面按分析菜单配置读取扩展数据源，使用分组字段生成榜单，并严格按照 detail_columns 渲染明细列。
            </p>
          </div>
          <div className="absolute right-8 top-6 hidden h-28 w-28 rounded-full border border-foreground/10 bg-foreground/[0.03] lg:flex items-center justify-center">
            <BarChart3 className="h-12 w-12 text-foreground/20" />
          </div>
        </section>

        {configs.isLoading || menuQuery.isLoading ? (
          <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted">加载配置中…</div>
        ) : menuQuery.isError ? (
          <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted">分析菜单不存在或已删除。</div>
        ) : !activeConfig ? (
          <div className="rounded-card border border-border bg-surface px-5 py-10 text-center">
            <Database className="mx-auto h-8 w-8 text-muted" />
            <div className="mt-3 text-sm text-secondary">{emptyHint}</div>
            <div className="mt-1 text-xs text-muted">请先在“数据”页面新增扩展数据，并在“扩展分析”中创建分析菜单。</div>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <StatCard label="数据源" value={activeConfig.label} hint={`${activeConfig.mode === 'snapshot' ? '快照' : '时序'} · ${activeConfig.id}`} icon={Database} />
              <StatCard
                label="分组数量"
                value={activeGroupField ? groups.length : '—'}
                hint={activeGroupField ? `按 ${fieldLabel(fields, activeGroupField)} 聚合` : '未分类 — 在明细列表右上选「分类」字段'}
                icon={Tags}
              />
              <StatCard label="覆盖标的" value={coveredSymbols || rows.length} hint={rowsQuery.data?.date ? `数据日期 ${rowsQuery.data.date}` : '当前快照'} icon={Users} />
              <StatCard label="列表列数" value={displayColumns.length} hint={`配置列 ${displayColumns.length} 个`} icon={TrendingUp} />
            </div>

            {missingConfiguredColumns.length > 0 && (
              <div className="rounded-card border border-warning/40 bg-warning/5 px-4 py-3 text-xs text-warning">
                菜单中有字段在当前数据中不存在：{missingConfiguredColumns.map(c => c.field).join('、')}
              </div>
            )}

            <div className={activeGroupField ? 'grid grid-cols-1 xl:grid-cols-[24rem_1fr] gap-5' : 'grid grid-cols-1'}>
              {activeGroupField && (
                <section className="rounded-card border border-border bg-surface overflow-hidden">
                  <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-medium text-foreground">分组榜单</h3>
                      <p className="mt-0.5 text-[11px] text-muted">按覆盖标的数量排序</p>
                    </div>
                    <span className="text-[10px] text-muted">Top {Math.min(filteredGroups.length, 100)}</span>
                  </div>
                  <div className="p-3 border-b border-border/60">
                    <div className="relative">
                      <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                      <input
                        value={search}
                        onChange={(e) => { setSearch(e.target.value); setSelectedGroup(null) }}
                        placeholder={`搜索${activeKindLabel}`}
                        className="h-8 w-full rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground placeholder:text-muted/50 focus:outline-none focus:border-accent/50"
                      />
                    </div>
                  </div>
                  <div className="max-h-[560px] overflow-auto p-2 space-y-1">
                    {filteredGroups.length === 0 ? (
                      <div className="px-3 py-8 text-center text-xs text-muted">没有可展示的分组</div>
                    ) : filteredGroups.slice(0, 100).map((group, i) => {
                      const active = currentGroup?.key === group.key
                      return (
                        <button
                          key={group.key}
                          onClick={() => setSelectedGroup(group.key)}
                          className={`w-full rounded-lg px-3 py-2 text-left transition-colors ${active ? 'bg-accent/10 border border-accent/25' : 'border border-transparent hover:bg-elevated/60'}`}
                        >
                          <div className="flex items-center gap-2">
                            <span className="w-5 text-[10px] font-mono text-muted">#{i + 1}</span>
                            <span className="flex-1 truncate text-xs font-medium text-foreground">{group.key}</span>
                            <span className="font-mono text-xs text-secondary">{group.count}</span>
                          </div>
                          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-elevated">
                            <div
                              className="h-full rounded-full bg-accent/70"
                              style={{ width: `${Math.max(6, (group.count / (filteredGroups[0]?.count || 1)) * 100)}%` }}
                            />
                          </div>
                          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted">
                            {groupColumns.filter(c => !['__dimension', '__count'].includes(c.field)).slice(0, 3).map(col => (
                              <span key={col.field}>{col.label || col.field}: {formatValue(group.metrics[col.field], col)}</span>
                            ))}
                          </div>
                        </button>
                      )
                    })}
                  </div>
                </section>
              )}

              <section className="rounded-card border border-border bg-surface overflow-hidden">
                <div className="px-5 py-4 border-b border-border flex items-center justify-between gap-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-[16px] leading-6 font-semibold text-foreground">{activeGroupField ? currentGroup?.key ?? `选择${activeKindLabel}` : '明细列表'}</h3>
                      <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] text-accent">{tableRows.length} 条</span>
                    </div>
                    <p className="mt-1 text-xs text-muted">列来自菜单 detail_columns：{displayColumns.map(f => f.label || f.field).join(' / ') || '暂无字段'}</p>
                  </div>
                  <div className="hidden sm:flex items-center gap-2 text-[11px] text-muted">
                    {isTableMode && (
                      <>
                        <select
                          value={groupBy}
                          onChange={(e) => { setGroupBy(e.target.value); setSelectedGroup(null); setSearch('') }}
                          className="h-8 min-w-24 rounded-btn border border-border bg-base px-2 text-xs text-foreground focus:outline-none focus:border-accent/50"
                          title="按该字段的值分组，左侧生成分组榜单"
                        >
                          <option value="">分类：无</option>
                          {dimensionOptions.map(field => (
                            <option key={field.name} value={field.name}>分类：{field.label || field.name}</option>
                          ))}
                        </select>
                        {!activeGroupField && (
                          <div className="relative mr-2">
                            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                            <input
                              value={search}
                              onChange={(e) => setSearch(e.target.value)}
                              placeholder="搜个股 / 代码 / 任意关键字"
                              className="h-8 w-56 rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground placeholder:text-muted/50 focus:outline-none focus:border-accent/50"
                            />
                          </div>
                        )}
                      </>
                    )}
                    <CalendarDays className="h-3.5 w-3.5" />
                    {rowsQuery.data?.date ?? '当前快照'}
                  </div>
                </div>

                <div className="overflow-auto">
                  <table className="min-w-full text-left text-xs">
                    <thead className="bg-elevated/50 text-[11px] text-muted">
                      <tr>
                        {displayColumns.map(col => (
                          <th key={col.field} className="whitespace-nowrap px-4 py-2 font-medium" style={col.width ? { width: col.width } : undefined}>
                            <button
                              type="button"
                              onClick={() => toggleSort(col.field)}
                              title="点击排序 (再点切换方向)"
                              className="inline-flex items-center gap-1 hover:text-foreground transition-colors"
                            >
                              <span>{col.label || col.field}</span>
                              {effectiveSort?.field === col.field && (
                                <span className="text-[9px] text-accent">{effectiveSort.order === 'asc' ? '▲' : '▼'}</span>
                              )}
                            </button>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/70">
                      {rowsQuery.isLoading ? (
                        <tr><td className="px-4 py-8 text-center text-muted" colSpan={Math.max(displayColumns.length, 1)}>加载数据中…</td></tr>
                      ) : tableRows.length === 0 ? (
                        <tr><td className="px-4 py-8 text-center text-muted" colSpan={Math.max(displayColumns.length, 1)}>暂无明细数据</td></tr>
                      ) : tableRows.slice(0, 300).map((row, i) => (
                        <tr key={`${row.symbol ?? row.code ?? i}-${i}`} className="hover:bg-elevated/30">
                          {displayColumns.map(col => (
                            <td key={col.field} className={`whitespace-nowrap px-4 py-2 ${col.field === (activeGroupField || activeDimensionField) ? 'max-w-[18rem] truncate text-secondary' : col.field === 'symbol' || col.field === 'code' || col.field === '股票代码' ? 'font-mono text-secondary' : 'text-foreground'}`}>
                              {formatValue(row[col.field], col)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {tableRows.length > 300 && (
                  <div className="border-t border-border px-4 py-2 text-center text-[11px] text-muted">仅展示前 300 条明细，共 {tableRows.length} 条</div>
                )}
              </section>
            </div>
          </>
        )}
      </div>
    </>
  )
}
