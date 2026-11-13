#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Python 与 JS 引擎的互验脚本

    python app/interop.py gen out.json        生成一条 Python 签名的标注
    python app/interop.py verify in.json      验一条 JS 签名的标注
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p2p"))

from identity import Identity
from store import make_event, verify_event


def gen(path: Path):
    ident = Identity.generate()
    ev = make_event(ident, "anchor", "haoyouduo", {
        "fields": {"name": "好又多超市", "address": "解放路 12 号",
                   "lat": 29.8401, "lon": 112.9002}},
        1, "2026-11-13T10:00:00")
    ev["pubkey"] = ident.pubkey
    path.write_text(json.dumps({"event": ev, "device_id": ident.device_id},
                               ensure_ascii=False), encoding="utf-8")
    print(f"OK generated device_id={ident.device_id}")


def verify(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    ev = data["event"]
    pub = ev.get("pubkey")
    if not pub:
        print("FAIL missing pubkey")
        return 1
    ok = verify_event(ev, {ev["author"]: pub})
    print(("OK" if ok else "FAIL") + f" verified id={ev['id']} author={ev['author']}")
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "gen"
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_interop.json")
    sys.exit(gen(target) if cmd == "gen" else verify(target))
