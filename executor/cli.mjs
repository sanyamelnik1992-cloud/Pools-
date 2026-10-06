#!/usr/bin/env node
// Исполнитель транзакций бота на Cetus (Sui). Каждая команда печатает одну строку JSON в stdout.
//
//   node cli.mjs status --pool <id> [--position <id>]              кошелёк, цена пула, позиции кошелька в пуле
//                                                                 (и есть ли ещё у кошелька позиция <id>)
//   node cli.mjs open   --pool <id> --tick-lower N --tick-upper N --amount-a X --amount-b Y [--band 0.0015]
//                                                                 открыть позицию; X и Y — потолки в минимальных
//                                                                 единицах: больше них не возьмётся, даже если цена
//                                                                 сдвинется в пределах ±band до исполнения
//   node cli.mjs close  --pool <id> --position <id> [--band 0.0015]
//                                                                 снять всю ликвидность, комиссии и награды, закрыть
//   node cli.mjs swap   --from <тип монеты> --to <тип> --amount X [--slippage 0.005]
//                                                                 обмен через агрегатор Cetus (лучший маршрут по Sui)
//
// Ключ — переменная окружения SUI_PRIVATE_KEY (формат suiprivkey1…, экспорт из кошелька). Без ключа или с флагом
// --simulate транзакция только симулируется в сети от имени --address / SUI_ADDRESS: ничего не подписывается
// и не отправляется. Своё RPC можно задать через SUI_RPC.
import { CetusClmmSDK } from '@cetusprotocol/sui-clmm-sdk'
import { ClmmPoolUtil, TickMath } from '@cetusprotocol/common-sdk'
import { AggregatorClient, Env } from '@cetusprotocol/aggregator-sdk'
import { Transaction } from '@mysten/sui/transactions'
import { decodeSuiPrivateKey } from '@mysten/sui/cryptography'
import { Ed25519Keypair } from '@mysten/sui/keypairs/ed25519'
import { Secp256k1Keypair } from '@mysten/sui/keypairs/secp256k1'
import BN from 'bn.js'

const SUI = '0x2::sui::SUI'

function args(argv) {
  const out = { _: [] }
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const k = argv[i].slice(2)
      const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true
      out[k] = v
    } else out._.push(argv[i])
  }
  return out
}

function keypair() {
  const key = process.env.SUI_PRIVATE_KEY
  if (!key) return null
  const { scheme, secretKey } = decodeSuiPrivateKey(key)
  if (scheme === 'ED25519') return Ed25519Keypair.fromSecretKey(secretKey)
  if (scheme === 'Secp256k1') return Secp256k1Keypair.fromSecretKey(secretKey)
  throw new Error(`неподдерживаемый тип ключа ${scheme}`)
}

const json = (o) => JSON.stringify(o, (k, v) => (typeof v === 'bigint' ? v.toString() : BN.isBN(v) ? v.toString() : v))
const fail = (msg) => {
  console.log(json({ ok: false, error: String(msg) }))
  process.exit(1)
}

const a = args(process.argv.slice(2))
const cmd = a._[0]
const kp = keypair()
const simulate = Boolean(a.simulate) || !kp
const address = kp ? kp.getPublicKey().toSuiAddress() : a.address || process.env.SUI_ADDRESS
if (!address) fail('нужен SUI_PRIVATE_KEY или адрес для симуляции (--address / SUI_ADDRESS)')
const slippage = Number(a.slippage ?? 0.005)
const band = Number(a.band ?? 0.0015)      // насколько цена может сдвинуться между расчётом и исполнением
const same = (x, y) => String(x).toLowerCase() === String(y).toLowerCase()

const sdk = CetusClmmSDK.createSDK(process.env.SUI_RPC ? { env: 'mainnet', full_rpc_url: process.env.SUI_RPC } : { env: 'mainnet' })
sdk.setSenderAddress(address)

