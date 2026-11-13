#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名片 · 事件流与归约

核心思想
--------
不存"当前状态"，存"发生过什么"。

  存状态 → 两台手机同时改一家店，同步时必然打架，谁覆盖谁扯不清
  存事件 → 同步退化成"补齐对方没有的事件"，顺序无关，结果必然一致

四类事件
--------
  anchor  路人标注"这儿有家店"          冷启动必需，可信度最低
  claim   店主认领"这家店归我管"        带担保人签名，担保优先于先到先得
  fact    事实更新"今天开着 / 有货"      带有效期，过期自动降权
  deny    店主打假"这条不是我说的"       只能否认指向自己的店

认领为什么不是"先到先得"
------------------------
先到先得意味着冒名者抢先就赢。改成"担保优先"：
  · 发起人（根）签字 → 1 位即可
  · 其他已认领店主作保 → 需要 2 位
  · 没人作保 → 只显示"待担保"，不给 L2
冒名者拿不到任何担保签名，抢多早都没用。

归约规则
--------
  1. 验签失败的、被店主 deny 的，直接丢弃
  2. 认领：担保链先算全局（担保可跨店），再落到每店选店主
  3. 结构性字段（店名/地址/电话/坐标）：店主优先；无主时按独立作者数投票，平票取最早
  4. 时效性字段（营业/货品/备注）：店主优先；无主时取最新观测；超过 7 天失效
  5. 不裁决分歧：矛盾值全部呈现，只统计人数
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from identity import Identity, canonical, sha256_hex, device_id_of

# 会随时间失效的字段（"今天有什么"类）
TEMPORAL_FIELDS = {"items", "note"}
# 结构性字段（店名、地址、营业开关、运力牌内容）：写一次一直算数，直到有人改
STRUCTURAL_FIELDS = {"kind", "name", "address", "phone", "hours",
                     "lat", "lon", "tags", "vehicle", "capacity", "areas", "window",
                     "open", "imported",
                     # 名片模型：姓名、类型、版本号
                     "person", "type", "ver"}

DEFAULT_TTL_DAYS = 7          # 营业状态/货品 7 天没人确认就过期
ANCHOR_TTL_DAYS = 30          # 路人标注 30 天没人认领就淡出
TRUST_L1_AUTHORS = 3          # ≥3 个不同设备说同一件事 → L1
SPONSOR_NEEDED = 2            # 普通店主作保需要 2 位；发起人（根）1 位即可


# ---------------------------------------------------------------- 事件

def _event_body(kind: str, target: str, author: str, lamport: int,
                ts: str, payload: dict) -> dict:
    return {"type": kind, "target": target, "author": author,
            "lamport": int(lamport), "ts": ts, "payload": payload or {}}


def make_event(identity: Identity, kind: str, target: str, payload: dict,
               lamport: int, ts: str = None) -> dict:
    ts = ts or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    body = _event_body(kind, target, identity.device_id, lamport, ts, payload)
    message = canonical(body)          # 先定下被签名的内容，再挂 id/sig
    body["id"] = sha256_hex(message)[:16]
    body["sig"] = identity.sign(message)
    return body


def verify_event(event: dict, pubkeys: dict) -> bool:
    """pubkeys: device_id -> {n, e}"""
    try:
        body = {k: event[k] for k in ("type", "target", "author", "lamport", "ts", "payload")}
        if event.get("id") != sha256_hex(canonical(body))[:16]:
            return False
        pub = pubkeys.get(event["author"])
        if not pub:
            return False
        return Identity.verify(pub, canonical(body), event["sig"])
    except Exception:
        return False


# ---------------------------------------------------------------- 担保

def sponsor_message(shop_id: str, claimer: str) -> str:
    """担保人签的就是这句话：我认识他，这家店确实归他。"""
    return canonical({"kind": "sponsor", "shop": shop_id, "claimer": claimer})


