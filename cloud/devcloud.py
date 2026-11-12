#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模拟"云存储 + 云函数"，用来在没有微信开发者工具的情况下验证上传链路。

它复刻了 getIndex.js / publishShop.js 的关键逻辑与权限规则：
  · 云存储：文件有属主，只有属主能覆盖
  · 数据库：索引记录带 _openid，别人不能改
  · 字段白名单：出现 price / rating / review 等字段直接拒绝
  · 索引与内容分离：数据库只存指针和摘要

跑一遍：
    python cloud/devcloud.py
"""

import hashlib
import json
from datetime import datetime

PRICE_TAGS = ["可议", "批发价", "零售价", "面谈"]
FORBIDDEN = {"price", "amount", "money", "fee", "rate", "score",
             "rating", "review", "comment", "star", "order", "pay"}
PUBLIC_FIELDS = ["name", "address", "phone", "hours", "lat", "lon",
                 "tags", "note", "open", "brief", "count", "hash", "fileID", "updated"]


class Cloud:
    def __init__(self):
        self.storage = {}   # path -> {"body": str, "owner": openid}
        self.db = {}        # id -> record(dict, 含 _openid)

    # ---------- 内部 ----------
    @staticmethod
    def _scan_forbidden(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in FORBIDDEN:
                    return f"{path}{k}"
                r = Cloud._scan_forbidden(v, f"{path}{k}.")
                if r:
                    return r
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                r = Cloud._scan_forbidden(v, f"{path}[{i}].")
                if r:
                    return r
        return ""

    @staticmethod
    def _s(v, n):
        return str(v if v is not None else "").strip()[:n]

    # ---------- 云函数 publishShop ----------
    def publish_shop(self, openid, shop):
        if not openid:
            return {"ok": False, "error": "no openid"}

        hit = self._scan_forbidden(shop)
        if hit:
            return {"ok": False,
                    "error": f'字段 "{hit}" 不被允许：本工具不记录价格与评价'}

        sid = self._s(shop.get("id") or openid.replace("-", "")[:16], 48)
        if not sid:
            return {"ok": False, "error": "bad id"}

        items = []
        for it in (shop.get("items") or [])[:60]:
            name = self._s(it.get("name"), 40)
            if not name:
                continue
            tag = it.get("tag") if it.get("tag") in PRICE_TAGS else "可议"
            items.append({"name": name, "unit": self._s(it.get("unit"), 10),
                          "note": self._s(it.get("note"), 60), "tag": tag})

        file = {
            "id": sid,
            "name": self._s(shop.get("name"), 30) or sid,
            "address": self._s(shop.get("address"), 60),
            "phone": self._s(shop.get("phone"), 20),
            "hours": self._s(shop.get("hours"), 30),
            "lat": float(shop.get("lat") or 0),
            "lon": float(shop.get("lon") or 0),
            "tags": [self._s(t, 10) for t in (shop.get("tags") or [])][:6],
            "note": self._s(shop.get("note"), 100),
            "open": bool(shop.get("open")),
            "items": items,
            "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "owner": openid[:8],
        }
        body = json.dumps(file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        h = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]

        path = f"catalog/{sid}.json"
        old = self.storage.get(path)
        if old and old["owner"] != openid:
            return {"ok": False, "error": "这家店的标识已被他人占用，请换一个"}

        self.storage[path] = {"body": body, "owner": openid}

        prev = self.db.get(sid)
        if prev and prev.get("_openid") and prev["_openid"] != openid:
            return {"ok": False, "error": "这家店属于别人"}

        self.db[sid] = {
            "_id": sid, "_openid": openid,
            "name": file["name"], "address": file["address"], "phone": file["phone"],
            "hours": file["hours"], "lat": file["lat"], "lon": file["lon"],
            "tags": file["tags"], "note": file["note"], "open": file["open"],
            "brief": "、".join(i["name"] for i in items[:4]),
            "count": len(items), "hash": h,
            "fileID": f"cloud://{path}", "updated": file["updated"],
        }
        return {"ok": True, "id": sid, "hash": h, "fileID": f"cloud://{path}"}

    # ---------- 云函数 getIndex ----------
    def get_index(self, since=""):
        rows = []
        for rec in self.db.values():
            if since and rec["updated"] <= since:
                continue
            row = {"id": rec["_id"]}
            for k in PUBLIC_FIELDS:
                if k in rec:
                    row[k] = rec[k]
            rows.append(row)
        rows.sort(key=lambda r: (not r.get("open"), r.get("updated", "")), reverse=False)
        rows.sort(key=lambda r: (0 if r.get("open") else 1, -len(r.get("updated", ""))))
        return {"ok": True, "count": len(rows), "shops": rows}

    # ---------- 客户端：按 fileID 下详情 ----------
    def download(self, file_id):
        path = file_id.replace("cloud://", "")
        obj = self.storage.get(path)
        if not obj:
            return {"ok": False, "error": "file not found"}
        data = json.loads(obj["body"])
        ok_hash = hashlib.sha1(obj["body"].encode("utf-8")).hexdigest()[:16] \
            == self.db[data["id"]]["hash"]
        return {"ok": True, "data": data, "hashMatch": ok_hash}


# ------------------------------------------------------------------ 自测

def run():
    c = Cloud()
    A, B, C = "openid-aaa", "openid-bbb", "openid-ccc"
    passed, failed = 0, 0

    def check(name, cond, extra=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  ✓ {name}")
        else:
            failed += 1
            print(f"  ✗ {name}  {extra}")

    print("\n[1] 店主A 上传店铺")
    r = c.publish_shop(A, {
        "id": "haoyouduo", "name": "好又多超市", "address": "解放路 12 号",
        "phone": "13800000001", "hours": "07:00-22:00", "lat": 29.8401, "lon": 112.9002,
        "tags": ["生鲜", "日杂"], "note": "熟客可以赊账", "open": True,
        "items": [{"name": "本地鸡蛋", "unit": "斤", "note": "今早到货", "tag": "可议"},
                  {"name": "大米", "unit": "袋", "note": "25kg", "tag": "批发价"}],
    })
    check("上传成功", r["ok"], r)
    check("拿到 fileID", str(r.get("fileID", "")).startswith("cloud://catalog/"))
    check("拿到内容哈希", len(r.get("hash", "")) == 16)

    print("[2] 店主B 想抢同一个店铺标识")
    r2 = c.publish_shop(B, {"id": "haoyouduo", "name": "冒名顶替", "items": []})
    check("被拒绝", not r2["ok"], r2)

    print("[3] 有人想塞价格字段")
    r3 = c.publish_shop(C, {"id": "sneaky", "name": "偷偷标价",
                            "items": [{"name": "鸡蛋", "price": 9.9}]})
    check("价格被拦下", not r3["ok"] and "price" in r3.get("error", ""), r3)

    print("[4] 有人想塞评价字段")
    r4 = c.publish_shop(C, {"id": "sneaky2", "name": "偷偷做评价", "rating": 5})
    check("评价被拦下", not r4["ok"] and "rating" in r4.get("error", ""), r4)

    print("[5] 店主A 更新自己的店（改货 + 关门）")
    r5 = c.publish_shop(A, {
        "id": "haoyouduo", "name": "好又多超市", "address": "解放路 12 号",
        "phone": "13800000001", "hours": "07:00-22:00", "lat": 29.8401, "lon": 112.9002,
        "tags": ["生鲜"], "open": False,
        "items": [{"name": "西红柿", "unit": "斤", "note": "沙瓤", "tag": "零售价"}],
    })
    check("本人可以更新", r5["ok"], r5)
    check("哈希变了", r5.get("hash") != r.get("hash"))

    print("[6] 顾客拉索引")
    idx = c.get_index()
    check("索引里有 1 家店", idx["count"] == 1, idx)
    shop = idx["shops"][0]
    check("索引带摘要 brief", shop.get("brief") == "西红柿", shop)
    check("索引不带完整货品", "items" not in shop, shop)
    check("索引不含价格字段", "price" not in shop)
    check("关门状态正确", shop.get("open") is False)

    print("[7] 顾客点开详情，按 fileID 下载")
    d = c.download(shop["fileID"])
    check("下载成功", d["ok"], d)
    check("哈希校验通过", d.get("hashMatch") is True)
    check("详情里有完整货品", len(d["data"]["items"]) == 1)

    print("[8] 增量同步：带上上次时间戳")
    idx2 = c.get_index(since=shop["updated"])
    check("没有新数据时返回空", idx2["count"] == 0, idx2)

    print(f"\n结果：{passed} 通过 / {failed} 失败\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
