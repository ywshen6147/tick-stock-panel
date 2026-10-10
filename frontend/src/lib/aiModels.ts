/**
 * 可用的模型 ID 清单 —— 各处「模型」输入框的下拉候选。
 *
 * 全部为 2026-10-10 用真实 Key 实测通过的 ID：
 *   - 火山方舟 Agent Plan（OpenAI 兼容入口 /api/plan/v3）13 个
 *   - DeepSeek 官方（api.deepseek.com）的 deepseek-flash 别名
 *
 * 注意：火山 Agent Plan 网关**不提供** /models 列表接口，故只能按实测清单维护；
 * 输入框本身仍可手打任意模型名（不是下拉硬约束）。
 */
export const AVAILABLE_MODELS = [
  'deepseek-v4-pro',
  'deepseek-v4-flash',
  'deepseek-v4-flash-vision-exp',
  'deepseek-flash',
  'doubao-seed-evolving',
  'doubao-seed-2.1-pro',
  'doubao-seed-2.1-turbo',
  'doubao-seed-2.0-lite',
  'doubao-seed-2.0-mini',
  'glm-5.3',
  'glm-5.3-flash',
  'kimi-k3',
  'kimi-k2.7-code',
  'minimax-m3',
]

/** 四派顾问（与后端 paper_personas.ANALYSTS 的 id/名称一一对应）。 */
export const DEBATE_ANALYSTS = [
  { id: 'caishen', name: '蔡森·多空转折', short: '蔡森' },
  { id: 'chanlun', name: '缠论·走势实战', short: '缠论' },
  { id: 'canslim', name: '笑傲股市 CAN SLIM', short: 'CAN SLIM' },
  { id: 'kxian', name: 'K线战法', short: 'K线战法' },
] as const
