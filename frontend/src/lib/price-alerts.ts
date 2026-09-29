import type { MonitorRule } from '@/lib/api'

export type PriceAlertDirection = 'up' | 'down'

export interface PointPriceAlert {
  rule: MonitorRule
  direction: PriceAlertDirection
  target: number
}

export interface MonitorPriceLine {
  value: number
  label: string
  color: string
}

export function parsePointPriceAlert(rule: MonitorRule, symbol?: string): PointPriceAlert | null {
  if (
    rule.type !== 'price'
    || rule.scope !== 'symbols'
    || rule.symbols.length !== 1
    || (symbol != null && rule.symbols[0] !== symbol)
    || rule.conditions.length !== 1
  ) return null

  const condition = rule.conditions[0]
  if (condition.field !== 'close' || !['>=', '<='].includes(condition.op)) return null
  if (typeof condition.value !== 'number' || !Number.isFinite(condition.value) || condition.value <= 0) return null

  return {
    rule,
    direction: condition.op === '>=' ? 'up' : 'down',
    target: condition.value,
  }
}

export function inferPriceAlertDirection(
  target: number,
  currentPrice: number | null | undefined,
): PriceAlertDirection {
  return currentPrice != null && Number.isFinite(currentPrice) && target < currentPrice ? 'down' : 'up'
}

export function buildPriceAlertMessage(
  name: string,
  symbol: string,
  direction: PriceAlertDirection,
  target: number,
): string {
  return `${name || symbol}股价${direction === 'up' ? '上穿' : '下穿'} ${target.toFixed(2)}`
}

export function buildMonitorPriceLines(rules: MonitorRule[], symbol: string): MonitorPriceLine[] {
  return rules.flatMap(rule => {
    if (!rule.enabled) return []
    const alert = parsePointPriceAlert(rule, symbol)
    if (!alert) return []
    const isUp = alert.direction === 'up'
    return [{
      value: alert.target,
      label: `${isUp ? '上穿' : '下穿'} ${alert.target.toFixed(2)}`,
      color: isUp ? '#C74040' : '#2D9B65',
    }]
  })
}

/**
 * 完整版监控点位提取: 兼容多条件规则与全部 close 算子 (>=/<=/>/<)。
 *
 * 与 parsePointPriceAlert 不同, 这里只要规则是「个股价格规则」(scope=symbols、
 * 命中该 symbol、type=price) 且含 close 价格条件, 就把 close 阈值作为点位输出 —
 * 覆盖「放量站稳 X + 量比」这类多条件规则, 让 K 线/分时图能画出全部监控价位。
 * 同价位多规则去重; 颜色: 突破位(>=/>)=红, 跌破位(<=/<)=绿。
 */
export function buildAllMonitorPriceLines(rules: MonitorRule[], symbol: string): MonitorPriceLine[] {
  const seen = new Set<string>()
  const out: MonitorPriceLine[] = []
  for (const rule of rules) {
    if (!rule.enabled) continue
    if (rule.type !== 'price' || rule.scope !== 'symbols') continue
    if (!rule.symbols.includes(symbol)) continue
    const cond = (rule.conditions ?? []).find(c => c.field === 'close' && typeof c.value === 'number')
    if (!cond || !Number.isFinite(cond.value as number) || (cond.value as number) <= 0) continue
    const target = cond.value as number
    const key = target.toFixed(2)
    if (seen.has(key)) continue
    seen.add(key)
    const isUp = cond.op === '>=' || cond.op === '>'
    out.push({
      value: target,
      label: `${isUp ? '上穿' : '下穿'} ${target.toFixed(2)}`,
      color: isUp ? '#C74040' : '#2D9B65',
    })
  }
  return out
}
