#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名片 · 完整演示：三台手机各记各的，碰一面之后是同一条街

演示四件事：
  1. 发起人部署时，用自己的签名担保第一批店主
  2. 三台手机离线各记各的，碰面后事件自动补齐
  3. 路人把货标错了，店主认领 + 否认，一键纠正
  4. 冒名者抢先认领没用 —— 他拿不到担保签名

跑：python p2p/demo.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from identity import Identity
from store import Store, sync, sponsor_sign

KEYS = Path(__file__).resolve().parent / "_keys"
NOW = datetime(2026, 11, 13, 12, 0, 0)

BADGE = {"L2": "✓ 店主认领", "L1": "多人确认", "L0": "路人标注"}


def ts(m: int) -> str:
    return (NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")


def main():
    root = Identity.load_or_create(KEYS / "root.json")
    owner_a = Identity.load_or_create(KEYS / "owner.json")
    owner_b = Identity.load_or_create(KEYS / "passer2.json")
    rider = Identity.load_or_create(KEYS / "passer3.json")
    owner_z = Identity.load_or_create(KEYS / "owner_z.json")

    def mk(ident):
        return Store(identity=ident, root_id=root.device_id)

    # ---- 三台手机，各自离线记录 ----
    phone_a = mk(owner_a)      # 好又多老板
    phone_b = mk(owner_b)      # 老陈五金老板
    phone_rider = mk(rider)    # 骑手老王

    # 发起人部署时，先替这两家做担保
    phone_a.add("anchor", "haoyouduo", {"fields": {
        "name": "好又多超市", "address": "解放路 12 号",
        "lat": 29.8401, "lon": 112.9002, "phone": "13800000001"}}, ts=ts(600))
    phone_a.add("claim", "haoyouduo",
                {"sponsors": [sponsor_sign(root, "haoyouduo", owner_a.device_id)]}, ts=ts(590))
    phone_a.add("fact", "haoyouduo", {"fields": {
        "open": True, "hours": "07:00-22:00",
        "items": [{"name": "本地鸡蛋", "unit": "斤", "note": "今早到货", "tag": "可议"},
                  {"name": "大米", "unit": "袋", "note": "25kg", "tag": "批发价"}]}}, ts=ts(30))

    phone_b.add("anchor", "laochen", {"fields": {
        "name": "老陈五金", "address": "解放路 28 号",
        "lat": 29.8412, "lon": 112.9016, "phone": "13800000002"}}, ts=ts(500))
    phone_b.add("claim", "laochen",
                {"sponsors": [sponsor_sign(root, "laochen", owner_b.device_id)]}, ts=ts(490))
    phone_b.add("fact", "laochen", {"fields": {
        "open": False, "hours": "08:00-18:30",
        "items": [{"name": "电线 2.5 平方", "unit": "卷", "note": "国标", "tag": "批发价"}]}},
        ts=ts(120))

    # 骑手先看到张姐的早餐店（张姐还没装 App，所以只是路人标注）
    phone_rider.add("anchor", "zhangjie", {"fields": {
        "name": "张姐早餐", "address": "菜市场东门",
        "lat": 29.8393, "lon": 112.8988}}, ts=ts(400))
    phone_rider.add("fact", "zhangjie", {"fields": {
        "open": True, "hours": "05:30-10:30",
        "items": [{"name": "热干面", "unit": "碗", "note": "", "tag": "零售价"}]}}, ts=ts(20))
    # 手滑标错了一条
    phone_rider.add("fact", "zhangjie", {"fields": {
        "items": [{"name": "三无辣条", "unit": "包", "note": "便宜", "tag": "可议"}]}},
        ts=ts(15))

    print("\n【碰面前】三台手机各看到的世界")
    for name, phone in (("老板甲", phone_a), ("老板乙", phone_b), ("骑手", phone_rider)):
        print(f"  {name}：{len(phone.state(NOW))} 家店")

    # ---- 在菜市场碰一面 ----
    sync(phone_a, phone_b)
    sync(phone_b, phone_rider)
    sync(phone_a, phone_rider)

    def show(phone):
        for shop in sorted(phone.state(NOW).values(),
                           key=lambda s: (not s.get("open"), s["name"])):
            mark = "营业中" if shop.get("open") else "已打烊"
            items = "、".join(i["name"] for i in shop.get("items", [])) or "今日未上货"
            print(f"  {shop['name']:<8} {mark}  {BADGE.get(shop['trust'], shop['trust'])}")
            print(f"            {shop.get('address','—')} · {shop.get('hours','—')}")
            print(f"            今日有货：{items}")
            if shop.get("conflicts"):
                print(f"            ⚠ 存在分歧：{list(shop['conflicts'].keys())}")
            print()

    print("【碰面后】任何一台手机，看到的都是同一条街")
    print("  " + "─" * 56)
    show(phone_a)

    # ---- 张姐来了：认领 + 纠错 ----
    print("【纠错】张姐装上 App，两位邻居作保，然后否认那条错货")
    phone_z = mk(owner_z)
    phone_z.merge(list(phone_a.events.values()))
    sponsors = [sponsor_sign(owner_a, "zhangjie", owner_z.device_id),
                sponsor_sign(owner_b, "zhangjie", owner_z.device_id)]
    phone_z.add("claim", "zhangjie", {"sponsors": sponsors}, ts=ts(10))
    bad = [e for e in phone_z.events.values()
           if e["type"] == "fact" and "三无辣条" in str(e["payload"])]
    if bad:
        phone_z.add("deny", "zhangjie", {"events": [bad[0]["id"]]}, ts=ts(8))

    sync(phone_z, phone_a)
    sync(phone_z, phone_b)
    sync(phone_z, phone_rider)
    sync(phone_a, phone_rider)
    sync(phone_b, phone_rider)

    z = phone_a.state(NOW)["zhangjie"]
    print(f"  张姐早餐 → {BADGE[z['trust']]}，今日有货："
          f"{'、'.join(i['name'] for i in z.get('items', [])) or '（无）'}")

    # ---- 冒名者尝试 ----
    print("\n【冒名】有人想抢走好又多（哪怕时间戳更早）")
    faker = Identity.load_or_create(KEYS / "faker.json")
    phone_f = mk(faker)
    phone_f.merge(list(phone_a.events.values()))
    phone_f.add("claim", "haoyouduo", {"sponsors": []}, ts=ts(300))   # 比店主更早
    sync(phone_f, phone_a)
    sync(phone_f, phone_b)
    sync(phone_f, phone_rider)
    sync(phone_f, phone_z)
    h = phone_a.state(NOW)["haoyouduo"]
    print(f"  冒名者时间更早，但没有担保 → 好又多的店主仍是："
          f"{'原店主' if h['owner'] == owner_a.device_id else '被抢走'}")
    print(f"  店名没被改：{h['name']}")

    same = (phone_a.ids() == phone_b.ids() == phone_rider.ids()
            == phone_z.ids() == phone_f.ids())
    print(f"\n五台手机的事件集完全一致：{same}")
    print(f"（冒名者的事件也在里面，但他依然不是店主 —— 数据传开了，权力没传开）\n")


if __name__ == "__main__":
    main()
