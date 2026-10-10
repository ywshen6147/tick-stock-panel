/**
 * 模型选择下拉 —— 用于「逐派模型 / 分组模型」这类需要**从清单里选**的位置。
 *
 * 为什么不用 <input list="datalist">：
 *   浏览器会把 datalist 候选按输入框**当前内容**过滤。字段一旦已有值（例如
 *   `doubao-seed-2.0-lite`），点开就只显示这一条 → 看起来"无法选择其它模型"。
 *   实测用户反馈就是这个现象。
 *
 * 这里用原生 <select>：候选永远完整可见，且额外提供：
 *   - 「跟随…」空值项（回落统一值 / 全局模型）
 *   - 若当前值不在清单内（手填过的自定义模型），把它作为一项保留
 *   - 「自定义…」项 → 弹输入框填写任意模型 ID
 */
import { AVAILABLE_MODELS } from '@/lib/aiModels'
import { cn } from '@/lib/cn'

const CUSTOM = '__custom__'

export function ModelSelect({
  value,
  onChange,
  fallbackLabel,
  className,
  title,
}: {
  /** 当前值；空串 = 跟随 fallback */
  value: string
  onChange: (v: string) => void
  /** 空值项的文案，例如 "跟随统一(deepseek-v4-pro)" */
  fallbackLabel: string
  className?: string
  title?: string
}) {
  const current = (value ?? '').trim()
  const inList = current !== '' && AVAILABLE_MODELS.includes(current)

  return (
    <select
      value={current}
      title={title}
      onChange={e => {
        const v = e.target.value
        if (v === CUSTOM) {
          const typed = window.prompt('输入模型 ID（可留空表示跟随）', current)
          if (typed === null) return           // 取消
          onChange(typed.trim())
          return
        }
        onChange(v)
      }}
      className={cn('rounded-btn border border-border bg-elevated px-1.5 py-1 font-mono text-[11px]',
        'text-secondary outline-none focus:border-accent', className)}
    >
      <option value="">{fallbackLabel}</option>
      {AVAILABLE_MODELS.map(m => <option key={m} value={m}>{m}</option>)}
      {/* 手填的自定义模型：不在清单里也要保留, 否则一打开下拉就被"重置" */}
      {current !== '' && !inList && <option value={current}>{current}（自定义）</option>}
      <option value={CUSTOM}>自定义…</option>
    </select>
  )
}
