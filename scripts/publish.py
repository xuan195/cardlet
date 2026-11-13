#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名片 · 发布器：把本地数据编译成一套"可上传的文件"

设计要点
--------
1. 产物是**纯文件**，不是数据库记录。丢到任何静态空间都能用：
   对象存储 / GitHub Pages / 微信云开发静态托管 / U 盘 / 局域网 NFS。
2. 公开文件里**绝不含 token**，只保留本来就该公开的字段。
3. 每个店铺文件带内容哈希，客户端靠哈希判断"要不要重新下载"，
   省流量、离线可缓存。
4. index.json 是"电话簿"，只放店名/坐标/营业状态/货品摘要，
   不含任何评价、价格；即使电话簿泄露，也没有生意秘密。

产物结构
--------
publish/
├── index.json            全城索引（客户端只拉这一个就能显示整条街）
├── catalog/
│   └── <shopId>.json     单店完整目录（点开才拉）
└── meta.json             版本与生成时间（客户端用来判断要不要刷新）

用法
----
    python scripts/publish.py                  # 生成到 publish/
    python scripts/publish.py --out D:/share   # 生成到指定目录
    python scripts/publish.py --zip            # 额外打一个 zip 便于手动传
"""

import argparse
import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_OUT = ROOT / "publish"

# 公开字段白名单：不在白名单里的一律不出去（token 因此天然被挡掉）
SHOP_PUBLIC = ("id", "name", "address", "phone", "hours", "lat", "lon",
               "tags", "note", "open", "items", "updated")
RIDER_PUBLIC = ("id", "name", "phone", "vehicle", "capacity", "areas",
                "window", "note", "available_now", "updated")


def canonical(obj) -> str:
    """稳定序列化：同样的数据永远得到同样的哈希。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj) -> str:
    return hashlib.sha1(canonical(obj).encode("utf-8")).hexdigest()[:16]


def load(folder: Path) -> list:
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            obj.setdefault("id", p.stem)
            out.append(obj)
        except Exception as e:
            print(f"[warn] 跳过损坏文件 {p.name}: {e}")
    return out


def project(obj, fields) -> dict:
    return {k: obj.get(k) for k in fields if k in obj}


def build(out: Path) -> dict:
    shops_raw = load(DATA / "shops")
    riders_raw = load(DATA / "riders")

    catalog_dir = out / "catalog"
    if catalog_dir.exists():
        shutil.rmtree(catalog_dir)
    catalog_dir.mkdir(parents=True, exist_ok=True)

    index_shops = []
    for s in shops_raw:
        pub = project(s, SHOP_PUBLIC)
        pub.pop("items", None)          # 完整货品只进 catalog，不进电话簿
        h = digest(project(s, SHOP_PUBLIC))
        pub["hash"] = h
        pub["path"] = f"catalog/{s['id']}.json"
        # 摘要：让列表页不下载详情也能显示"卖什么"
        items = project(s, SHOP_PUBLIC).get("items") or []
        pub["brief"] = "、".join(i.get("name", "") for i in items[:4] if i.get("name"))
        pub["count"] = len(items)
        index_shops.append(pub)

        detail = project(s, SHOP_PUBLIC)
        detail["hash"] = h
        (catalog_dir / f"{s['id']}.json").write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")

    index_riders = []
    for r in riders_raw:
        pub = project(r, RIDER_PUBLIC)
        pub["hash"] = digest(pub)
        index_riders.append(pub)

    index_shops.sort(key=lambda x: (not x.get("open"), x.get("name") or ""))
    meta = {
        "name": "名片",
        "format": 1,
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "shops": len(index_shops),
        "riders": len(index_riders),
        "note": "纯静态文件，无账号、无交易、无评价、无价格。整包可拷走。",
    }
    index = {"meta": meta, "shops": index_shops, "riders": index_riders}

    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return {"meta": meta, "index": index}


def make_zip(out: Path) -> Path:
    zpath = out.parent / (out.name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out))
    return zpath


def main():
    ap = argparse.ArgumentParser(description="把名片数据编译成可上传的静态文件")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--zip", action="store_true", help="额外打包成 zip")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = build(out)

    # 隐私自检：确认公开产物里没有 token
    leaked = []
    for p in out.rglob("*.json"):
        if '"token"' in p.read_text(encoding="utf-8"):
            leaked.append(p.name)
    if leaked:
        print(f"[严重] 公开文件里发现 token：{leaked}")
        return 1

    m = result["meta"]
    print(f"OK  店铺 {m['shops']} 家 · 骑手 {m['riders']} 位 · 生成于 {m['generated']}")
    print(f"    {out / 'index.json'}    <- 电话簿，客户端拉这个就够")
    print(f"    {out / 'catalog'}       <- 单店目录，点开才拉")
    print(f"    {out / 'meta.json'}     <- 版本号，用来判断要不要刷新")
    if args.zip:
        z = make_zip(out)
        size = z.stat().st_size
        print(f"    {z}  ({size} 字节，可直接微信发送/拷 U 盘)")
    print("    隐私自检通过：公开文件不含任何 token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
