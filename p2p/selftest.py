#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门牌 · 认领机制端到端自检

验证 10 件事：
  1. 路人标注 → 只有 L0
  2. 三台设备独立标注且一致 → 升到 L1
  3. 店主认领 → L2，修改生效
  4. 别人再认领 → 无效，记为争议
  5. 店主更新营业状态 → 以店主为准
  6. 非店主改店名 → 店主的优先
  7. 店主 deny → 那条事件被作废
  8. 超过 TTL 的"今天有货"→ 不参与归约
  9. 两台设备互相同步 → 状态完全一致（顺序无关）
 10. 有人篡改事件内容 → 验签失败，被丢弃

跑：python p2p/selftest.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from identity import Identity, canonical
from store import Store, sync, reduce_events

KEYS = Path(__file__).resolve().parent / "_keys"
NOW = datetime(2026, 11, 13, 12, 0, 0)
T_SHOP = "haoyouduo"


def ts(minutes_ago: int = 0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def main():
    passed, failed = 0, 0

    def check(name, cond, extra=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  ✓ {name}")
        else:
            failed += 1
            print(f"  ✗ {name}   {extra}")

    # 四个身份：三个路人 + 一个店主；外加一个冒名者
    passer1 = Identity.load_or_create(KEYS / "passer1.json")
    passer2 = Identity.load_or_create(KEYS / "passer2.json")
    passer3 = Identity.load_or_create(KEYS / "passer3.json")
    owner = Identity.load_or_create(KEYS / "owner.json")
    faker = Identity.load_or_create(KEYS / "faker.json")

    print("\n[1] 路人甲标注「这儿有家店」")
    s1 = Store(identity=passer1)
    s1.add("anchor", T_SHOP, {"fields": {
        "name": "好又多超市", "address": "解放路 12 号",
        "lat": 29.8401, "lon": 112.9002}},
        ts=ts(minutes_ago=300))
    st = s1.state(NOW)[T_SHOP]
    check("等级 L0", st["trust"] == "L0", st["trust"])
    check("店名已记录", st["name"] == "好又多超市")
    check("没有店主", st["owner"] is None)

    print("[2] 另外两台设备也标了同一家店，内容一致")
    s2 = Store(identity=passer2)
    s3 = Store(identity=passer3)
    for i, s in enumerate((s2, s3), start=2):
        s.add("fact", T_SHOP, {"fields": {
            "name": "好又多超市", "open": True,
            "items": [{"name": "本地鸡蛋", "unit": "斤", "tag": "可议"}]}},
            ts=ts(minutes_ago=200 - i * 10))
    merged = Store(identity=passer1)
    for s in (s1, s2, s3):
        merged.merge(list(s.events.values()))
    st = merged.state(NOW)[T_SHOP]
    check("三台设备一致 → L1", st["trust"] == "L1", (st["trust"], st.get("trust_reason")))
    check("营业状态为开", st.get("open") is True)
    check("货品已记录", len(st.get("items", [])) == 1)

    print("[3] 店主认领")
    s_owner = Store(identity=owner)
    s_owner.merge(list(merged.events.values()))
    claim = s_owner.add("claim", T_SHOP, {}, ts=ts(minutes_ago=100))
    merged.merge(list(s_owner.events.values()))
    st = merged.state(NOW)[T_SHOP]
    check("升级到 L2", st["trust"] == "L2", st["trust"])
    check("店主指纹正确", st["owner"] == owner.device_id)

    print("[4] 冒名者想抢认领")
    s_faker = Store(identity=faker)
    s_faker.merge(list(merged.events.values()))
    s_faker.add("claim", T_SHOP, {}, ts=ts(minutes_ago=50))
    merged.merge(list(s_faker.events.values()))
    st = merged.state(NOW)[T_SHOP]
    check("认领权仍归原店主", st["owner"] == owner.device_id)
    check("冒名者被记为争议", faker.device_id in st["contested"], st["contested"])

    print("[5] 店主更新营业状态（关门），路人还说开着")
    s_owner.add("fact", T_SHOP, {"fields": {"open": False}}, ts=ts(minutes_ago=10))
    merged.merge(list(s_owner.events.values()))
    st = merged.state(NOW)[T_SHOP]
    check("以店主为准：已关门", st.get("open") is False, st.get("open"))
    check("分歧被记录", "open" in st["conflicts"], st.get("conflicts"))

    print("[6] 非店主试图改店名")
    s_faker.add("fact", T_SHOP, {"fields": {"name": "黑心超市"}}, ts=ts(minutes_ago=5))
    merged.merge(list(s_faker.events.values()))
    st = merged.state(NOW)[T_SHOP]
    check("店名没被改掉", st["name"] == "好又多超市", st["name"])

    print("[7] 店主打假：否认一条恶意货品")
    bad = merged.add("fact", T_SHOP, {"fields": {
        "items": [{"name": "三无产品", "unit": "件", "tag": "可议"}]}},
        ts=ts(minutes_ago=3))
    # 上面这条是"路人一号"发的（merged 用的是 passer1 身份）
    s_owner.merge([bad])
    s_owner.add("deny", T_SHOP, {"events": [bad["id"]]}, ts=ts(minutes_ago=2))
    merged.merge(list(s_owner.events.values()))
    st = merged.state(NOW)[T_SHOP]
    names = [i["name"] for i in st.get("items", [])]
    check("恶意货品已作废", "三无产品" not in names, names)

    print("[8] 时间衰减：8 天前的营业状态不算数")
    old = Store(identity=passer2)
    old.add("fact", T_SHOP, {"fields": {"open": True}}, ts=ts(minutes_ago=60 * 24 * 8))
    st_old = reduce_events(list(old.events.values()), old.pubkeys,
                           now=NOW + timedelta(days=1))
    check("过期状态不参与", st_old[T_SHOP].get("open") is None,
          st_old[T_SHOP].get("open"))

    print("[9] 两台设备各自离线记录，同步后状态一致")
    phoneA = Store(identity=passer1)
    phoneB = Store(identity=passer2)
    phoneA.add("fact", T_SHOP, {"fields": {"note": "A 记的：熟客可赊账"}}, ts=ts(minutes_ago=4))
    phoneB.add("fact", "laochen", {"fields": {"name": "老陈五金", "open": True}},
               ts=ts(minutes_ago=3))
    a1, a2 = sync(phoneA, phoneB)
    check("A 收到 B 的事件", a2 >= 1, a2)
    check("B 收到 A 的事件", a1 >= 1, a1)
    check("两边事件集相同", phoneA.ids() == phoneB.ids())
    st_a = phoneA.state(NOW)
    st_b = phoneB.state(NOW)
    check("归约结果一致", canonical(st_a) == canonical(st_b))
    check("两家店都在", set(st_a.keys()) == {T_SHOP, "laochen"}, list(st_a.keys()))
    check("重复同步不再产生新增", sync(phoneA, phoneB) == (0, 0))

    print("[10] 有人篡改事件内容")
    target = next(iter(phoneA.events.values()))
    tampered = dict(target)
    tampered["payload"] = {"fields": {"name": "被篡改进去了"}}
    rejected = phoneB.merge([tampered])
    check("篡改事件被拒收", rejected == 0, rejected)

    print(f"\n结果：{passed} 通过 / {failed} 失败\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
