#!/usr/bin/env node
/**
 * stock-sdk 桥接脚本。Python 后端通过 subprocess 调用它，用真实 stock-sdk 抓数据。
 *
 * Original implementation by @forrany (PR #57), migrated to plugin architecture.
 *
 * 协议:
 *   - stdin: 单行 JSON  { op, symbols?, adjust?, period?, start?, end?, concurrency? }
 *   - stdout: 单行 JSON
 *       daily/adj/minute: { ok:true, op, rows: { [appSymbol]: Row[] } }
 *       realtime/instruments: { ok:true, op, rows: Row[] }
 *       ping: { ok:true, op:'ping', version }
 *       失败:  { ok:false, error }
 *
 * op:
 *   daily        —— 日K(adjust 默认 none), 每个 symbol 一组 bars
 *   adj          —— 除权因子: 取 hfq 与 none 收盘价, ex_factor = close_hfq / close_none
 *   minute       —— 分钟K(period 默认 5)
 *   realtime     —— 全 A 股实时快照(batch.cn)
 *   instruments  —— 全 A 股标的维表(batch.cn 提取元数据)
 *   ping         —— 探活
 *
 * 说明: daily/adj/minute 的入参 symbols 是「app 符号」(如 600519.SH)。stock-sdk 能容错解析，
 * 返回结果里我们**回显原始 app 符号**作为 key，避免 code→符号 的歧义(指数/股票同码等)。
 * realtime/instruments 是全市场枚举，由 code + marketId 反推后缀。
 */
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execSync } from 'node:child_process'
import path from 'node:path'

/**
 * 解析 stock-sdk 入口。ESM 的 bare import 只查本地 node_modules 链，不查全局，
 * 因此这里显式在 [本地(脚本旁), 全局 npm root, NODE_PATH] 中查找后动态 import。
 * 部署时优先用脚本旁 vendored 的 node_modules/stock-sdk。
 */
async function loadSDK() {
  const require = createRequire(import.meta.url)
  const scriptDir = path.dirname(fileURLToPath(import.meta.url))
  // 候选 node_modules 目录（按优先级）
  const nmDirs = [path.join(scriptDir, 'node_modules')]
  for (const p of (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean)) nmDirs.push(p)
  try {
    const groot = execSync('npm root -g', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    if (groot) nmDirs.push(groot)
  } catch {
    /* npm 不可用则忽略 */
  }
  let entry
  for (const nm of nmDirs) {
    try {
      entry = require.resolve(path.join(nm, 'stock-sdk'))
      break
    } catch {
      /* 试下一个 */
    }
  }
  if (!entry) {
    throw new Error(
      `无法解析 stock-sdk（已搜索: ${nmDirs.join(', ')}）。请在桥接目录 npm install，或全局 npm i -g stock-sdk。`
    )
  }
  const mod = await import(pathToFileURL(entry).href)
  return mod.StockSDK || (mod.default && mod.default.StockSDK)
}

const MARKET_ID_TO_SUFFIX = { '1': 'SH', '51': 'SZ', '62': 'BJ' }

// 下游(Python)可能提前关闭管道，忽略 EPIPE 避免噪声崩溃。
process.stdout.on('error', (e) => {
  if (e && e.code === 'EPIPE') process.exit(0)
})

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (c) => (buf += c))
    process.stdin.on('end', () => resolve(buf))
    process.stdin.on('error', reject)
  })
}

/** 简单并发池: 对 items 逐个跑 worker，最多 concurrency 个在飞。 */
async function mapPool(items, concurrency, worker) {
  const results = new Array(items.length)
  let next = 0
  const runners = new Array(Math.min(concurrency, items.length)).fill(0).map(async () => {
    while (true) {
      const i = next++
      if (i >= items.length) return
      try {
        results[i] = await worker(items[i], i)
      } catch (e) {
        results[i] = { __error: String((e && e.message) || e) }
      }
    }
  })
  await runners.reduce((p) => p, Promise.resolve())
  await Promise.all(runners)
  return results
}

/** code + marketId → app 符号(600519.SH)。反推失败则退化用 code 前缀猜测。 */
function toAppSymbol(code, marketId) {
  const suffix = MARKET_ID_TO_SUFFIX[String(marketId)] || guessSuffix(code)
  return suffix ? `${code}.${suffix}` : String(code)
}

