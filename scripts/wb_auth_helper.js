// wb_auth_helper.js — 从 WorkBuddy 客户端运行时取得 at-rest 密钥并解密登录态
// 用法（由 workbuddy_checkin.py 自动调用，也可手动）：
//   ELECTRON_RUN_AS_NODE=1 "WorkBuddy.exe" wb_auth_helper.js <auth-info.json 路径> [字段名...]
// 输出: 单行 JSON {"ok":true,"fields":{"accessToken":"<明文JWT>",...,"uid":"..."}}
// 密钥只存在于本子进程内存，进程退出即消失，不写任何文件。

const fs = require("fs");
const crypto = require("crypto");

const AUTH_FILE = process.argv[2];
const WANTED = process.argv.slice(3).length ? process.argv.slice(3) : ["accessToken", "refreshToken"];

if (!AUTH_FILE || !fs.existsSync(AUTH_FILE)) {
  console.log(JSON.stringify({ ok: false, error: `auth file not found: ${AUTH_FILE}` }));
  process.exit(1);
}

function fail(error) {
  console.log(JSON.stringify({ ok: false, error }));
  process.exit(1);
}

// 1) 取 atRestSecretKey：走 WorkBuddy 自己的原生绑定，密钥只存在于运行中的客户端
let storage;
try {
  storage = process._linkedBinding("electron_browser_workbuddy_storage");
} catch (e) {
  fail(`cannot access workbuddy storage binding: ${e.message}`);
}
let payload;
try {
  payload = JSON.parse(storage.loggerGet());
} catch (e) {
  fail(`loggerGet() failed: ${e.message}`);
}
if (!payload || typeof payload.atRestSecretKey !== "string") {
  fail("atRestSecretKey unavailable (client logged out or unsupported version)");
}

// 2) 派生密钥: protectorKey = SHA256(atRestSecretKey utf8); keyId = sha256(key).hex[:16]
const protectorKey = crypto.createHash("sha256").update(payload.atRestSecretKey, "utf8").digest();
const keyId = crypto.createHash("sha256").update(protectorKey).digest("hex").slice(0, 16);

// 3) 读登录态文件
let info;
try {
  info = JSON.parse(fs.readFileSync(AUTH_FILE, "utf8"));
} catch (e) {
  fail(`cannot parse auth file: ${e.message}`);
}

// 4) AES-256-GCM 解密 sym-v1 field 信封
//    AAD = "WB-AAD\0" | 0x01 | u32(5)"WBEV1" | u32(6)"sym-v1" | u32(suite) | u32(16)keyId | [2] | [0] | [0]
function buildAad(suite, envelopeKeyId) {
  const prefix = Buffer.from("WB-AAD\0", "ascii");
  const parts = [prefix, Buffer.from([1])];
  const tag = (s) => {
    const b = Buffer.from(s, "utf8");
    const l = Buffer.alloc(4);
    l.writeUInt32BE(b.length);
    return Buffer.concat([l, b]);
  };
  parts.push(tag("WBEV1"));            // framing tag: field
  parts.push(tag("sym-v1"));           // scheme
  const suiteBuf = Buffer.alloc(4);
  suiteBuf.writeUInt32BE(suite);
  parts.push(suiteBuf);
  parts.push(tag(envelopeKeyId));
  parts.push(Buffer.from([2]));        // framing number: field=2
  parts.push(Buffer.from([0]));        // optional uint64 sequence: absent
  parts.push(Buffer.from([0]));        // final: absent -> 0
  return Buffer.concat(parts);
}

function decryptField(wrapper) {
  if (typeof wrapper === "string") return wrapper; // 明文 JWT（旧版本/未加密）
  if (!wrapper || wrapper.$wbEncrypted !== 1 || !wrapper.envelope) {
    throw new Error("unsupported field wrapper");
  }
  const env = JSON.parse(Buffer.from(wrapper.envelope, "base64").toString("utf8"));
  if (env.suite !== 1) throw new Error(`unsupported suite ${env.suite}`);
  if (env.keyId !== keyId) throw new Error(`KEY_MISMATCH: envelope keyId ${env.keyId}`);
  const key = Buffer.from(protectorKey);
  const decipher = crypto.createDecipheriv(
    "aes-256-gcm", key, Buffer.from(env.nonce, "base64"), { authTagLength: 16 });
  decipher.setAAD(buildAad(env.suite, env.keyId));
  decipher.setAuthTag(Buffer.from(env.authTag, "base64"));
  return Buffer.concat([
    decipher.update(Buffer.from(env.ciphertext, "base64")),
    decipher.final(),
  ]).toString("utf8");
}

try {
  const auth = info.auth || {};
  const account = info.account || {};
  const fields = {};
  for (const name of WANTED) {
    // 字段可能在 auth(令牌) 或 account(昵称/手机号) 下
    const wrapper = auth[name] !== undefined ? auth[name]
      : account[name] !== undefined ? account[name] : undefined;
    if (wrapper === undefined) continue;
    try {
      fields[name] = decryptField(wrapper);
    } catch (e) {
      if (name === "accessToken") throw e; // 令牌解密失败必须报错
      fields[name + "Error"] = e.message;   // 其他字段失败则降级
    }
  }
  fields.uid = account.uid || "";
  fields.type = account.type || "";
  fields.uin = account.uin ? String(account.uin) : "";
  fields.domain = auth.domain || "";
  // 输出前清理密钥
  protectorKey.fill(0);
  console.log(JSON.stringify({ ok: true, fields }));
} catch (e) {
  fail(`decrypt failed: ${e.message}`);
}