async function run(tx) {
  // симуляция или подпись и отправка; возвращает статус, изменения балансов этого кошелька и созданные позиции
  const include = { effects: true, balanceChanges: true, objectTypes: true }
  tx.setSender(address)
  let r
  if (simulate) r = await sdk.FullClient.simulateTransaction({ transaction: tx, include })
  else {
    r = await sdk.FullClient.signAndExecuteTransaction({ transaction: tx, signer: kp, include })
    const d = (r?.Transaction ?? r?.FailedTransaction)?.digest
    if (d) await sdk.FullClient.waitForTransaction({ digest: d }).catch(() => null)
  }
  const t = r?.Transaction ?? r?.FailedTransaction ?? {}
  const types = t.objectTypes ?? {}
  const created = (t.effects?.changedObjects ?? [])
    .filter((c) => c.idOperation === 'Created' && /::position::Position$/.test(types[c.objectId] ?? ''))
    .map((c) => c.objectId)
  return { simulated: simulate, ok: r?.$kind === 'Transaction' && t.effects?.status?.success !== false,
           digest: t.digest, status: t.effects?.status, gas: t.effects?.gasUsed,
           balance_changes: (t.balanceChanges ?? []).filter((c) => !c.address || same(c.address, address)),
           created_positions: created }
}

const norm = (t) => t.replace(/^0x0+(?=[0-9a-f]{1,}::)/i, '0x')   // 0x000…02::sui::SUI → 0x2::sui::SUI

async function balances(types) {
  // балансы только нужных монет (в кошельке бывает много мусорных токенов)
  const list = await sdk.FullClient.getOwnerCoinBalances(address)
  const want = new Set(types.map(norm))
  return Object.fromEntries(list.filter((b) => want.has(norm(b.coinType))).map((b) => [norm(b.coinType), b.balance ?? b.totalBalance]))
}

async function positions(pool_id) {
  const ps = await sdk.Position.getPositionList(address, [pool_id], false)
  return ps.map((p) => ({ id: p.pos_object_id, liquidity: p.liquidity, tick_lower: p.tick_lower_index, tick_upper: p.tick_upper_index }))
}

async function owned(id) {
  // есть ли объект позиции и принадлежит ли он этому кошельку (без индекса — напрямую по объекту)
  try {
    const o = await sdk.FullClient.getObject({ objectId: id })
    return same(o?.object?.owner?.AddressOwner ?? '', address)
  } catch (e) {
    if (/not ?found|does not exist|deleted/i.test(String(e?.message ?? e))) return false
    throw e
  }
}

async function status() {
  const pool = await sdk.Pool.getPool(a.pool, true)
  const out = {
    ok: true, address, simulate,
    pool: { id: pool.id, coin_a: pool.coin_type_a, coin_b: pool.coin_type_b, sqrt_price: pool.current_sqrt_price,
            tick: pool.current_tick_index, tick_spacing: Number(pool.tick_spacing),
            rewarders: pool.rewarder_infos.map((r) => r.coin_type) },
    balances: await balances([pool.coin_type_a, pool.coin_type_b, SUI, ...pool.rewarder_infos.map((r) => r.coin_type)]),
    positions: await positions(pool.id),
  }
  if (a.position) {
    out.position_owned = await owned(a.position)
    if (out.position_owned) {
      const p = await sdk.Position.getPositionById(a.position)
      out.position = { id: a.position, liquidity: p.liquidity, tick_lower: p.tick_lower_index, tick_upper: p.tick_upper_index }
    }
  }
  return out
}

function scaled(sqrt, k) {
  // sqrt-цена × √k — граница коридора цены
  return new BN(sqrt).mul(new BN(String(Math.round(Math.sqrt(k) * 1e12)))).div(new BN(String(1e12)))
}