function guessSuffix(code) {
  const c = String(code)
  if (/^(6|5|9)/.test(c)) return 'SH'
  if (/^(0|3|1|2)/.test(c)) return 'SZ'
  if (/^(4|8|92)/.test(c)) return 'BJ'
  return ''
}

/** app 符号(600519.SH / 000001.SZ) → 腾讯实时接口代码(sh600519 / sz000001)。 */
function toTencentCode(appSymbol) {
  const s = String(appSymbol).trim()
  const dot = s.lastIndexOf('.')
  if (dot === -1) return ''
  const code = s.slice(0, dot)
  const suffix = s.slice(dot + 1).toUpperCase()
  if (suffix === 'SH') return `sh${code}`
  if (suffix === 'SZ') return `sz${code}`
  if (suffix === 'BJ') return `bj${code}`
  return ''
}

/** stock-sdk 的 adjust 取值是 '' | 'qfq' | 'hfq'（无 'none'）。这里做兼容映射。 */
function normAdjust(v) {
  if (v === 'hfq') return 'hfq'
  if (v === 'qfq') return 'qfq'
  return '' // none / undefined / 空 → 不复权
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** 安全数值转换：null/undefined/非数字 → null。 */
function num(v) {
  if (v == null || v === '' || v === '-') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/**
 * 上游(东财)偶发冷启动/限流时对活跃标的返回空数组(非报错)。对已知应有数据的请求，
 * 空结果重试若干次以提升鲁棒性；真正无数据(退市/停牌/区间无交易)时多花几次调用可接受。
 */
async function fetchWithRetry(fn, { retries = 2, delayMs = 300 } = {}) {
  let last = []
  for (let i = 0; i <= retries; i++) {
    const r = await fn()
    if (Array.isArray(r) && r.length > 0) return r
    last = Array.isArray(r) ? r : []
    if (i < retries) await sleep(delayMs)
  }
  return last
}

async function fetchDaily(sdk, sym, { adjust, period = 'daily', start, end }) {
  const opts = { period, adjust: normAdjust(adjust) }
  if (start) opts.startDate = start
  if (end) opts.endDate = end
  return fetchWithRetry(() => sdk.kline.cn(sym, opts))
}

async function opDaily(sdk, job) {
  const { symbols = [], adjust = 'none', period = 'daily', start, end, concurrency = 6 } = job
  const out = {}
  const rows = await mapPool(symbols, concurrency, (sym) =>
    fetchDaily(sdk, sym, { adjust, period, start, end })
  )
  symbols.forEach((sym, i) => {
    const r = rows[i]
    out[sym] = Array.isArray(r) ? r : []
  })
  return out
}

async function opAdj(sdk, job) {
  const { symbols = [], start, end, concurrency = 6 } = job
  const out = {}
  await mapPool(symbols, concurrency, async (sym) => {
    const [none, hfq] = await Promise.all([
      fetchDaily(sdk, sym, { adjust: 'none', start, end }),
      fetchDaily(sdk, sym, { adjust: 'hfq', start, end }),
    ])
    const noneByDate = new Map()
    for (const b of none) if (b && b.close) noneByDate.set(b.date, b.close)
    const factors = []
    for (const b of hfq) {
      if (!b || !b.date) continue
      const rawClose = noneByDate.get(b.date)
      if (!rawClose || !b.close) continue
      factors.push({ symbol: sym, trade_date: b.date, ex_factor: b.close / rawClose })
    }
    out[sym] = factors
    return factors
  })
  return out
}

async function opMinute(sdk, job) {
  const { symbols = [], period = 5, start, end, concurrency = 6 } = job
  const out = {}
  await mapPool(symbols, concurrency, async (sym) => {
    const opts = { period: String(period) }
    if (start) opts.startDate = start
    if (end) opts.endDate = end
    const bars = await fetchWithRetry(() => sdk.kline.cnMinute(sym, opts))
    out[sym] = Array.isArray(bars) ? bars : []
    return out[sym]
  })
  return out
}

/**
 * 全 A 股实时快照（新浪分页）。
 * 上游 stock-sdk 的 batch.cn 依赖 assets.linkdiary.cn 的 A 股代码列表，
 * 部分网络环境无法访问该域名（超时），导致整个实时行情拉取失败。
 * 东财 clist 对脚本请求有反爬(偶发 502/HTML)。这里改用新浪
 * vip.stock.finance.sina.com.cn（稳定、不反爬、Node 可直连）分页拉全市场，
 * 绕过 linkdiary，保证实时功能可用。
 */
const SINA_PAGE_NUM = 80

async function fetchSinaAllAShare() {
  const rows = []
  let page = 1
  const h = {
    'User-Agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    Referer: 'https://finance.sina.com.cn/',
  }
  for (; page <= 120; page++) {
    const url =
      'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/' +
      'Market_Center.getHQNodeData' +
      `?page=${page}&num=${SINA_PAGE_NUM}&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page`
    const resp = await fetch(url, { headers: h })
    if (!resp.ok) {
      if (resp.status === 502 || resp.status >= 500) break // 新浪偶发限流, 已取到的返回
      throw new Error(`sina getHQNodeData HTTP ${resp.status}`)
    }
    const text = await resp.text()
    let json = null
    try {
      json = JSON.parse(text)
    } catch {
      break // 非 JSON 说明页尾/异常, 截断返回已取到的
    }
    if (!Array.isArray(json) || json.length === 0) break
    rows.push(...json)
    if (json.length < SINA_PAGE_NUM) break
    await sleep(80)
  }
  return rows
}

function guessSuffixFromCode(code) {
  const c = String(code)
  // 东财 marketId: 1=SH, 0=SZ, 0(北交所用别的) —— 用代码规则兜底
  if (/^(6|5|9)/.test(c)) return 'SH'
  if (/^(0|3|1|2)/.test(c)) return 'SZ'
  if (/^(4|8|92)/.test(c)) return 'BJ'
  return ''
}

/** 新浪 symbol(sh600519) → app 符号(600519.SH)。 */
function sinaSymbolToApp(sinaSym) {
  const s = String(sinaSym || '').trim().toLowerCase()
  if (/^sh/.test(s)) return `${s.slice(2)}.SH`
  if (/^sz/.test(s)) return `${s.slice(2)}.SZ`
  if (/^bj/.test(s)) return `${s.slice(2)}.BJ`
  return s
}

async function fetchEastmoneyClistAll() {
  const rows = []
  let pn = 1
  let total = Infinity
  // 与 stock-sdk 相同的 token，东财接口稳定返回
  const ut = 'bd1d9ddb04089700cf9c27f6f7426281'
  for (; rows.length < total && pn <= 60; pn++) {
    const url =
      `https://push2.eastmoney.com/api/qt/clist/get?pn=${pn}&pz=${EM_CLIST_PAGE}` +
      `&po=1&np=1&fltt=2&invt=2&fid=f3&fs=${encodeURIComponent(EM_CLIST_FS)}` +
      `&fields=${encodeURIComponent(EM_CLIST_FIELDS)}&ut=${ut}`
    const resp = await fetch(url, {
      headers: {
        'User-Agent':
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        Referer: 'https://quote.eastmoney.com/',
        Accept: 'application/json, text/plain, */*',
      },
    })
    if (!resp.ok) throw new Error(`eastmoney clist HTTP ${resp.status}`)
    const json = await resp.json()
    const data = json && json.data
    if (!data || !Array.isArray(data.diff)) break
    total = data.total != null ? Number(data.total) : total
    rows.push(...data.diff)
    if (!data.diff.length) break
    // 限速：东财 clist 分页过快可能触发风控
    await sleep(120)
  }
  return rows
}

async function opRealtime(sdk, job) {
  // 若显式指定 symbols，则走腾讯单股报价（qt.gtimg.cn，同样可达且不依赖 linkdiary）。
  const requested = Array.isArray(job.symbols) ? job.symbols.filter(Boolean) : []
  if (requested.length) {
    // app 符号(600519.SH / 000001.SZ) → 腾讯格式(sh600519 / sz000001)
    const tencentCodes = requested.map(toTencentCode).filter(Boolean)
    const quotes = tencentCodes.length ? await sdk.quotes.cn(tencentCodes) : []
    const rows = []
    for (const q of quotes || []) {
      if (!q || !q.code) continue
      rows.push({
        symbol: toAppSymbol(q.code, q.marketId),
        name: q.name,
        last_price: q.price,
        prev_close: q.prevClose,
        open: q.open,
        high: q.high,
        low: q.low,
        volume: q.volume,
        amount: q.amount,
        change_pct: q.changePercent,
        timestamp: q.timestamp,
      })
    }
    return rows
  }

  // 默认：新浪全市场快照（绕过 linkdiary，新浪稳定不反爬）
  let raw = []
  try {
    raw = await fetchSinaAllAShare()
  } catch (e) {
    // 新浪失败时回退到原 stock-sdk 路径（可能需要 linkdiary）
    process.stderr.write(`[bridge] sina all-a failed: ${(e && e.message) || e}\n`)
    const all = await sdk.batch.cn({ concurrency: job.concurrency || 8 })
    raw = (all || []).map((q) => ({
      symbol: toAppSymbol(q.code, q.marketId),
      name: q.name,
      last_price: q.price,
      prev_close: q.prevClose,
      open: q.open,
      high: q.high,
      low: q.low,
      volume: q.volume,
      amount: q.amount,
      change_pct: q.changePercent,
      timestamp: q.timestamp,
    }))
    const rows = []
    for (const it of raw || []) {
      if (!it || !it.symbol) continue
      rows.push(it)
    }
    return rows
  }
  const rows = []
  for (const it of raw || []) {
    // 新浪 symbol(sh600519) → app 符号
    const symbol = sinaSymbolToApp(it.symbol)
    if (!symbol) continue
    const code = symbol.split('.')[0]
    if (!code) continue
    rows.push({
      symbol,
      name: it.name != null ? String(it.name) : '',
      last_price: num(it.trade),
      prev_close: num(it.settlement),
      open: num(it.open),
      high: num(it.high),
      low: num(it.low),
      volume: num(it.volume),
      amount: num(it.amount),
      change_pct: it.changepercent != null ? Number(it.changepercent) : null,
      timestamp: it.trade != null ? Date.now() : null,
    })
  }
  return rows
}

async function opInstruments(sdk, job) {
  const { concurrency = 8 } = job
  const all = await sdk.batch.cn({ concurrency })
  const rows = []
  for (const q of all || []) {
    if (!q || !q.code) continue
    const suffix = MARKET_ID_TO_SUFFIX[String(q.marketId)] || guessSuffix(q.code)
    // 形状对齐 tickflow 的 Instrument(数值扩展字段放 ext),以复用 instrument_sync 的 flatten。
    rows.push({
      symbol: toAppSymbol(q.code, q.marketId),
      name: q.name,
      code: String(q.code),
      exchange: suffix,
      region: 'CN',
      type: 'stock',
      ext: {
        total_shares: q.totalShares ?? null,
        float_shares: q.circulatingShares ?? null,
        limit_up: q.limitUp ?? null,
        limit_down: q.limitDown ?? null,
      },
    })
  }
  return rows
}

async function main() {
  let job
  try {
    const raw = (await readStdin()).trim()
    job = raw ? JSON.parse(raw) : {}
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: `invalid job json: ${e.message}` }))
    return
  }
  const op = job.op || 'ping'
  try {
    const StockSDK = await loadSDK()
    if (!StockSDK) throw new Error('stock-sdk 已解析但未导出 StockSDK')
    if (op === 'ping') {
      process.stdout.write(JSON.stringify({ ok: true, op: 'ping', version: StockSDK.version || 'ok' }))
      return
    }
    const sdk = new StockSDK({ retry: { maxRetries: 3, baseDelay: 400 } })
    let rows
    switch (op) {
      case 'daily':
        rows = await opDaily(sdk, job)
        break
      case 'adj':
        rows = await opAdj(sdk, job)
        break
      case 'minute':
        rows = await opMinute(sdk, job)
        break
      case 'realtime':
        rows = await opRealtime(sdk, job)
        break
      case 'instruments':
        rows = await opInstruments(sdk, job)
        break
      default:
        process.stdout.write(JSON.stringify({ ok: false, error: `unknown op: ${op}` }))
        return
    }
    process.stdout.write(JSON.stringify({ ok: true, op, rows }))
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, op, error: String((e && e.stack) || e) }))
  }
}

main()