def sponsor_sign(sponsor: Identity, shop_id: str, claimer_device_id: str) -> dict:
    """一位老店主给新店主的认领作保。

    公钥随签名一起走（自证明）：别人拿到这条担保就能验，
    不需要事先认识担保人，也不需要任何中心登记。
    """
    return {"by": sponsor.device_id,
            "pub": sponsor.pubkey,
            "sig": sponsor.sign(sponsor_message(shop_id, claimer_device_id))}


def resolve_claims(events: list, pubkeys: dict, root_id: str = None) -> dict:
    """全局解析所有认领，返回 {shop_id: 解析结果}。

    担保可跨店传播：老陈给自己的店认领后，就能替张姐作保。
    所以必须先全局算一遍担保链，再落到每家店。
    """
    claims = [e for e in events if e["type"] == "claim"]
    if not claims:
        return {}

    # 背书事件（endorse）：任何人可以独立给某人的认领作保，不用等对方重发
    endorse_map = {}
    for e in events:
        if e.get("type") != "endorse":
            continue
        p = e.get("payload") or {}
        shop, claimer = p.get("shop"), p.get("claimer")
        if not shop or not claimer:
            continue
        pub = pubkeys.get(e["author"]) or e.get("pubkey")
        if not pub:
            continue
        if not Identity.verify(pub, sponsor_message(shop, claimer), p.get("sig", "")):
            continue
        endorse_map.setdefault((shop, claimer), set()).add(e["author"])

    # 每条认领的担保人（先验签名，再谈可信）
    sponsors = {}
    for c in claims:
        msg = sponsor_message(c["target"], c["author"])
        got = set()
        for sp in c["payload"].get("sponsors", []):
            by = sp.get("by")
            pub = sp.get("pub") or pubkeys.get(by)
            # 公钥必须自证：指纹算出来要和 by 一致，否则是冒名
            if not pub or device_id_of(pub) != by:
                continue
            if not Identity.verify(pub, msg, sp.get("sig", "")):
                continue
            pubkeys[by] = pub          # 顺手登记，后续事件也能验
            got.add(by)
        got |= endorse_map.get((c["target"], c["author"]), set())
        sponsors[c["id"]] = got

    trusted = {root_id} if root_id else set()
    valid = set()
    bootstrap = not root_id

    # bootstrap：没有根时，只能让最早的认领先生效，否则永远没人能认领
    if bootstrap:
        first = sorted(claims, key=lambda e: (e["ts"], e["id"]))[0]
        valid.add(first["id"])
        trusted.add(first["author"])

    # 担保链传播：作保的人自己必须已经可信
    for _ in range(10):
        changed = False
        for c in claims:
            if c["id"] in valid:
                continue
            sp = sponsors[c["id"]]
            ok = (c["author"] in trusted                         # 已受信店主开新店，免担保
                  or (root_id and root_id in sp)                  # 发起人作保，一位即可
                  or (len(sp) >= SPONSOR_NEEDED if bootstrap      # 无根部署：任意 2 台设备
                      else len(sp & trusted) >= SPONSOR_NEEDED))
            if ok:
                valid.add(c["id"])
                trusted.add(c["author"])
                changed = True
        if not changed:
            break

    by_shop = {}
    for c in claims:
        by_shop.setdefault(c["target"], []).append(c)

    out = {}
    for shop, cs in by_shop.items():
        good = sorted([c for c in cs if c["id"] in valid], key=lambda c: (c["ts"], c["id"]))
        primary = good[0] if good else sorted(cs, key=lambda c: (c["ts"], c["id"]))[0]
        sponsor_list = sorted(sponsors.get(primary["id"], set()))
        out[shop] = {
            "owner": good[0]["author"] if good else None,
            "valid": {c["id"] for c in good},
            "pending": [c["author"] for c in cs if c["id"] not in valid],
            "contested": [c["author"] for c in good[1:]],
            "sponsor_ids": sponsor_list,
        }
    return out


# ---------------------------------------------------------------- 归约

def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return datetime(1970, 1, 1)


