#!/usr/bin/env node
// Исполнитель транзакций бота на Cetus (Sui). Каждая команда печатает одну строку JSON в stdout.
//
//   node cli.mjs status --pool <id>                               кошелёк, цена пула, позиции кошелька в пуле
//   node cli.mjs open   --pool <id> --tick-lower N --tick-upper N --amount-a X --amount-b Y [--slippage 0.005]
//                                                                 открыть позицию (суммы в минимальных единицах;
//                                                                 ограничивающая монета берётся целиком)
//   node cli.mjs close  --pool <id> --position <id> [--slippage 0.005]
//                                                                 снять всю ликвидность, комиссии и награды, закрыть
//   node cli.mjs swap   --from <тип монеты> --to <тип> --amount X [--slippage 0.005]
//                                                                 обмен через агрегатор Cetus (лучший маршрут по Sui)
//
// Ключ — переменная окружения SUI_PRIVATE_KEY (формат suiprivkey1…, экспорт из кошелька). Без ключа или с флагом
// --simulate транзакция только симулируется в сети от имени --address / SUI_ADDRESS: ничего не подписывается
// и не отправляется. Своё RPC можно задать через SUI_RPC.
import { CetusClmmSDK } from '@cetusprotocol/sui-clmm-sdk'
import { ClmmPoolUtil, Percentage, TickMath, adjustForCoinSlippage } from '@cetusprotocol/common-sdk'
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

const sdk = CetusClmmSDK.createSDK(process.env.SUI_RPC ? { env: 'mainnet', full_rpc_url: process.env.SUI_RPC } : { env: 'mainnet' })
sdk.setSenderAddress(address)

async function run(tx) {
  // симуляция или подпись и отправка; возвращает статус и изменения балансов кошелька
  if (simulate) {
    const r = await sdk.FullClient.sendSimulationTransaction(tx, address)
    const t = r?.Transaction ?? r?.FailedTransaction ?? {}
    return { simulated: true, ok: r?.$kind === 'Transaction' && t.effects?.status?.success !== false,
             status: t.effects?.status, balance_changes: t.balanceChanges }
  }
  tx.setSender(address)
  const r = await sdk.FullClient.signAndExecuteTransaction({ transaction: tx, signer: kp,
                                                              include: { effects: true, balanceChanges: true } })
  const t = r?.Transaction ?? r?.FailedTransaction ?? {}
  if (t.digest) await sdk.FullClient.waitForTransaction({ digest: t.digest }).catch(() => null)
  return { simulated: false, ok: r?.$kind === 'Transaction' && t.effects?.status?.success !== false,
           digest: t.digest, status: t.effects?.status, balance_changes: t.balanceChanges }
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

async function status() {
  const pool = await sdk.Pool.getPool(a.pool, true)
  return {
    ok: true, address, simulate,
    pool: { id: pool.id, coin_a: pool.coin_type_a, coin_b: pool.coin_type_b, sqrt_price: pool.current_sqrt_price,
            tick: pool.current_tick_index, tick_spacing: Number(pool.tick_spacing),
            rewarders: pool.rewarder_infos.map((r) => r.coin_type) },
    balances: await balances([pool.coin_type_a, pool.coin_type_b, SUI, ...pool.rewarder_infos.map((r) => r.coin_type)]),
    positions: await positions(pool.id),
  }
}

async function open() {
  const pool = await sdk.Pool.getPool(a.pool, true)
  const lower = Number(a['tick-lower']), upper = Number(a['tick-upper'])
  const sp = Number(pool.tick_spacing)
  if (lower % sp || upper % sp || lower >= upper) fail(`границы должны быть кратны шагу ${sp} и нижняя меньше верхней`)
  const have_a = new BN(String(a['amount-a'])), have_b = new BN(String(a['amount-b']))
  const cur = new BN(pool.current_sqrt_price)
  // фиксируем монету a; если монеты b с запасом на проскальзывание не хватает — уменьшаем a пропорционально.
  // Если монеты a нет совсем (диапазон ниже цены) — фиксируем b.
  let fix_a = !have_a.isZero()
  let est
  if (fix_a) {
    est = ClmmPoolUtil.estLiquidityAndCoinAmountFromOneAmounts(lower, upper, have_a, true, true, slippage, cur)
    const need_b = new BN(est.coin_amount_limit_b)
    if (need_b.gt(have_b)) {
      const x = have_a.mul(have_b).div(need_b).muln(998).divn(1000)
      est = ClmmPoolUtil.estLiquidityAndCoinAmountFromOneAmounts(lower, upper, x, true, true, slippage, cur)
      have_a.isub(have_a.sub(x))
    }
  } else {
    est = ClmmPoolUtil.estLiquidityAndCoinAmountFromOneAmounts(lower, upper, have_b, false, true, slippage, cur)
  }
  const amount_a = fix_a ? have_a.toString() : est.coin_amount_limit_a.toString()
  const amount_b = fix_a ? est.coin_amount_limit_b.toString() : have_b.toString()
  const before = new Set((await positions(pool.id)).map((p) => p.id))
  const tx = await sdk.Position.createAddLiquidityFixTokenPayload({
    coin_type_a: pool.coin_type_a, coin_type_b: pool.coin_type_b, pool_id: pool.id,
    tick_lower: String(lower), tick_upper: String(upper), fix_amount_a: fix_a, amount_a, amount_b,
    slippage, is_open: true, pos_id: '', rewarder_coin_types: [], collect_fee: false,
  })
  const res = await run(tx)
  let position = null
  if (!res.simulated && res.ok) {
    for (let i = 0; i < 10 && !position; i++) {   // ждём, пока новая позиция появится в индексе
      position = (await positions(pool.id)).find((p) => !before.has(p.id)) ?? null
      if (!position) await new Promise((r) => setTimeout(r, 1500))
    }
  }
  return { ...res, fix_amount_a: fix_a, amount_a, amount_b, liquidity: est.liquidity_amount?.toString?.() ?? null, position }
}

async function close() {
  const pool = await sdk.Pool.getPool(a.pool, true)
  const pos = await sdk.Position.getPositionById(a.position)
  const amounts = ClmmPoolUtil.getCoinAmountFromLiquidity(
    new BN(pos.liquidity), new BN(pool.current_sqrt_price),
    TickMath.tickIndexToSqrtPriceX64(Number(pos.tick_lower_index)),
    TickMath.tickIndexToSqrtPriceX64(Number(pos.tick_upper_index)), false)
  const tol = new Percentage(new BN(Math.round(slippage * 10000)), new BN(10000))
  const { coin_amount_limit_a, coin_amount_limit_b } = adjustForCoinSlippage(amounts, tol, false)
  const tx = await sdk.Position.closePositionPayload({
    coin_type_a: pool.coin_type_a, coin_type_b: pool.coin_type_b, pool_id: pool.id, pos_id: pos.pos_object_id,
    min_amount_a: coin_amount_limit_a.toString(), min_amount_b: coin_amount_limit_b.toString(),
    rewarder_coin_types: pool.rewarder_infos.map((r) => r.coin_type), collect_fee: true,
  })
  return { ...(await run(tx)), expected_a: amounts.coin_amount_a?.toString(), expected_b: amounts.coin_amount_b?.toString() }
}

async function swap() {
  const agg = new AggregatorClient({ env: Env.Mainnet, signer: address, client: sdk.FullClient })
  const route = await agg.findRouters({ from: a.from, target: a.to, amount: new BN(String(a.amount)), byAmountIn: true })
  if (!route || route.insufficientLiquidity) fail('агрегатор не нашёл маршрут')
  const tx = new Transaction()
  tx.setSender(address)
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
