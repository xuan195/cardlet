/* 门牌 · JS 引擎自检（Node 运行）
 *
 *   node app/selftest.cjs
 *
 * 重点验证与 Python 端的互验：
 *   Python 签的事件，JS 必须能验；JS 签的事件，Python 必须能验。
 * 对不上，说明 canonical / 指纹 / 签名方案有分歧，必须修到对为止。
 */
const DP = require("./core.js");
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const crypto = require("crypto");

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; console.log("  ✗ " + name + (extra ? "   " + extra : "")); }
}

const TMP = path.join(__dirname, "_interop");
fs.mkdirSync(TMP, { recursive: true });

console.log("\n[1] 基础件：SHA-256 / canonical");
const samples = ["", "abc", "好又多超市", "a".repeat(1000), "😀中文混排"];
let shaOk = true;
for (const s of samples) {
  const mine = DP.sha256Hex(s);
  const ref = crypto.createHash("sha256").update(s, "utf8").digest("hex");
  if (mine !== ref) { shaOk = false; console.log("    不一致:", JSON.stringify(s).slice(0, 30)); }
}
check("SHA-256 与 Node 原生一致（含中文/长文本/emoji）", shaOk);

check("canonical 排序键",
  DP.canonical({ b: 1, a: 2 }) === '{"a":2,"b":1}',
  DP.canonical({ b: 1, a: 2 }));
check("canonical 保留中文不转义",
  DP.canonical({ name: "好又多" }) === '{"name":"好又多"}',
  DP.canonical({ name: "好又多" }));

console.log("[2] 身份与签名");
const t0 = Date.now();
const alice = DP.Identity.generate();
const bob = DP.Identity.generate();
const genMs = Date.now() - t0;
check("生成两把密钥 < 5s", genMs < 5000, genMs + "ms");
check("设备指纹 12 位", alice.deviceId.length === 12, alice.deviceId);
check("两把钥匙指纹不同", alice.deviceId !== bob.deviceId);

const msg = "好又多超市|解放路12号";
const sig = alice.sign(msg);
check("自己签自己验", DP.Identity.verify(alice.pubkeyStr, msg, sig));
check("别人也能验", DP.Identity.verify(alice.pubkeyStr, msg, sig));
check("改一个字符就失败", !DP.Identity.verify(alice.pubkeyStr, msg + " ", sig));
check("用别人的公钥失败", !DP.Identity.verify(bob.pubkeyStr, msg, sig));

console.log("[3] 事件与归约");
const A = new DP.Store(alice, null);
const ev1 = A.add("anchor", "haoyouduo",
  { fields: { name: "好又多超市", address: "解放路 12 号", lat: 29.8401, lon: 112.9002 } },
  "2026-11-13T09:00:00");
check("事件有 id 与签名", !!ev1.id && !!ev1.sig);
check("事件验签通过", DP.verifyEvent(ev1, { [alice.deviceId]: ev1.pubkey }));

const A2 = new DP.Store(bob, null);
A2.merge([ev1]);
check("另一台设备能验收入库", A2.events.has(ev1.id));

const st = A.state(new Date("2026-11-13T12:00:00"));
check("店铺被归约出来", !!st.haoyouduo);
check("等级 L0（仅路人）", st.haoyouduo.trust === "L0", st.haoyouduo.trust);
check("店名正确", st.haoyouduo.name === "好又多超市", st.haoyouduo.name);

console.log("[4] ★ 与 Python 互验");
const pyGen = path.join(TMP, "py_event.json");
const jsGen = path.join(TMP, "js_event.json");
let pyOk = true;
try {
  execFileSync("python", [path.join(__dirname, "interop.py"), "gen", pyGen], { stdio: "pipe" });
} catch (e) {
  pyOk = false;
  console.log("    Python 生成失败:", String(e.stderr || e.message).slice(0, 200));
}
check("Python 端生成事件", pyOk && fs.existsSync(pyGen));

let jsVerifyPy = false, sameFingerprint = false;
if (pyOk && fs.existsSync(pyGen)) {
  const pyData = JSON.parse(fs.readFileSync(pyGen, "utf8"));
  const pyEvent = pyData.event;
  jsVerifyPy = DP.verifyEvent(pyEvent, { [pyEvent.author]: pyEvent.pubkey });
  sameFingerprint = DP.deviceIdOf(pyEvent.pubkey) === pyEvent.author;
}
check("★ JS 能验 Python 签的事件", jsVerifyPy);
check("★ 设备指纹两端算出同一个值", sameFingerprint);

