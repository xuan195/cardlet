#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门牌 · 本地商圈信息板 —— 技术原型 v0.1

单文件服务端，纯标准库，零依赖。跑在任何有 Python 的机器上（旧安卓 Termux 亦可）。

设计红线（写在代码最前面，防止后续长歪）：
  1. 不承载交易：没有订单、没有支付、没有佣金、没有资金流水字段
  2. 不标价格：商品只有 名称/单位/备注/价签枚举，不存在任何数字价格字段
  3. 不做评价：没有评分、评论、点赞、信用分字段
  4. 数据即文件：data/ 下的 JSON 就是唯一真相源，任何人可整包拷走

因为不碰钱、不碰价、不碰评价，工具不构成任何意义上的"平台经营者"，
也就没有可以被滥用的权力位 —— 这是"0 抽成"的结构性保证，而非道德承诺。

用法：
    python server.py            # 默认 8000 端口
    python server.py 9000       # 自定义端口
"""

import json
import random
import socket
import string
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SHOPS = DATA / "shops"
RIDERS = DATA / "riders"
WEB = ROOT / "web"

# 价签枚举：只表达"什么价位的档"，绝不承载具体数字
PRICE_TAGS = ["可议", "批发价", "零售价", "面谈"]

# 公开投影时剔除的字段（令牌永不出网）
PRIVATE_FIELDS = ("token",)

_lock = threading.Lock()


# ----------------------------------------------------------------- 工具函数

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def gen_token() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))


def safe_id(raw: str) -> str:
    """只允许小写字母、数字、连字符，防止路径穿越。"""
    cleaned = "".join(c for c in raw.lower() if c.isalnum() or c == "-")
    return cleaned[:48]


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def public_shop(shop: dict) -> dict:
    """对外的店铺视图：没有令牌，没有价格数字，没有任何评价。"""
    out = {k: v for k, v in shop.items() if k not in PRIVATE_FIELDS}
    items = []
    for it in out.get("items", []):
        tag = it.get("tag") if it.get("tag") in PRICE_TAGS else "可议"
        items.append({
            "name": str(it.get("name", ""))[:40],
            "unit": str(it.get("unit", ""))[:10],
            "note": str(it.get("note", ""))[:60],
            "tag": tag,
        })
    out["items"] = items
    # 只暴露"有没有货"，不暴露价格
    out.pop("price", None)
    out["has_stock"] = bool(items)
    return out


def load_all(kind: str) -> list:
    folder = SHOPS if kind == "shops" else RIDERS
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(folder.glob("*.json")):
        obj = read_json(p)
        if isinstance(obj, dict):
            obj.setdefault("id", p.stem)
            out.append(obj)
    return out


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ----------------------------------------------------------------- 请求处理

class Handler(SimpleHTTPRequestHandler):
    server_version = "Doorplate/0.1"

    # ---------- 静态 ----------
    def translate_path(self, path):
        rel = urlparse(path).path.lstrip("/") or "index.html"
        if rel == "":
            rel = "index.html"
        return str(WEB / rel)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- 响应 ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/info":
            port = self.server.server_address[1]
            ip = lan_ip()
            shops = load_all("shops")
            riders = load_all("riders")
            return self.send_json({
                "name": "门牌",
                "version": "0.1",
                "lan_ip": ip,
                "port": port,
                "url": f"http://{ip}:{port}",
                "open_shops": sum(1 for s in shops if s.get("open")),
                "total_shops": len(shops),
                "total_riders": len(riders),
                "server_time": now_iso(),
            })

        if path == "/api/shops":
            shops = [public_shop(s) for s in load_all("shops")]
            shops.sort(key=lambda s: (not s.get("open"), s.get("name", "")))
            return self.send_json({"shops": shops, "count": len(shops)})

        if path.startswith("/api/shop/"):
            sid = safe_id(unquote(path[len("/api/shop/"):]))
            obj = read_json(SHOPS / f"{sid}.json")
            if obj is None:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json(public_shop(obj))

        if path == "/api/riders":
            riders = load_all("riders")
            riders.sort(key=lambda r: (not r.get("available_now"), r.get("name", "")))
            return self.send_json({"riders": riders, "count": len(riders)})

        # ---- 同步：列出本地文件指纹 ----
        if path == "/api/sync/manifest":
            files = {}
            for kind, folder in (("shops", SHOPS), ("riders", RIDERS)):
                folder.mkdir(parents=True, exist_ok=True)
                for p in folder.glob("*.json"):
                    obj = read_json(p, {})
                    files[f"{kind}/{p.name}"] = {
                        "updated": obj.get("updated", ""),
                        "size": p.stat().st_size,
                    }
            return self.send_json({"files": files, "generated": now_iso()})

        if path == "/api/sync/export":
            # 整包导出：把全部数据打包成一个 JSON，任何人可拷走
            payload = {
                "exported": now_iso(),
                "shops": {p.stem: read_json(p, {}) for p in SHOPS.glob("*.json")},
                "riders": {p.stem: read_json(p, {}) for p in RIDERS.glob("*.json")},
            }
            return self.send_json(payload)

        return super().do_GET()

    # ---------- POST ----------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        data = self.body_json()

        # ---- 营业开关：物理状态即网络状态（必须排在通用店铺接口之前） ----
        if path.startswith("/api/shop/") and path.endswith("/toggle"):
            sid = safe_id(unquote(path[len("/api/shop/"):-len("/toggle")]))
            with _lock:
                record = read_json(SHOPS / f"{sid}.json")
                if record is None:
                    return self.send_json({"error": "not found"}, 404)
                if record.get("token") and data.get("token") != record.get("token"):
                    return self.send_json({"error": "token mismatch"}, 403)
                record["open"] = not record.get("open", False)
                record["updated"] = now_iso()
                write_json(SHOPS / f"{sid}.json", record)
            return self.send_json({"ok": True, "open": record["open"]})

        # ---- 新建/更新店铺 ----
        if path.startswith("/api/shop/"):
            sid = safe_id(unquote(path[len("/api/shop/"):]))
            if not sid:
                return self.send_json({"error": "bad id"}, 400)
            with _lock:
                existing = read_json(SHOPS / f"{sid}.json")
                if existing and existing.get("token") and data.get("token") != existing.get("token"):
                    return self.send_json({"error": "token mismatch"}, 403)
                record = self._merge_shop(existing, data, sid)
                write_json(SHOPS / f"{sid}.json", record)
            return self.send_json({"ok": True, "shop": public_shop(record), "token": record["token"]})

        # ---- 骑手运力牌 ----
        if path.startswith("/api/rider/"):
            rid = safe_id(unquote(path[len("/api/rider/"):]))
            if not rid:
                return self.send_json({"error": "bad id"}, 400)
            with _lock:
                existing = read_json(RIDERS / f"{rid}.json")
                if existing and existing.get("token") and data.get("token") != existing.get("token"):
                    return self.send_json({"error": "token mismatch"}, 403)
                record = {
                    "id": rid,
                    "token": existing.get("token") if existing else gen_token(),
                    "name": str(data.get("name", ""))[:20],
                    "phone": str(data.get("phone", ""))[:20],
                    "vehicle": str(data.get("vehicle", ""))[:20],
                    "capacity": str(data.get("capacity", ""))[:30],
                    "areas": [str(a)[:20] for a in data.get("areas", [])][:6],
                    "window": str(data.get("window", ""))[:40],
                    "note": str(data.get("note", ""))[:80],
                    "available_now": bool(data.get("available_now", True)),
                    "updated": now_iso(),
                }
                write_json(RIDERS / f"{rid}.json", record)
            return self.send_json({"ok": True, "rider": record, "token": record["token"]})

        # ---- 同步：拉取对方新增/更新的文件 ----
        if path == "/api/sync/pull":
            have = data.get("have", {})
            changed = {}
            for kind, folder in (("shops", SHOPS), ("riders", RIDERS)):
                folder.mkdir(parents=True, exist_ok=True)
                for p in folder.glob("*.json"):
                    key = f"{kind}/{p.name}"
                    obj = read_json(p, {})
                    local_ts = obj.get("updated", "")
                    if have.get(key) != local_ts and local_ts:
                        changed[key] = obj
            return self.send_json({"files": changed, "count": len(changed)})

        # ---- 同步：接收对方推送（按 updated 时间戳后写覆盖） ----
        if path == "/api/sync/push":
            incoming = data.get("files", {})
            applied = 0
            with _lock:
                for key, obj in incoming.items():
                    if "/" not in key:
                        continue
                    kind, fname = key.split("/", 1)
                    if kind == "shops":
                        folder = SHOPS
                    elif kind == "riders":
                        folder = RIDERS
                    else:
                        continue
                    fname = Path(fname).name
                    if not fname.endswith(".json"):
                        continue
                    target = folder / fname
                    current = read_json(target, {})
                    if obj.get("updated", "") > current.get("updated", ""):
                        write_json(target, obj)
                        applied += 1
            return self.send_json({"ok": True, "applied": applied})

        return self.send_json({"error": "unknown endpoint"}, 404)

    @staticmethod
    def _merge_shop(existing, data, sid) -> dict:
        base = existing or {}
        token = base.get("token") or gen_token()
        items = []
        for it in (data.get("items") or [])[:60]:
            name = str(it.get("name", "")).strip()[:40]
            if not name:
                continue
            tag = it.get("tag") if it.get("tag") in PRICE_TAGS else "可议"
            items.append({
                "name": name,
                "unit": str(it.get("unit", "")).strip()[:10],
                "note": str(it.get("note", "")).strip()[:60],
                "tag": tag,
            })
        try:
            lat = round(float(data.get("lat", base.get("lat", 0))), 6)
            lon = round(float(data.get("lon", base.get("lon", 0))), 6)
        except (TypeError, ValueError):
            lat, lon = base.get("lat", 0), base.get("lon", 0)
        return {
            "id": sid,
            "token": token,
            "name": str(data.get("name", base.get("name", ""))).strip()[:30] or sid,
            "address": str(data.get("address", base.get("address", ""))).strip()[:60],
            "phone": str(data.get("phone", base.get("phone", ""))).strip()[:20],
            "hours": str(data.get("hours", base.get("hours", ""))).strip()[:30],
            "lat": lat,
            "lon": lon,
            "tags": [str(t)[:10] for t in (data.get("tags") or base.get("tags") or [])][:6],
            "note": str(data.get("note", base.get("note", ""))).strip()[:100],
            "open": bool(data.get("open", base.get("open", True))),
            "items": items,
            "updated": now_iso(),
        }


# ----------------------------------------------------------------- 入口

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    for folder in (SHOPS, RIDERS, WEB):
        folder.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    ip = lan_ip()
    print("=" * 52)
    print("  门牌 · 本地商圈信息板  v0.1")
    print("=" * 52)
    print(f"  本机访问   http://127.0.0.1:{port}")
    print(f"  手机访问   http://{ip}:{port}   (同一 WiFi)")
    print(f"  顾客入口   http://{ip}:{port}/index.html")
    print(f"  店主入口   http://{ip}:{port}/shop.html")
    print(f"  电子门牌   http://{ip}:{port}/board.html?id=<店铺id>")
    print(f"  骑手入口   http://{ip}:{port}/rider.html")
    print("=" * 52)
    print("  Ctrl+C 停止。数据在 data/ 目录，拷走即备份。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
