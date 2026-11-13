/* 门牌 · 引擎（浏览器 / Node 共用）
 *
 * 与 Python 版 p2p/ 完全同构：
 *   · 同样的 canonical 序列化
 *   · 同样的 RSA 签名方案（sha256 → 整数 → 模幂，无填充）
 *   · 同样的事件结构、归约规则、担保链
 * 因此 Python 签的事件，JS 能验；JS 签的事件，Python 也能验。
 *
 * 全同步实现（自带 SHA-256 与模幂），不依赖 WebCrypto，浏览器和 Node 行为一致。
 */
(function (global) {
  "use strict";

  const DOMAIN = "doorplate/v1|";
  const KEY_BITS = 1024;
  const TEMPORAL_FIELDS = new Set(["open", "items", "note"]);
  const STRUCTURAL_FIELDS = new Set(["kind", "name", "address", "phone", "hours",
    "lat", "lon", "tags", "vehicle", "capacity", "areas", "window"]);
  const DEFAULT_TTL_DAYS = 7;
  const ANCHOR_TTL_DAYS = 30;
  const TRUST_L1_AUTHORS = 3;
  const SPONSOR_NEEDED = 2;

  // ---------------------------------------------------------------- SHA-256
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ]);

  function sha256Bytes(bytes) {
    const H = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    const l = bytes.length;
    // 补到「最小的、≥ l+9 的 64 倍数」——当天 (l+9) 正好是 64 的倍数时不能多补一块
    const withPad = new Uint8Array(Math.ceil((l + 9) / 64) * 64);
    withPad.set(bytes);
    withPad[l] = 0x80;
    const bitLen = BigInt(l) * 8n;
    for (let i = 0; i < 8; i++) {
      withPad[withPad.length - 1 - i] = Number((bitLen >> BigInt(8 * i)) & 0xffn);
    }
    const w = new Uint32Array(64);
    for (let off = 0; off < withPad.length; off += 64) {
      for (let i = 0; i < 16; i++) {
        w[i] = (withPad[off + i * 4] << 24) | (withPad[off + i * 4 + 1] << 16) |
          (withPad[off + i * 4 + 2] << 8) | withPad[off + i * 4 + 3];
      }
      for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let [a, b, c, d, e, f, g, h] = H;
      for (let i = 0; i < 64; i++) {
        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const ch = (e & f) ^ (~e & g);
        const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0;
        d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
    }
    const out = new Uint8Array(32);
    for (let i = 0; i < 8; i++) {
      out[i * 4] = H[i] >>> 24; out[i * 4 + 1] = (H[i] >>> 16) & 0xff;
      out[i * 4 + 2] = (H[i] >>> 8) & 0xff; out[i * 4 + 3] = H[i] & 0xff;
    }
    return out;
  }

  function rotr(x, n) { return ((x >>> n) | (x << (32 - n))) >>> 0; }

  function enc(str) { return new TextEncoder().encode(str); }
  function toHex(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0");
    return s;
  }
  function sha256Hex(str) { return toHex(sha256Bytes(enc(str))); }

  // ---------------------------------------------------------------- 序列化
  /** 与 Python json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False) 对齐 */
  function canonical(v) {
    if (v === null) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") {
      if (!isFinite(v)) return "null";
      if (Number.isInteger(v)) return String(v);
      return String(v);
    }
    if (typeof v === "string") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    if (typeof v === "object") {
      const keys = Object.keys(v).filter(k => v[k] !== undefined).sort();
      return "{" + keys.map(k => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
    }
    return "null";
  }

  // ---------------------------------------------------------------- 大整数
  function modPow(base, exp, mod) {
    let r = 1n, b = base % mod, e = exp;
    while (e > 0n) {
      if (e & 1n) r = (r * b) % mod;
      b = (b * b) % mod;
      e >>= 1n;
    }
    return r;
  }

  function randBig(bytes) {
    const buf = new Uint8Array(bytes);
    (global.crypto || global.msCrypto).getRandomValues(buf);
    let n = 0n;
    for (const b of buf) n = (n << 8n) | BigInt(b);
    return n;
  }

  const SMALL_PRIMES = [2n, 3n, 5n, 7n, 11n, 13n, 17n, 19n, 23n, 29n, 31n, 37n];

  function isProbablePrime(n, rounds) {
    if (n < 2n) return false;
    for (const p of SMALL_PRIMES) {
      if (n % p === 0n) return n === p;
    }
    let d = n - 1n, s = 0;
    while (d % 2n === 0n) { d /= 2n; s++; }
    for (let i = 0; i < rounds; i++) {
      const a = randBig(16) % (n - 3n) + 2n;
      let x = modPow(a, d, n);
      if (x === 1n || x === n - 1n) continue;
      let ok = false;
      for (let j = 0; j < s - 1; j++) {
        x = (x * x) % n;
        if (x === n - 1n) { ok = true; break; }
      }
      if (!ok) return false;
    }
    return true;
  }

  function genPrime(bits) {
    const bytes = Math.ceil(bits / 8);
    for (;;) {
      const cand = randBig(bytes) | (1n << BigInt(bits - 1)) | 1n;
      if (isProbablePrime(cand, 8)) return cand;
    }
  }

  function hexToBig(h) { return BigInt("0x" + h); }

  // ---------------------------------------------------------------- 身份
  class Identity {
    constructor(n, e, d) { this.n = n; this.e = e; this.d = d; }

    static generate(bits = KEY_BITS) {
      const e = 65537n;
      for (;;) {
        const p = genPrime(bits / 2), q = genPrime(bits / 2);
        if (p === q) continue;
        const phi = (p - 1n) * (q - 1n);
        if (phi % e === 0n) continue;
        const d = modInv(e, phi);
        if (d) return new Identity(p * q, e, d);
      }
    }

    static fromJSON(o) {
      return new Identity(BigInt(o.n), BigInt(o.e), BigInt(o.d));
    }

    toJSON() { return { n: this.n.toString(), e: this.e.toString(), d: this.d.toString() }; }

    get pubkey() { return { n: Number(this.n), e: Number(this.e) }; }

    /** 公钥用字符串存，避免超过 2^53 精度问题 */
    get pubkeyStr() { return { n: this.n.toString(), e: this.e.toString() }; }

    get deviceId() { return deviceIdOf(this.pubkeyStr); }

    sign(message) {
      const digest = hexToBig(sha256Hex(DOMAIN + message)) % this.n;
      return modPow(digest, this.d, this.n).toString(16);
    }

    static verify(pub, message, sigHex) {
      try {
        const n = BigInt(pub.n), e = BigInt(pub.e);
        const digest = hexToBig(sha256Hex(DOMAIN + message)) % n;
        return modPow(hexToBig(sigHex), e, n) === digest;
      } catch (e) { return false; }
    }
  }

  function modInv(a, m) {
    let [old_r, r] = [a % m, m];
    let [old_s, s] = [1n, 0n];
    while (r !== 0n) {
      const q = old_r / r;
      [old_r, r] = [r, old_r - q * r];
      [old_s, s] = [s, old_s - q * s];
    }
    if (old_r !== 1n) return null;
    return ((old_s % m) + m) % m;
  }

  function deviceIdOf(pub) {
    try {
      return sha256Hex(canonical({ n: BigInt(pub.n).toString(), e: BigInt(pub.e).toString() })).slice(0, 12);
    } catch (e) { return ""; }
  }

  // ---------------------------------------------------------------- 事件
  function eventBody(kind, target, author, lamport, ts, payload) {
    return { type: kind, target, author, lamport: Number(lamport), ts, payload: payload || {} };
  }

  function makeEvent(identity, kind, target, payload, lamport, ts) {
    ts = ts || nowTs();
    const body = eventBody(kind, target, identity.deviceId, lamport, ts, payload);
    const message = canonical(body);
    body.id = sha256Hex(message).slice(0, 16);
    body.sig = identity.sign(message);
    return body;
  }

  function verifyEvent(ev, pubkeys) {
    try {
      const body = {
        type: ev.type, target: ev.target, author: ev.author,
        lamport: Number(ev.lamport), ts: ev.ts, payload: ev.payload || {}
      };
      if (ev.id !== sha256Hex(canonical(body)).slice(0, 16)) return false;
      const pub = pubkeys[ev.author];
      if (!pub) return false;
      return Identity.verify(pub, canonical(body), ev.sig);
    } catch (e) { return false; }
  }

  // ---------------------------------------------------------------- 担保
  function sponsorMessage(shopId, claimer) {
    return canonical({ kind: "sponsor", shop: shopId, claimer });
  }

  function sponsorSign(sponsor, shopId, claimerId) {
    return { by: sponsor.deviceId, pub: sponsor.pubkeyStr,
             sig: sponsor.sign(sponsorMessage(shopId, claimerId)) };
  }

  function resolveClaims(events, pubkeys, rootId) {
    const claims = events.filter(e => e.type === "claim");
    if (!claims.length) return {};

    // 背书事件（endorse）：任何人可以独立给某人的认领作保，
    // 不用等对方重发认领 —— 这正是"一键作保"能成立的原因。
    const endorseMap = {};
    for (const e of events) {
      if (e.type !== "endorse") continue;
      const p = e.payload || {};
      if (!p.shop || !p.claimer) continue;
      const pub = pubkeys[e.author] || e.pubkey;
      if (!pub || !Identity.verify(pub, sponsorMessage(p.shop, p.claimer), p.sig || "")) continue;
      const k = p.shop + "|" + p.claimer;
      (endorseMap[k] = endorseMap[k] || new Set()).add(e.author);
    }

    const sponsors = {};
    for (const c of claims) {
      const msg = sponsorMessage(c.target, c.author);
      const got = new Set();
      for (const sp of (c.payload.sponsors || [])) {
        const pub = sp.pub || pubkeys[sp.by];
        if (!pub || deviceIdOf(pub) !== sp.by) continue;
        if (!Identity.verify(pub, msg, sp.sig || "")) continue;
        pubkeys[sp.by] = pub;
        got.add(sp.by);
      }
      for (const by of (endorseMap[c.target + "|" + c.author] || [])) got.add(by);
      sponsors[c.id] = got;
    }

    const trusted = new Set(rootId ? [rootId] : []);
    const valid = new Set();
    const bootstrap = !rootId;
    if (bootstrap) {
      const first = [...claims].sort((a, b) => cmp(a, b))[0];
      valid.add(first.id);
      trusted.add(first.author);
    }
    for (let k = 0; k < 10; k++) {
      let changed = false;
      for (const c of claims) {
        if (valid.has(c.id)) continue;
        const sp = sponsors[c.id];
        const ok = trusted.has(c.author)                       // 已受信的店主开新店，免担保
          || (rootId && sp.has(rootId))                        // 发起人作保，一位即可
          || (bootstrap
              ? sp.size >= SPONSOR_NEEDED                      // 无根部署：任意 2 台设备作保
              : [...sp].filter(x => trusted.has(x)).length >= SPONSOR_NEEDED);
        if (ok) { valid.add(c.id); trusted.add(c.author); changed = true; }
      }
      if (!changed) break;
    }

    const byShop = {};
    for (const c of claims) (byShop[c.target] = byShop[c.target] || []).push(c);
    const out = {};
    for (const [shop, cs] of Object.entries(byShop)) {
      const good = cs.filter(c => valid.has(c.id)).sort((a, b) => cmp(a, b));
      const primary = good[0] || cs.sort((a, b) => cmp(a, b))[0];
      const sponsorList = primary ? [...(sponsors[primary.id] || [])].sort() : [];
      out[shop] = {
        owner: good.length ? good[0].author : null,
        valid: new Set(good.map(c => c.id)),
        pending: cs.filter(c => !valid.has(c.id)).map(c => c.author),
        contested: good.slice(1).map(c => c.author),
        sponsor_ids: sponsorList,
      };
    }
    return out;
  }

  function cmp(a, b) {
    if (a.ts !== b.ts) return a.ts < b.ts ? -1 : 1;
    return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
  }

  // ---------------------------------------------------------------- 归约
  function parseTs(ts) {
    const d = new Date(ts);
    return isNaN(d.getTime()) ? new Date(0) : d;
  }

  function reduceEvents(events, pubkeys, now, rootId) {
    now = now || new Date();
    const valid = [], seen = new Set();
    for (const ev of events) {
      if (seen.has(ev.id)) continue;
      if (!verifyEvent(ev, pubkeys)) continue;
      seen.add(ev.id);
      valid.push(ev);
    }
    const rcAll = resolveClaims(valid, pubkeys, rootId);
    const groups = {};
    for (const ev of valid) (groups[ev.target] = groups[ev.target] || []).push(ev);

    const out = {};
    for (const [shop, group] of Object.entries(groups)) {
      // 只有认领、没有 anchor 的孤儿不算一家店
      if (!group.some(e => e.type === "anchor")) continue;
      out[shop] = reduceOne(shop, group, now, rcAll[shop] || {});
    }
    return out;
  }

  function reduceOne(shopId, group, now, rc) {
    const owner = rc.owner || null;
    const contested = rc.contested || [];
    const pending = rc.pending || [];

    const denied = new Set();
    for (const e of group) {
      if (e.type === "deny" && e.author === owner) {
        (e.payload.events || []).forEach(x => denied.add(x));
      }
    }
    const alive = group.filter(e => !denied.has(e.id));
    const facts = alive.filter(e => e.type === "fact");
    const anchors = alive.filter(e => e.type === "anchor");
    const basePool = anchors.concat(facts);

    const state = { id: shopId };
    const conflicts = {};
    const allFields = new Set([...STRUCTURAL_FIELDS, ...TEMPORAL_FIELDS]);

    for (const field of allFields) {
      const cands = [];
      for (const e of basePool) {
        const p = (e.payload && typeof e.payload === "object") ? e.payload : {};
        const fields = (p.fields && typeof p.fields === "object") ? p.fields : p;
        if (!(field in fields)) continue;
        if (TEMPORAL_FIELDS.has(field) &&
          (now - parseTs(e.ts)) > DEFAULT_TTL_DAYS * 86400000) continue;
        cands.push({ author: e.author, ts: e.ts, lamport: Number(e.lamport), id: e.id, val: fields[field] });
      }
      if (!cands.length) continue;

      const byValue = {};
      for (const c of cands) {
        const key = canonical(c.val);
        const slot = byValue[key] || (byValue[key] = { authors: new Set(), first: c.ts, value: c.val });
        slot.authors.add(c.author);
        if (c.ts < slot.first) slot.first = c.ts;
      }
      if (Object.keys(byValue).length > 1) {
        conflicts[field] = {};
        for (const [k, v] of Object.entries(byValue)) conflicts[field][k] = v.authors.size;
      }

      const ownerC = cands.filter(c => c.author === owner).sort(byCand);
      if (ownerC.length) {
        state[field] = ownerC[ownerC.length - 1].val;
      } else if (STRUCTURAL_FIELDS.has(field)) {
        const ranked = Object.values(byValue)
          .sort((a, b) => (b.authors.size - a.authors.size) || (a.first < b.first ? -1 : 1));
        state[field] = ranked[0].value;
      } else {
        state[field] = [...cands].sort(byCand).pop().val;
      }
    }

    const authors = new Set(basePool.map(e => e.author));
    let trust, reason;
    if (owner) { trust = "L2"; reason = "店主已认领并有担保人作保"; }
    else if (pending.length) {
      trust = "L0";
      reason = pending.length + " 条认领待担保（需 " + SPONSOR_NEEDED + " 位已认领店主作保）";
      state.claim_pending = pending;
    } else if (authors.size >= TRUST_L1_AUTHORS && !Object.keys(conflicts).length) {
      trust = "L1"; reason = authors.size + " 台设备各自标注且一致";
    } else if (anchors.length && (now - parseTs(anchors[0].ts)) > ANCHOR_TTL_DAYS * 86400000) {
      trust = "L0"; reason = "路人标注超过 30 天无人认领，已淡出"; state.faded = true;
    } else { trust = "L0"; reason = "仅路人标注，未经店主确认"; }

    state.trust = trust;
    state.trust_reason = reason;
    state.owner = owner;
    state.contested = contested;
    state.conflicts = conflicts;
    state.sponsors = rc.sponsor_ids || [];      // 谁作过保，用来显示"xxx、xxx 已担保"
    state.events = group.length;
    state.updated = group.reduce((m, e) => (e.ts > m ? e.ts : m), "");
    return state;
  }

  function byCand(a, b) {
    if (a.ts !== b.ts) return a.ts < b.ts ? -1 : 1;
    if (a.lamport !== b.lamport) return a.lamport - b.lamport;
    return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
  }

  // ---------------------------------------------------------------- 本地库
  class Store {
    constructor(identity, rootId, storageKey) {
      this.identity = identity;
      this.rootId = rootId || null;
      this.events = new Map();
      this.pubkeys = {};
      this.lamport = 0;
      this.storageKey = storageKey || null;
      if (this.storageKey) this.load();
    }

    load() {
      try {
        const raw = global.localStorage && localStorage.getItem(this.storageKey);
        if (!raw) return;
        const data = JSON.parse(raw);
        for (const ev of data.events || []) this.events.set(ev.id, ev);
        this.pubkeys = data.pubkeys || {};
        this.lamport = data.lamport || 0;
      } catch (e) { /* 忽略损坏数据 */ }
    }

    save() {
      if (!this.storageKey || !global.localStorage) return;
      const data = { events: [...this.events.values()], pubkeys: this.pubkeys, lamport: this.lamport };
      try { localStorage.setItem(this.storageKey, JSON.stringify(data)); } catch (e) {}
    }

    add(kind, target, payload, ts) {
      this.lamport += 1;
      const ev = makeEvent(this.identity, kind, target, payload, this.lamport, ts);
      ev.pubkey = this.identity.pubkeyStr;
      this.events.set(ev.id, ev);
      this.pubkeys[this.identity.deviceId] = this.identity.pubkeyStr;
      this.save();
      return ev;
    }

    merge(events) {
      let added = 0;
      for (const ev of events) {
        if (this.events.has(ev.id)) continue;
        const pub = ev.pubkey || this.pubkeys[ev.author];
        if (!pub || !verifyEvent(ev, { [ev.author]: pub })) continue;
        this.events.set(ev.id, ev);
        this.pubkeys[ev.author] = pub;
        this.lamport = Math.max(this.lamport, Number(ev.lamport) || 0);
        added++;
      }
      if (added) this.save();
      return added;
    }

    ids() { return new Set(this.events.keys()); }

    diff(otherIds) { return [...this.events.values()].filter(e => !otherIds.has(e.id)); }

    state(now) { return reduceEvents([...this.events.values()], this.pubkeys, now, this.rootId); }

    sponsor(shopId, claimerId) { return sponsorSign(this.identity, shopId, claimerId); }
  }

  function nowTs(d) {
    d = d || new Date();
    const p = n => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" +
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  const DP = {
    DOMAIN, DEFAULT_TTL_DAYS, ANCHOR_TTL_DAYS, TRUST_L1_AUTHORS, SPONSOR_NEEDED,
    canonical, sha256Hex, modPow, modInv,
    Identity, deviceIdOf,
    makeEvent, verifyEvent, sponsorMessage, sponsorSign, resolveClaims, reduceEvents,
    Store, nowTs, cmp, byCand,
    TEMPORAL_FIELDS, STRUCTURAL_FIELDS
  };

  global.DP = DP;
  if (typeof module !== "undefined" && module.exports) module.exports = DP;
})(typeof globalThis !== "undefined" ? globalThis : this);
