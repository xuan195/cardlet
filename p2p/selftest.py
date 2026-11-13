#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名片 · 认领 + 担保机制端到端自检

重点验证那个窟窿补上了没有：
    冒名者抢先认领（时间戳更早）→ 因为拿不到担保签名 → 依然无效
    真店主后认领（时间戳更晚）→ 有 2 位老店主作保 → 反而赢得所有权

跑：python p2p/selftest.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from identity import Identity, canonical
from store import (Store, sync, reduce_events, sponsor_sign,
                   resolve_claims, SPONSOR_NEEDED)

KEYS = Path(__file__).resolve().parent / "_keys"
NOW = datetime(2026, 11, 13, 12, 0, 0)


def ts(m: int) -> str:
    return (NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")


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

    root = Identity.load_or_create(KEYS / "root.json")
    passer1 = Identity.load_or_create(KEYS / "passer1.json")
    passer2 = Identity.load_or_create(KEYS / "passer2.json")
    passer3 = Identity.load_or_create(KEYS / "passer3.json")
    owner_a = Identity.load_or_create(KEYS / "owner_a.json")
    owner_b = Identity.load_or_create(KEYS / "owner_b.json")
    owner_c = Identity.load_or_create(KEYS / "owner_c.json")
    faker = Identity.load_or_create(KEYS / "faker.json")

    # 所有设备共享一个根（发起人）
    def mk(ident):
        return Store(identity=ident, root_id=root.device_id)

    # ---------------- 先让两家店成立，作为后续担保人 ----------------
    print("\n[0] 发起人部署：老陈五金、张姐早餐 由发起人担保认领")
    b = mk(owner_b)
    b.add("anchor", "laochen", {"fields": {"name": "老陈五金", "address": "解放路 28 号"}},
          ts=ts(600))
    b.add("claim", "laochen",
          {"sponsors": [sponsor_sign(root, "laochen", owner_b.device_id)]}, ts=ts(590))

    c = mk(owner_c)
    c.add("anchor", "zhangjie", {"fields": {"name": "张姐早餐", "address": "菜市场东门"}},
          ts=ts(600))
    c.add("claim", "zhangjie",
          {"sponsors": [sponsor_sign(root, "zhangjie", owner_c.device_id)]}, ts=ts(590))

    check("老陈 → L2", b.state(NOW)["laochen"]["trust"] == "L2")
    check("张姐 → L2", c.state(NOW)["zhangjie"]["trust"] == "L2")

    # ---------------- 核心场景 ----------------
    print("[1] 路人甲标注「好又多」，还没有店主")
    a = mk(passer1)
    a.add("anchor", "haoyouduo", {"fields": {
        "name": "好又多超市", "address": "解放路 12 号",
        "lat": 29.8401, "lon": 112.9002}}, ts=ts(500))
    st = a.state(NOW)["haoyouduo"]
    check("等级 L0", st["trust"] == "L0", st["trust"])
    check("店名已记录", st["name"] == "好又多超市")
    check("没有店主", st["owner"] is None)

    print("[2] 另外两台设备也标了同一家店，内容一致")
    for i, ident in enumerate((passer2, passer3), start=2):
        p = mk(ident)
        p.add("fact", "haoyouduo", {"fields": {"name": "好又多超市", "open": True}}, ts=ts(400 + i))
        a.merge(list(p.events.values()))
    st = a.state(NOW)["haoyouduo"]
    check("三台设备一致 → L1", st["trust"] == "L1", (st["trust"], st["trust_reason"]))

    print("[3] 店主甲认领，但没有人作保")
    a_owner = mk(owner_a)
    a_owner.merge(list(a.events.values()))
    a_owner.add("claim", "haoyouduo", {"sponsors": []}, ts=ts(100))
    st = a_owner.state(NOW)["haoyouduo"]
    check("不给 L2（待担保）", st["trust"] != "L2", st["trust"])
    check("明确标出待担保", owner_a.device_id in st.get("claim_pending", []),
          st.get("claim_pending"))
    check("仍无店主", st["owner"] is None)

    print("[4] ★ 冒名者用「更早」的时间戳抢先认领，仍无担保")
    f = mk(faker)
    f.merge(list(a_owner.events.values()))
    f.add("claim", "haoyouduo", {"sponsors": []}, ts=ts(300))   # 时间比店主更早
    f.add("fact", "haoyouduo", {"fields": {"name": "黑心超市"}}, ts=ts(290))
    st = f.state(NOW)["haoyouduo"]
    check("冒名者依然不是店主", st["owner"] is None, st["owner"])
    check("冒名者也进待担保", faker.device_id in st.get("claim_pending", []))

    print("[5] ★ 老陈 + 张姐给店主甲作保（2 位）")
    sponsors = [sponsor_sign(owner_b, "haoyouduo", owner_a.device_id),
                sponsor_sign(owner_c, "haoyouduo", owner_a.device_id)]
    a_owner.add("claim", "haoyouduo", {"sponsors": sponsors}, ts=ts(90))
    merged = mk(passer1)
    for s in (a_owner, b, c, f):
        merged.merge(list(s.events.values()))
    st = merged.state(NOW)["haoyouduo"]
    check("升到 L2", st["trust"] == "L2", (st["trust"], st["trust_reason"]))
    check("店主是店主甲", st["owner"] == owner_a.device_id, st["owner"])
    check("冒名者未获认领权", st["owner"] != faker.device_id)
    check("店名没被冒名者改掉", st["name"] == "好又多超市", st["name"])

    print("[6] 伪造担保签名")
    f.merge(list(c.events.values()))     # 先让冒名者拿到真实的张姐数据
    fake_sponsor = [{"by": owner_b.device_id, "sig": "deadbeef"},
                    {"by": owner_c.device_id, "sig": "deadbeef"}]
    fake_claim = f.add("claim", "zhangjie", {"sponsors": fake_sponsor}, ts=ts(50))
    f.merge([fake_claim])
    st = f.state(NOW)["zhangjie"]
    check("伪造担保无效，店主仍是张姐", st["owner"] == owner_c.device_id, st["owner"])

    print("[7] 只有 1 位担保，不够")
    one = Store(identity=Identity.load_or_create(KEYS / "lone.json"), root_id=root.device_id)
    one.add("anchor", "lone_shop", {"fields": {"name": "独担保店"}}, ts=ts(200))
    one.add("claim", "lone_shop",
            {"sponsors": [sponsor_sign(owner_b, "lone_shop", one.identity.device_id)]},
            ts=ts(190))
    st = one.state(NOW)["lone_shop"]
    check(f"1 位担保不够（需 {SPONSOR_NEEDED} 位）", st["owner"] is None, st["owner"])

    print("[8] 店主更新营业状态，路人还说开着")
    a_owner.add("fact", "haoyouduo", {"fields": {"open": False, "items": [
        {"name": "本地鸡蛋", "unit": "斤", "tag": "可议"}]}}, ts=ts(20))
    merged.merge(list(a_owner.events.values()))
    st = merged.state(NOW)["haoyouduo"]
    check("以店主为准：已关门", st.get("open") is False, st.get("open"))
    check("货品按店主记录", [i["name"] for i in st.get("items", [])] == ["本地鸡蛋"])

    print("[9] 非店主改店名")
    f.add("fact", "haoyouduo", {"fields": {"name": "黑心超市"}}, ts=ts(10))
    merged.merge(list(f.events.values()))
    st = merged.state(NOW)["haoyouduo"]
    check("店名没被改掉", st["name"] == "好又多超市", st["name"])
    check("分歧被记录", "name" in st["conflicts"], st.get("conflicts"))

    print("[10] 店主打假：否认一条恶意货品")
    bad = merged.add("fact", "haoyouduo", {"fields": {
        "items": [{"name": "三无产品", "unit": "件", "tag": "可议"}]}}, ts=ts(6))
    a_owner.merge([bad])
    a_owner.add("deny", "haoyouduo", {"events": [bad["id"]]}, ts=ts(5))
    merged.merge(list(a_owner.events.values()))
    st = merged.state(NOW)["haoyouduo"]
    check("恶意货品已作废", "三无产品" not in [i["name"] for i in st.get("items", [])])

    print("[11] 时间衰减：只有「今天有什么」会过期，营业开关不过期")
    old = mk(passer2)
    old.add("anchor", "laochen", {"fields": {"name": "老陈五金"}}, ts=ts(60 * 24 * 9))
    old.add("fact", "laochen", {"fields": {
        "open": False,
        "items": [{"name": "电线", "unit": "卷", "note": "", "tag": "批发价"}]}},
        ts=ts(60 * 24 * 8))
    st_old = reduce_events(list(old.events.values()), old.pubkeys,
                           now=NOW + timedelta(days=1), root_id=root.device_id)
    check("8 天前的货品已过期", st_old["laochen"].get("items") is None,
          st_old["laochen"].get("items"))
    check("歇业状态不会自己失效", st_old["laochen"].get("open") is False,
          st_old["laochen"].get("open"))

    print("[12] 两机离线记录，同步后状态一致")
    pa, pb = mk(passer1), mk(passer2)
    pa.add("fact", "haoyouduo", {"fields": {"note": "熟客可赊账"}}, ts=ts(4))
    pb.add("fact", "laochen", {"fields": {"open": True}}, ts=ts(3))
    n1, n2 = sync(pa, pb)
    check("双向都收到新增", n1 >= 1 and n2 >= 1, (n1, n2))
    check("事件集相同", pa.ids() == pb.ids())
    check("归约一致", canonical(pa.state(NOW)) == canonical(pb.state(NOW)))
    check("重复同步无新增", sync(pa, pb) == (0, 0))

    print("[13] 篡改事件内容")
    target = next(iter(pa.events.values()))
    tampered = dict(target)
    tampered["payload"] = {"fields": {"name": "被篡改进去了"}}
    check("篡改事件被拒收", pb.merge([tampered]) == 0)

    print("[14] 没有根的首次部署（bootstrap）")
    solo = Store(identity=Identity.load_or_create(KEYS / "solo.json"), root_id=None)
    solo.add("anchor", "solo_shop", {"fields": {"name": "第一家店"}}, ts=ts(30))
    solo.add("claim", "solo_shop", {"sponsors": []}, ts=ts(20))
    st = solo.state(NOW)["solo_shop"]
    check("无根时首个认领生效（否则永远起不来）", st["trust"] == "L2", st["trust"])

    print("[15] 撤回：只有标注作者能删自己的标注")
    r = mk(owner_a)
    r.add("anchor", "del-me", {"fields": {"name": "临时店"}}, ts=ts(20))
    check("标注建立成功", "del-me" in r.state(NOW))
    p = mk(passer1)
    p.merge(list(r.events.values()))
    p.add("retract", "del-me", {}, ts=ts(15))
    check("别人撤回无效", "del-me" in p.state(NOW))
    r.merge(list(p.events.values()))
    r.add("retract", "del-me", {}, ts=ts(10))
    check("作者撤回后消失", "del-me" not in r.state(NOW))

    print(f"\n结果：{passed} 通过 / {failed} 失败\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