async function open() {
  const pool = await sdk.Pool.getPool(a.pool, true)
  const lower = Number(a['tick-lower']), upper = Number(a['tick-upper'])
  const sp = Number(pool.tick_spacing)
  if (lower % sp || upper % sp || lower >= upper) fail(`границы должны быть кратны шагу ${sp} и нижняя меньше верхней`)
  const have_a = new BN(String(a['amount-a'])), have_b = new BN(String(a['amount-b']))
  const cur = new BN(pool.current_sqrt_price)
  const sqrts = [cur, scaled(cur, 1 - band), scaled(cur, 1 + band)]
  const need = (amount, is_a) => sqrts.map((q) => {
    // сколько второй монеты потребуется, если к исполнению цена окажется q (берём худший случай в коридоре)
    const e = ClmmPoolUtil.estLiquidityAndCoinAmountFromOneAmounts(lower, upper, amount, is_a, true, 0, q)
    return new BN(String(is_a ? e.coin_amount_b : e.coin_amount_a))
  }).reduce((m, x) => (x.gt(m) ? x : m), new BN(0))
  // фиксируем монету a (или b, если монеты a нет) и уменьшаем её, пока второй монеты хватает при любой цене
  // в коридоре; потолки — ровно монеты бота, чужие средства кошелька в позицию не попадут
  const fix_a = !have_a.isZero()
  let x = fix_a ? have_a : have_b
  const other = fix_a ? have_b : have_a
  let n = need(x, fix_a)
  if (n.gt(other)) {
    x = x.mul(other).div(n).muln(999).divn(1000)
    n = need(x, fix_a)
  }
  if (x.isZero() || n.gt(other)) fail('не хватает монет для позиции в этом диапазоне')
  const est = ClmmPoolUtil.estLiquidityAndCoinAmountFromOneAmounts(lower, upper, x, fix_a, true, 0, cur)
  const amount_a = (fix_a ? x : have_a).toString(), amount_b = (fix_a ? have_b : x).toString()
  const tx = await sdk.Position.createAddLiquidityFixTokenPayload({
    coin_type_a: pool.coin_type_a, coin_type_b: pool.coin_type_b, pool_id: pool.id,
    tick_lower: String(lower), tick_upper: String(upper), fix_amount_a: fix_a, amount_a, amount_b,
    slippage: 0, is_open: true, pos_id: '', rewarder_coin_types: [], collect_fee: false,
  })
  const res = await run(tx)
  let position = null
  if (res.created_positions.length === 1) {
    // позиция — из эффектов самой транзакции; ликвидность — с объекта (если узел ещё не успел — оценка)
    const id = res.created_positions[0]
    position = { id, liquidity: est.liquidity_amount?.toString?.() ?? null, tick_lower: lower, tick_upper: upper }
    if (!res.simulated && res.ok) {
      for (let i = 0; i < 5; i++) {
        try { position.liquidity = (await sdk.Position.getPositionById(id)).liquidity; break } catch (e) {
          await new Promise((r) => setTimeout(r, 1500))
        }
      }
    }
  }
  return { ...res, fix_amount_a: fix_a, amount_a, amount_b, position }
}

async function close() {
  const pool = await sdk.Pool.getPool(a.pool, true)
  const pos = await sdk.Position.getPositionById(a.position)
  const lo = TickMath.tickIndexToSqrtPriceX64(Number(pos.tick_lower_index))
  const hi = TickMath.tickIndexToSqrtPriceX64(Number(pos.tick_upper_index))
  const cur = new BN(pool.current_sqrt_price)
  // минимум к получению — худший случай при сдвиге цены в коридоре ±band до исполнения
  const at = [cur, scaled(cur, 1 - band), scaled(cur, 1 + band)].map((q) =>
    ClmmPoolUtil.getCoinAmountFromLiquidity(new BN(pos.liquidity), q, lo, hi, false))
  const min = (k) => at.map((x) => new BN(String(x[k]))).reduce((m, x) => (x.lt(m) ? x : m)).muln(999).divn(1000)
  const tx = await sdk.Position.closePositionPayload({
    coin_type_a: pool.coin_type_a, coin_type_b: pool.coin_type_b, pool_id: pool.id, pos_id: pos.pos_object_id,
    min_amount_a: min('coin_amount_a').toString(), min_amount_b: min('coin_amount_b').toString(),
    rewarder_coin_types: pool.rewarder_infos.map((r) => r.coin_type), collect_fee: true,
  })
  return { ...(await run(tx)), expected_a: at[0].coin_amount_a?.toString(), expected_b: at[0].coin_amount_b?.toString() }
}

async function swap() {
  const agg = new AggregatorClient({ env: Env.Mainnet, signer: address, client: sdk.FullClient })
  const route = await agg.findRouters({ from: a.from, target: a.to, amount: new BN(String(a.amount)), byAmountIn: true })
  if (!route || route.insufficientLiquidity) fail('агрегатор не нашёл маршрут')
  const tx = new Transaction()
  await agg.fastRouterSwap({ router: route, txb: tx, slippage })
  return { ...(await run(tx)), amount_in: route.amountIn.toString(), amount_out: route.amountOut.toString() }
}

try {
  const fn = { status, open, close, swap }[cmd]
  if (!fn) fail('команда: status | open | close | swap')
  if (cmd !== 'swap' && !a.pool) fail('нужен --pool')
  console.log(json(await fn()))
} catch (e) {
  fail(e?.message ?? e)
}