def reduce_events(events: list, pubkeys: dict, now: datetime = None,
                  root_id: str = None) -> dict:
    """把一堆乱序事件归约成"这条街当前长什么样"。

    root_id：发起人（根信任）的设备指纹。传 None 则走 bootstrap 规则。
    """
    now = now or datetime.now()

    # 1) 验签 + 去重
    valid, seen = [], set()
    for ev in events:
        if ev.get("id") in seen:
            continue
        if not verify_event(ev, pubkeys):
            continue
        seen.add(ev["id"])
        valid.append(ev)

    # 2) 全局解析认领（担保可跨店）
    rc_all = resolve_claims(valid, pubkeys, root_id)

    # 3) 按店铺归约
    groups = {}
    for ev in valid:
        groups.setdefault(ev["target"], []).append(ev)

    # 只有认领、没有 anchor 的孤儿不算一家店
    out = {}
    for shop_id, group in groups.items():
        if not any(e["type"] == "anchor" for e in group):
            continue
        reduced = _reduce_one(shop_id, group, now, rc_all.get(shop_id, {}))
        if reduced is not None:
            out[shop_id] = reduced
    return out


def _reduce_one(shop_id: str, group: list, now: datetime, rc: dict) -> dict:
    owner = rc.get("owner")
    contested = rc.get("contested") or []
    pending = rc.get("pending") or []

    # 店主的 deny：只能否认指向自己店铺的事件
    denied = set()
    for e in group:
        if e["type"] == "deny" and e["author"] == owner:
            denied.update(e["payload"].get("events", []))

    alive = [e for e in group if e["id"] not in denied]
    facts = [e for e in alive if e["type"] == "fact"]
    anchors = [e for e in alive if e["type"] == "anchor"]
    base_pool = list(anchors) + facts

    # 谁说的话算数：店主 > 标注作者 > 其他人
    authority = owner
    anchor_author = None
    if anchors:
        anchor_author = sorted(anchors, key=lambda e: (e["ts"], e["id"]))[0]["author"]
        if not authority:
            authority = anchor_author

    # 撤回：只有标注作者能删自己的标注
    # 日志只增不减，所以用一条 retract 事件把整条记录作废
    if any(e["type"] == "retract" and e["author"] == anchor_author for e in group):
        return None

    state = {"id": shop_id}
    conflicts = {}

    for field in STRUCTURAL_FIELDS | TEMPORAL_FIELDS:
        cands = []
        for e in base_pool:
            p = e["payload"] if isinstance(e.get("payload"), dict) else {}
            fields = p.get("fields") if isinstance(p.get("fields"), dict) else p
            if not isinstance(fields, dict) or field not in fields:
                continue
            # 时间衰减只作用于"今天有什么"
            if field in TEMPORAL_FIELDS and \
                    (now - _parse_ts(e["ts"])) > timedelta(days=DEFAULT_TTL_DAYS):
                continue
            cands.append((e["author"], e["ts"], e["lamport"], e["id"], fields[field]))

        if not cands:
            continue

        # 分歧统计：按"独立作者数"投票，不按事件数（一台设备刷一百条也只算一票）
        by_value = {}
        for author, ts_, _, _, val in cands:
            key = json.dumps(val, ensure_ascii=False, sort_keys=True)
            slot = by_value.setdefault(key, {"authors": set(), "first": ts_, "value": val})
            slot["authors"].add(author)
            slot["first"] = min(slot["first"], ts_)
        if len(by_value) > 1:
            conflicts[field] = {k: len(v["authors"]) for k, v in by_value.items()}

        owner_c = [c for c in cands if c[0] == authority]
        if owner_c:
            owner_c.sort(key=lambda c: (c[1], c[2], c[3]))
            state[field] = owner_c[-1][4]
        elif field in STRUCTURAL_FIELDS:
            # 店名/地址这类"事实"：路人只能投票建议，不能单方面推翻
            ranked = sorted(by_value.values(), key=lambda v: (-len(v["authors"]), v["first"]))
            state[field] = ranked[0]["value"]
        else:
            # "今天开着吗/有什么货"：最新一次观测最可信
            latest = sorted(cands, key=lambda c: (c[1], c[2], c[3]))[-1]
            state[field] = latest[4]

    # 可信度分级
    authors = {e["author"] for e in base_pool}
    if owner:
        trust, reason = "L2", "店主已认领并有担保人作保"
    elif pending:
        trust, reason = "L0", f"{len(pending)} 条认领待担保（需 2 位已认领店主作保）"
        state["claim_pending"] = pending
    elif len(authors) >= TRUST_L1_AUTHORS and not conflicts:
        trust, reason = "L1", f"{len(authors)} 台设备各自标注且一致"
    elif anchors and (now - _parse_ts(anchors[0]["ts"])) > timedelta(days=ANCHOR_TTL_DAYS):
        trust, reason = "L0", "路人标注超过 30 天无人认领，已淡出"
        state["faded"] = True
    else:
        trust, reason = "L0", "仅路人标注，未经店主确认"

    state.update({
        "trust": trust,
        "trust_reason": reason,
        "owner": owner,
        "contested": contested,
        "conflicts": conflicts,
        "sponsors": rc.get("sponsor_ids") or [],
        "events": len(group),
        "updated": max((e["ts"] for e in group), default=""),
    })
    return state


