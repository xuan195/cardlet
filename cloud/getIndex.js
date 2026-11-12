// 云函数 getIndex —— 返回全城索引
//
// 职责单一：把"店铺索引"合并成一个数组返回给客户端。
// 索引里只有指针和摘要，不含任何价格、评价、交易字段。
//
// 部署：微信开发者工具 → cloudfunctions/getIndex → 右键「上传并部署」
// 依赖：wx-server-sdk（云函数模板自带，无需额外安装）

const cloud = require('wx-server-sdk')
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV })

const db = cloud.database()
const _ = db.command
const COLL = 'shops'

// 只允许出现在索引里的字段 —— 白名单，防止未来有人往里塞价格或评价
const PUBLIC_FIELDS = [
  'name', 'address', 'phone', 'hours', 'lat', 'lon',
  'tags', 'note', 'open', 'brief', 'count', 'hash', 'fileID', 'updated'
]

exports.main = async (event) => {
  const { since = '', limit = 200, skip = 0 } = event || {}

  // since 用于增量：客户端带上上次拿到的更新时间，只返回更新的
  const where = since ? { updated: _.gt(since) } : {}

  try {
    const res = await db.collection(COLL)
      .where(where)
      .orderBy('updated', 'desc')
      .skip(skip)
      .limit(Math.min(limit, 500))
      .field(PUBLIC_FIELDS.reduce((acc, k) => (acc[k] = true, acc), { _id: true }))
      .get()

    const shops = res.data.map(doc => {
      const out = { id: doc._id }
      PUBLIC_FIELDS.forEach(k => { if (doc[k] !== undefined) out[k] = doc[k] })
      return out
    })

    // 营业中的排前面，其次按更新时间
    shops.sort((a, b) => {
      if (!!a.open !== !!b.open) return a.open ? -1 : 1
      return String(b.updated || '').localeCompare(String(a.updated || ''))
    })

    return {
      ok: true,
      count: shops.length,
      hasMore: shops.length === Math.min(limit, 500),
      shops,
      serverTime: new Date().toISOString(),
    }
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) }
  }
}
