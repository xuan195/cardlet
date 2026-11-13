#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门牌 · 认领机制演示：三台手机各记各的，碰一面之后，同一条街

跑：python p2p/demo.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from identity import Identity
from store import Store, sync

KEYS = Path(__file__).resolve().parent / "_keys"
NOW = datetime(2026, 11, 13, 12, 0, 0)


def ts(m: int) -> str:
    return (NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")


BADGE = {"L2": "✓ 店主认领", "L1": "多人确认", "L0": "路人标注"}


def main():
    owner_a = Identity.load_or_create(KEYS / "owner.json")
    owner_b = Identity.load_or_create(KEYS / "passer2.json")
    rider = Identity.load_or_create(KEYS / "passer3.json")

    # 三台手机，各自离线记录
    phone_owner = Store(identity=owner_a)   # 好又多的老板
    phone_b = Store(identity=owner_b)       # 老陈五金的老板
    phone_rider = Store(identity=rider)     # 骑手老王

    phone_owner.add("anchor", "haoyouduo", {"fields": {
        "name": "好又多超市", "address": "解放路 12 号",
        "lat": 29.8401, "lon": 112.9002, "phone": "13800000001"}}, ts=ts(600))
    phone_owner.add("claim", "haoyouduo", {}, ts=ts(590))
    phone_owner.add("fact", "haoyouduo", {"fields": {
        "open": True, "hours": "07:00-22:00",
        "items": [{"name": "本地鸡蛋", "unit": "斤", "note": "今早到货", "tag": "可议"},
                  {"name": "大米", "unit": "袋", "note": "25kg", "tag": "批发价"}]}},
        ts=ts(30))

    phone_b.add("anchor", "laochen", {"fields": {
        "name": "老陈五金", "address": "解放路 28 号",
        "lat": 29.8412, "lon": 112.9016, "phone": "13800000002"}}, ts=ts(500))
    phone_b.add("claim", "laochen", {}, ts=ts(490))
    phone_b.add("fact", "laochen", {"fields": {
        "open": False, "hours": "08:00-18:30",
        "items": [{"name": "电线 2.5 平方", "unit": "卷", "note": "国标", "tag": "批发价"}]}},
        ts=ts(120))

    phone_rider.add("anchor", "zhangjie", {"fields": {
        "name": "张姐早餐", "address": "菜市场东门",
        "lat": 29.8393, "lon": 112.8988}}, ts=ts(400))
    phone_rider.add("fact", "zhangjie", {"fields": {
        "open": True, "hours": "05:30-10:30",
        "items": [{"name": "热干面", "unit": "碗", "note": "", "tag": "零售价"}]}}, ts=ts(20))
    phone_rider.add("fact", "zhangjie", {"fields": {
        "items": [{"name": "三无辣条", "unit": "包", "note": "便宜", "tag": "可议"}]}},
        ts=ts(15))

    print("\n【碰面前】三台手机各看到的世界")
    for name, phone in (("老板甲", phone_owner), ("老板乙", phone_b), ("骑手", phone_rider)):
        print(f"  {name}：{len(phone.state(NOW))} 家店")

    # 三台手机在菜市场碰了一面，互相同步
    sync(phone_owner, phone_b)
    sync(phone_b, phone_rider)
    sync(phone_owner, phone_rider)

    print("\n【碰面后】任何一台手机，看到的都是同一条街")
    print("  " + "─" * 56)
    for shop in sorted(phone_owner.state(NOW).values(),
                       key=lambda s: (not s.get("open"), s["name"])):
        mark = "营业中" if shop.get("open") else "已打烊"
        items = "、".join(i["name"] for i in shop.get("items", [])) or "今日未上货"
        print(f"  {shop['name']:<8} {mark}  {BADGE.get(shop['trust'], shop['trust'])}")
        print(f"            {shop.get('address','—')} · {shop.get('hours','—')}")
        print(f"            今日有货：{items}")
        if shop.get("conflicts"):
            print(f"            ⚠ 存在分歧：{list(shop['conflicts'].keys())}")
        print()

    # 张姐的店被骑手误标了"三无辣条"，店主认领后一键否认
    print("【纠错】张姐认领自己的店，并否认那条错误货品")
    owner_z = Identity.load_or_create(KEYS / "faker.json")
    phone_z = Store(identity=owner_z)
    phone_z.merge(list(phone_owner.events.values()))
    phone_z.add("claim", "zhangjie", {}, ts=ts(10))
    bad = [e for e in phone_z.events.values()
           if e["type"] == "fact" and "三无辣条" in str(e["payload"])]
    if bad:
        phone_z.add("deny", "zhangjie", {"events": [bad[0]["id"]]}, ts=ts(8))
    sync(phone_z, phone_owner)
    sync(phone_z, phone_rider)
    sync(phone_owner, phone_rider)

    z = phone_owner.state(NOW)["zhangjie"]
    print(f"  张姐早餐 → {BADGE[z['trust']]}，今日有货："
          f"{'、'.join(i['name'] for i in z.get('items', [])) or '（无）'}")
    print(f"  三条手机的事件集完全一致："
          f"{phone_owner.ids() == phone_z.ids() == phone_rider.ids()}\n")


if __name__ == "__main__":
    main()