# ---------------------------------------------------------------- 本地库

class Store:
    """一台设备上的事件日志。append-only，永不删除。"""

    def __init__(self, path: Path = None, identity: Identity = None,
                 root_id: str = None):
        self.path = Path(path) if path else None
        self.identity = identity
        self.root_id = root_id        # 发起人（根信任）的设备指纹
        self.events = {}              # id -> event
        self.pubkeys = {}             # device_id -> pubkey
        self.lamport = 0
        if self.path and self.path.exists():
            self._load()

    # ---- 持久化 ----
    def _load(self):
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            self.events[ev["id"]] = ev
            if ev.get("pubkey"):
                self.pubkeys[ev["author"]] = ev["pubkey"]
            self.lamport = max(self.lamport, ev.get("lamport", 0))

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for ev in self.events.values():
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    # ---- 写入 ----
    def add(self, kind: str, target: str, payload: dict, ts: str = None) -> dict:
        self.lamport += 1
        ev = make_event(self.identity, kind, target, payload, self.lamport, ts)
        # 公钥随事件一起传播，别人才能验签
        ev["pubkey"] = self.identity.pubkey
        self.events[ev["id"]] = ev
        self.pubkeys[self.identity.device_id] = self.identity.pubkey
        return ev

    def merge(self, events: list) -> int:
        """接收别人的事件：验签后入库，返回新增条数。"""
        added = 0
        for ev in events:
            if ev.get("id") in self.events:
                continue
            pub = ev.get("pubkey") or self.pubkeys.get(ev.get("author"))
            if not pub or not verify_event(ev, {ev["author"]: pub}):
                continue
            self.events[ev["id"]] = ev
            self.pubkeys[ev["author"]] = pub
            self.lamport = max(self.lamport, ev.get("lamport", 0))
            added += 1
        return added

    # ---- 同步 ----
    def ids(self) -> set:
        return set(self.events.keys())

    def diff(self, other_ids: set) -> list:
        """对方没有的事件（同步时发过去的东西）。"""
        return [self.events[i] for i in self.events if i not in other_ids]

    def state(self, now: datetime = None) -> dict:
        return reduce_events(list(self.events.values()), self.pubkeys, now, self.root_id)

    def sponsor(self, shop_id: str, claimer_device_id: str) -> dict:
        """我（已认领的店主）给某人作保。"""
        return sponsor_sign(self.identity, shop_id, claimer_device_id)


def sync(a: Store, b: Store) -> tuple:
    """最简单的双向同步：互相补齐差集。顺序无关，跑几次结果都一样。"""
    n_ab = b.merge(a.diff(b.ids()))
    n_ba = a.merge(b.diff(a.ids()))
    return n_ab, n_ba
