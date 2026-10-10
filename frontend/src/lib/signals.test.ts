// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { fieldRefName, isFieldRef, rightConstantText } from './signals'

/**
 * 回归: 信号库页面曾因 `c.right.startsWith is not a function` 整页崩溃 ——
 * 15 个「海平盘中信号」里有 13 个的 right 是**数字**（0.03 / 2 / -0.03 …），
 * 而前端按字符串处理。以下用例锁住「数字右值不得走字符串分支」这条不变量。
 */
describe('信号条件右值归一化', () => {
  it('数字常量不得被当作字段引用（原崩溃点）', () => {
    for (const v of [0.03, 2, -0.03, 0, 1.2, 0.098]) {
      expect(isFieldRef(v)).toBe(false)
      expect(fieldRefName(v)).toBe('')
      expect(rightConstantText(v)).toBe(String(v))
    }
  })

  it('field:xxx 正确解析为字段名', () => {
    expect(isFieldRef('field:vwap')).toBe(true)
    expect(fieldRefName('field:vwap')).toBe('vwap')
    expect(rightConstantText('field:vwap')).toBe('field:vwap')
  })

  it('数字字符串（编辑框回填产出）按常量处理', () => {
    expect(isFieldRef('0.03')).toBe(false)
    expect(rightConstantText('0.03')).toBe('0.03')
  })

  it('null / undefined / 空串安全', () => {
    expect(isFieldRef(null)).toBe(false)
    expect(isFieldRef(undefined)).toBe(false)
    expect(rightConstantText(null)).toBe('')
    expect(rightConstantText(undefined)).toBe('')
    expect(rightConstantText('')).toBe('')
  })

  it('模拟 Signals.tsx 的条件渲染循环：混合右值不抛异常', () => {
    const conds = [
      { left: 'pct_from_open', op: '>=', right: 0.03 },
      { left: 'vol_ratio_3m_today', op: '>=', right: 2 },
      { left: 'price', op: 'cross_up', right: 'field:vwap' },
      { left: 'hp_risk', op: '<=', right: -0.03 },
    ]
    const rendered = conds.map(c => (isFieldRef(c.right) ? fieldRefName(c.right) : rightConstantText(c.right)))
    expect(rendered).toEqual(['0.03', '2', 'vwap', '-0.03'])
  })
})