// 反向：JS 签，Python 验
const jsEvent = new DP.Store(alice, null).add("anchor", "laochen",
  { fields: { name: "老陈五金", address: "解放路 28 号" } }, "2026-11-13T09:30:00");
fs.writeFileSync(jsGen, JSON.stringify({ event: jsEvent, device_id: alice.deviceId }), "utf8");
let pyVerifyJs = false;
try {
  const out = execFileSync("python", [path.join(__dirname, "interop.py"), "verify", jsGen],
    { stdio: "pipe" }).toString();
  pyVerifyJs = out.includes("OK verified");
  if (!pyVerifyJs) console.log("    Python 输出:", out.trim());
} catch (e) {
  console.log("    Python 验签失败:", String(e.stdout || e.stderr || e.message).slice(0, 200));
}
check("★ Python 能验 JS 签的事件", pyVerifyJs);

console.log("[5] 担保链（跨店）");
const root = DP.Identity.generate();
const ownerA = DP.Identity.generate();
const ownerB = DP.Identity.generate();
const faker = DP.Identity.generate();

const shopB = new DP.Store(ownerB, root.deviceId);
shopB.add("anchor", "laochen", { fields: { name: "老陈五金" } }, "2026-11-13T08:00:00");
shopB.add("claim", "laochen",
  { sponsors: [DP.sponsorSign(root, "laochen", ownerB.deviceId)] }, "2026-11-13T08:10:00");
check("根担保 1 位即成立", shopB.state(new Date("2026-11-13T12:00:00")).laochen.trust === "L2");

const shopA = new DP.Store(ownerA, root.deviceId);
shopA.add("anchor", "haoyouduo", { fields: { name: "好又多超市" } }, "2026-11-13T08:00:00");
shopA.add("claim", "haoyouduo", { sponsors: [] }, "2026-11-13T09:00:00");
let s = shopA.state(new Date("2026-11-13T12:00:00"));
check("无担保不给 L2", s.haoyouduo.trust !== "L2", s.haoyouduo.trust);

const fake = new DP.Store(faker, root.deviceId);
fake.merge([...shopA.events.values()]);
fake.add("claim", "haoyouduo", { sponsors: [] }, "2026-11-13T07:00:00"); // 更早也没用
s = fake.state(new Date("2026-11-13T12:00:00"));
check("冒名者时间更早但无担保 → 不是店主", s.haoyouduo.owner === null, s.haoyouduo.owner);

// 老陈 + 根之外的第二位作保人：再造一位
const ownerC = DP.Identity.generate();
const shopC = new DP.Store(ownerC, root.deviceId);
shopC.add("anchor", "zhangjie", { fields: { name: "张姐早餐" } }, "2026-11-13T08:00:00");
shopC.add("claim", "zhangjie",
  { sponsors: [DP.sponsorSign(root, "zhangjie", ownerC.deviceId)] }, "2026-11-13T08:10:00");

const all = new DP.Store(ownerA, root.deviceId);
for (const src of [shopA, shopB, shopC, fake]) all.merge([...src.events.values()]);
all.add("claim", "haoyouduo", {
  sponsors: [DP.sponsorSign(ownerB, "haoyouduo", ownerA.deviceId),
             DP.sponsorSign(ownerC, "haoyouduo", ownerA.deviceId)]
}, "2026-11-13T09:30:00");
s = all.state(new Date("2026-11-13T12:00:00"));
check("两位已认领店主作保 → L2", s.haoyouduo.trust === "L2", s.haoyouduo.trust);
check("店主是真店主，不是冒名者", s.haoyouduo.owner === ownerA.deviceId, s.haoyouduo.owner);

console.log("[6] 同步收敛");
const p1 = new DP.Store(alice, root.deviceId);
const p2 = new DP.Store(bob, root.deviceId);
p1.add("fact", "haoyouduo", { fields: { note: "熟客可赊账" } }, "2026-11-13T11:50:00");
p2.add("fact", "laochen", { fields: { open: true } }, "2026-11-13T11:55:00");
p2.merge(p1.diff(p2.ids()));
p1.merge(p2.diff(p1.ids()));
check("事件集一致", [...p1.ids()].sort().join() === [...p2.ids()].sort().join());
const now = new Date("2026-11-13T12:00:00");
check("归约结果一致", DP.canonical(p1.state(now)) === DP.canonical(p2.state(now)));

console.log("\n结果：" + pass + " 通过 / " + fail + " 失败\n");
process.exit(fail === 0 ? 0 : 1);
