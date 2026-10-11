// Shared helpers: Upstash Redis REST, auth, body parsing, vast.ai cleanup.

const REDIS_URL = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
const REDIS_TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;

export const STATUS_TTL = 48 * 3600;   // fallback: máy không gửi gì trong 48h thì tự xoá
export const LOST_AFTER = 180;         // giây không thấy heartbeat => "mất liên lạc"

export async function redis(...cmd) {
  const r = await fetch(REDIS_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${REDIS_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  });
  const j = await r.json();
  if (j.error) throw new Error("redis: " + j.error);
  return j.result;
}

export async function pipeline(cmds) {
  if (!cmds.length) return [];
  const r = await fetch(REDIS_URL.replace(/\/$/, "") + "/pipeline", {
    method: "POST",
    headers: { Authorization: `Bearer ${REDIS_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(cmds),
  });
  const j = await r.json();
  if (!Array.isArray(j)) throw new Error("redis pipeline: " + JSON.stringify(j));
  return j.map(x => x.result);
}

export function send(res, code, obj) {
  res.statusCode = code;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(obj));
}

export async function readBody(req) {
  if (req.body !== undefined) {
    return typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body;
  }
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const s = Buffer.concat(chunks).toString("utf8");
  return s ? JSON.parse(s) : {};
}

export function query(req) {
  return Object.fromEntries(new URL(req.url, "http://x").searchParams);
}

function bearer(req) {
  const h = req.headers["authorization"] || "";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}

function safeEq(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

export function checkEnv(res) {
  const missing = [];
  if (!REDIS_URL || !REDIS_TOKEN) missing.push("UPSTASH_REDIS_REST_URL/TOKEN");
  if (!process.env.AGENT_TOKEN) missing.push("AGENT_TOKEN");
  if (!process.env.VIEW_TOKEN) missing.push("VIEW_TOKEN");
  if (missing.length) { send(res, 500, { error: "Thiếu biến môi trường: " + missing.join(", ") }); return false; }
  return true;
}

export const isAgent = req => safeEq(bearer(req), process.env.AGENT_TOKEN || "");
export const isViewer = req => safeEq(bearer(req), process.env.VIEW_TOKEN || "");

export const K = {
  ids: "vm:ids",
  status: id => `vm:s:${id}`,
  cmd: id => `vm:cmd:${id}`,
  watch: id => `vm:watch:${id}`,
  res: id => `vm:res:${id}`,
  events: "vm:ev",
};

export async function forget(id) {
  await pipeline([
    ["DEL", K.status(id), K.cmd(id), K.watch(id), K.res(id)],
    ["SREM", K.ids, id],
  ]);
}

// ------------------------------------------------------------ vast.ai lookup
let vastCache = { at: 0, key: "", data: null };

/** Trả về Map(instance_id -> actual_status) cho các id được hỏi, hoặc null nếu không tra được. */
export async function vastStatuses(ids) {
  const key = process.env.VAST_API_KEY;
  if (!key || !ids.length) return null;
  const cacheKey = ids.slice().sort().join(",");
  if (vastCache.key === cacheKey && Date.now() - vastCache.at < 60_000) return vastCache.data;
  const out = new Map();
  let after = null;
  try {
    for (let page = 0; page < 20; page++) {
      const q = new URLSearchParams({
        limit: "25",
        select_cols: JSON.stringify(["id", "actual_status", "cur_state", "intended_status"]),
        select_filters: JSON.stringify({ id: { in: ids.map(Number) } }),
      });
      if (after) q.set("after_token", after);
      const r = await fetch(`https://console.vast.ai/api/v1/instances?${q}`, {
        headers: { Authorization: `Bearer ${key}` },
      });
      if (!r.ok) return null;          // lỗi API -> không xoá gì cả
      const j = await r.json();
      for (const inst of j.instances || []) out.set(String(inst.id), inst.actual_status || inst.cur_state || "unknown");
      after = j.next_token;
      if (!after) break;
    }
  } catch {
    return null;
  }
  vastCache = { at: Date.now(), key: cacheKey, data: out };
  return out;
}

/**
 * Với các máy lâu không gửi heartbeat: hỏi vast.ai.
 *  - instance không còn trong tài khoản  -> đã bị huỷ -> xoá khỏi dashboard
 *  - còn nhưng không running            -> gắn vast_status (vd "exited") để UI hiện "Đã dừng"
 */
export async function reconcile(machines, now = Date.now() / 1000) {
  const stale = machines.filter(m => m.instance_id && now - (m.last_seen || 0) > LOST_AFTER);
  const removed = [];
  if (!stale.length) return { machines, removed };
  const st = await vastStatuses(stale.map(m => String(m.instance_id)));
  if (!st) return { machines, removed };
  const keep = [];
  for (const m of machines) {
    if (stale.includes(m)) {
      const s = st.get(String(m.instance_id));
      if (s === undefined) { await forget(m.id); removed.push(m.id); continue; }
      m.vast_status = s;
    }
    keep.push(m);
  }
  return { machines: keep, removed };
}
