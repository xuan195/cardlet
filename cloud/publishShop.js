// 云函数 publishShop —— 上传一家店的数据文件
//
// 一次调用做三件事：
//   1. 校验字段（白名单；出现价格数字或评价字段直接拒绝）
//   2. 把完整目录写成文件传到云存储 catalog/<shopId>.json
//   3. 在数据库 shops 集合里 upsert 一条"索引记录"（只有指针和摘要）
//
// 为什么不让客户端直连数据库：客户端直写无法校验内容，
// 有人塞个 price: 9.9 进来，"不标价"这条红线就破了。
//
// 防篡改靠 OPENID：记录里的 _openid 是微信给的，改别人的记录会被拒绝。

const cloud = require('wx-server-sdk')
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV })

const db = cloud.database()
const COLL = 'shops'
const crypto = require('crypto')

const ITEM_FIELDS = ['name', 'unit', 'note', 'tag']
const PRICE_TAGS = ['可议', '批发价', '零售价', '面谈']

// 绝对禁止出现的字段名 —— 任何人传进来直接拒绝，不做"尽力而为"的清洗
const FORBIDDEN = ['price', 'amount', 'money', 'fee', 'rate', 'score',
                   'rating', 'review', 'comment', 'star', 'order', 'pay']

function bad(obj, path = '') {
  for (const k of Object.keys(obj || {})) {
    if (FORBIDDEN.includes(k.toLowerCase())) return `${path}${k}`
    const v = obj[k]
    if (v && typeof v === 'object') {
      const r = bad(v, `${path}${k}.`)
      if (r) return r
    }
  }
  return ''
}

function str(v, max) {
  return String(v == null ? '' : v).trim().slice(0, max)
}

exports.main = async (event) => {
  const { OPENID } = cloud.getWXContext()
  if (!OPENID) return { ok: false, error: 'no openid' }

  const raw = (event && event.shop) || {}
  const hit = bad(raw)
  if (hit) return { ok: false, error: `字段 "${hit}" 不被允许：本工具不记录价格与评价` }

  const id = str(raw.id || OPENID.replace(/[^a-zA-Z0-9]/g, '').slice(0, 16), 48)
  if (!id) return { ok: false, error: 'bad id' }

  const items = (Array.isArray(raw.items) ? raw.items : []).slice(0, 60).map(it => {
    const tag = PRICE_TAGS.includes(it.tag) ? it.tag : '可议'
    return {
      name: str(it.name, 40),
      unit: str(it.unit, 10),
      note: str(it.note, 60),
      tag,
    }
  }).filter(it => it.name)

  const file = {
    id,
    name: str(raw.name, 30) || id,
    address: str(raw.address, 60),
    phone: str(raw.phone, 20),
    hours: str(raw.hours, 30),
    lat: Number(raw.lat) || 0,
    lon: Number(raw.lon) || 0,
    tags: (Array.isArray(raw.tags) ? raw.tags : []).slice(0, 6).map(t => str(t, 10)),
    note: str(raw.note, 100),
    open: !!raw.open,
    items,
    updated: new Date().toISOString(),
    owner: OPENID.slice(0, 8),      // 只存短标识，不存完整 openid 到公开文件里
  }

  const body = JSON.stringify(file)
  const hash = crypto.createHash('sha1').update(body).digest('hex').slice(0, 16)

  // 1) 写文件
  const uploaded = await cloud.uploadFile({
    cloudPath: `catalog/${id}.json`,
    fileContent: Buffer.from(body, 'utf8'),
  })

  // 2) 写索引记录：先查是否已被别人占用，再 upsert
  let existing = null
  try {
    const got = await db.collection(COLL).doc(id).get()
    existing = got.data
  } catch (e) {
    existing = null   // 文档不存在时 get 会抛错
  }
  if (existing && existing._openid && existing._openid !== OPENID) {
    return { ok: false, error: '这家店的标识已被他人占用，请换一个' }
  }
  const record = {
    _id: id,
    _openid: OPENID,
    name: file.name,
    address: file.address,
    phone: file.phone,
    hours: file.hours,
    lat: file.lat,
    lon: file.lon,
    tags: file.tags,
    note: file.note,
    open: file.open,
    brief: items.slice(0, 4).map(i => i.name).join('、'),
    count: items.length,
    hash,
    fileID: uploaded.fileID,
    updated: file.updated,
  }
  try {
    await db.collection(COLL).doc(id).set({ data: record })
  } catch (e) {
    return { ok: false, error: 'write failed: ' + (e && e.message || e) }
  }

  return { ok: true, id, hash, fileID: uploaded.fileID, updated: file.updated }
}
