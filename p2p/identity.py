#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门牌 · 身份与签名（纯标准库实现）

为什么要签名
------------
没有中心，就没法"颁发"可信度。唯一能离线验证的办法是密码学：
每条标注带作者签名，别人拿着他的公钥就能验，不需要问任何人。

为什么用 RSA 而不是手机号
------------------------
手机号只能证明"这个号是活的"，签名能证明"这条标注出自这台设备"。
再配合"认领"事件，就能证明"这家店由这把私钥说了算"——
这才是我们真正需要的东西，而且零成本、离线可用。

⚠️ 原型说明
    纯标准库没有 Ed25519，这里用教科书式 RSA（1024 位）+ SHA-256 做演示。
    正式版应换成 Ed25519（`cryptography` 库），体积小、速度快、无填充陷阱。
"""

import hashlib
import json
import secrets
import sys
from pathlib import Path

# 域分隔前缀：不同用途的签名不能互相冒用
DOMAIN = b"doorplate/v1|"
KEY_BITS = 1024


# ---------------------------------------------------------------- 数学基础

def _is_probable_prime(n: int, rounds: int = 8) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(cand):
            return cand


def canonical(obj) -> str:
    """稳定序列化：同样的内容永远得到同样的字节。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 身份

class Identity:
    """一台设备的密钥对。私钥只待在这台设备上，永不出门。"""

    def __init__(self, n: int, e: int, d: int):
        self.n, self.e, self.d = n, e, d

    # ---- 生命周期 ----
    @classmethod
    def generate(cls, bits: int = KEY_BITS) -> "Identity":
        while True:
            p, q = _gen_prime(bits // 2), _gen_prime(bits // 2)
            if p == q:
                continue
            phi = (p - 1) * (q - 1)
            e = 65537
            if phi % e == 0:
                continue
            try:
                d = pow(e, -1, phi)
            except ValueError:
                continue
            return cls(p * q, e, d)

    @classmethod
    def load_or_create(cls, path: Path, bits: int = KEY_BITS) -> "Identity":
        path = Path(path)
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return cls(obj["n"], obj["e"], obj["d"])
        ident = cls.generate(bits)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"n": ident.n, "e": ident.e, "d": ident.d}),
                        encoding="utf-8")
        return ident

    # ---- 标识 ----
    @property
    def pubkey(self) -> dict:
        return {"n": self.n, "e": self.e}

    @property
    def device_id(self) -> str:
        """设备指纹：公钥的短哈希，用作 author 字段。"""
        return sha256_hex(canonical(self.pubkey))[:12]

    # ---- 签名 ----
    def sign(self, message: str) -> str:
        digest = int(sha256_hex((DOMAIN + message.encode("utf-8")).hex()), 16) % self.n
        return format(pow(digest, self.d, self.n), "x")

    @staticmethod
    def verify(pubkey: dict, message: str, sig_hex: str) -> bool:
        try:
            n, e = int(pubkey["n"]), int(pubkey["e"])
            digest = int(sha256_hex((DOMAIN + message.encode("utf-8")).hex()), 16) % n
            return pow(int(sig_hex, 16), e, n) == digest
        except Exception:
            return False


# ---------------------------------------------------------------- 自检

def _selfcheck():
    sys.setrecursionlimit(10000)
    import time
    t0 = time.time()
    a = Identity.generate()
    b = Identity.generate()
    gen_ms = (time.time() - t0) * 1000
    ok = []
    ok.append(("生成两把密钥 < 3s", gen_ms < 3000, f"{gen_ms:.0f}ms"))
    ok.append(("设备指纹 12 位", len(a.device_id) == 12, a.device_id))
    ok.append(("设备指纹互不相同", a.device_id != b.device_id))
    msg = "好又多超市|解放路12号"
    sig = a.sign(msg)
    ok.append(("自己签自己验通过", Identity.verify(a.pubkey, msg, sig)))
    ok.append(("别人验也通过", Identity.verify(a.pubkey, msg, sig)))
    ok.append(("改一个字节就验不过", not Identity.verify(a.pubkey, msg + " ", sig)))
    ok.append(("用别人的公钥验不过", not Identity.verify(b.pubkey, msg, sig)))
    ok.append(("伪造签名验不过", not Identity.verify(a.pubkey, msg, "deadbeef")))
    for item in ok:
        name, passed = item[0], item[1]
        extra = item[2] if len(item) > 2 else ""
        print(f"  {'✓' if passed else '✗'} {name} {extra if not passed else ''}")
    return all(item[1] for item in ok)


if __name__ == "__main__":
    print("身份与签名自检：")
    raise SystemExit(0 if _selfcheck() else 1)
