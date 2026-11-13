#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门牌 · 事件流与归约

核心思想
--------
不存"当前状态"，存"发生过什么"。

  存状态 → 两台手机同时改一家店，同步时必然打架，谁覆盖谁扯不清
  存事件 → 同步退化成"补齐对方没有的事件"，顺序无关，结果必然一致

四类事件
--------
  anchor  路人标注"这儿有家店"          冷启动必需，可信度最低
  claim   店主认领"这家店归我管"        带签名，公钥即所有权
  fact    事实更新"今天开着 / 有货"      带有效期，过期自动降权
  deny    店主打假"这条不是我说的"       只能否认指向自己的店

归约规则
--------
  1. 验签失败的、被店主 deny 的，直接丢弃
  2. 认领权：最早的 claim 生效（先到先得），后来的记为"争议"
  3. 字段权威：店主的事实永远优先；无主时取最新
  4. 时间衰减：营业状态 / 货品 / 备注 超过 TTL 视为过期；店名地址不过期
  5. 不裁决分歧：同一字段出现矛盾值，统计各方人数并全部呈现
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from identity import Identity, canonical, sha256_hex

# 会随时间失效的字段（"今天有什么"类）
TEMPORAL_FIELDS = {"open", "items", "note"}
# 结构性字段（店在哪儿、叫什么，不会因为没人更新就消失）
STRUCTURAL_FIELDS = {"name", "address", "phone", "hours", "lat", "lon", "tags"}

DEFAULT_TTL_DAYS = 7          # 营业状态/货品 7 天没人确认就过期
ANCHOR_TTL_DAYS = 30          # 路人标注 30 天没人认领就淡出
TRUST_L1_AUTHORS = 3          # ≥3 个不同设备说同一件事 → L1


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


# ---------------------------------------------------------------- 归约

def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return datetime(1970, 1, 1)


def reduce_events(events: list, pubkeys: dict, now: datetime = None) -> dict:
    """把一堆乱序事件归约成"这条街当前长什么样"。"""
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

    # 2) 按店铺分组
    groups = {}
    for ev in valid:
        groups.setdefault(ev["target"], []).append(ev)

    result = {}
    for shop_id, group in groups.items():
        result[shop_id] = _reduce_one(shop_id, group, now)
    return result


def _reduce_one(shop_id: str, group: list, now: datetime) -> dict:
    claims = [e for e in group if e["type"] == "claim"]
    claims.sort(key=lambda e: (e["ts"], e["id"]))
    owner = claims[0]["author"] if claims else None
    contested = [c["author"] for c in claims[1:]] if len(claims) > 1 else []

    # 店主的 deny：只能否认指向自己店铺的事件
    denied = set()
    for e in group:
        if e["type"] == "deny" and e["author"] == owner:
            denied.update(e["payload"].get("events", []))

    alive = [e for e in group if e["id"] not in denied]
    facts = [e for e in alive if e["type"] == "fact"]
    anchors = [e for e in alive if e["type"] == "anchor"]

    # anchor 只提供"店在这儿"的初始信息
    base_pool = list(anchors) + facts

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
            if field in TEMPORAL_FIELDS:
                ttl = DEFAULT_TTL_DAYS
                if (now - _parse_ts(e["ts"])) > timedelta(days=ttl):
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

        owner_c = [c for c in cands if c[0] == owner]
        if owner_c:
            # 店主说了算
            owner_c.sort(key=lambda c: (c[1], c[2], c[3]))
            state[field] = owner_c[-1][4]
        elif field in STRUCTURAL_FIELDS:
            # 店名/地址这类"事实"：路人只能投票建议，不能单方面推翻
            # 多数优先，平票取最早 —— 一个人改不掉别人已经标好的店名
            ranked = sorted(by_value.values(), key=lambda v: (-len(v["authors"]), v["first"]))
            state[field] = ranked[0]["value"]
        else:
            # "今天开着吗/有什么货"：最新一次观测最可信
            latest = sorted(cands, key=lambda c: (c[1], c[2], c[3]))[-1]
            state[field] = latest[4]

    # 3) 可信度分级
    authors = {e["author"] for e in base_pool}
    if owner:
        trust, reason = "L2", "店主已认领，修改由私钥签名"
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
        "events": len(group),
        "updated": max((e["ts"] for e in group), default=""),
    })
    return state


# ---------------------------------------------------------------- 本地库

class Store:
    """一台设备上的事件日志。append-only，永不删除。"""

    def __init__(self, path: Path = None, identity: Identity = None):
        self.path = Path(path) if path else None
        self.identity = identity
        self.events = {}      # id -> event
        self.pubkeys = {}     # device_id -> pubkey
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
        return reduce_events(list(self.events.values()), self.pubkeys, now)


def sync(a: Store, b: Store) -> tuple:
    """最简单的双向同步：互相补齐差集。顺序无关，跑几次结果都一样。"""
    n_ab = b.merge(a.diff(b.ids()))
    n_ba = a.merge(b.diff(a.ids()))
    return n_ab, n_ba
