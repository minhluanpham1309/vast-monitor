// Shared helpers: Upstash Redis REST, auth, body parsing, vast.ai cleanup.

// Vercel/Upstash có thể gắn tiền tố tuỳ ý (vd STORAGE_KV_REST_API_URL) nên tìm theo hậu tố.
function envBySuffix(...suffixes) {
  for (const suf of suffixes) {
    if (process.env[suf]) return process.env[suf];
    const k = Object.keys(process.env).find(k => k.endsWith("_" + suf) && process.env[k]);
    if (k) return process.env[k];
  }
  return undefined;
}
const REDIS_URL = envBySuffix("UPSTASH_REDIS_REST_URL", "KV_REST_API_URL");
const REDIS_TOKEN = envBySuffix("UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN");

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
  if (missing.length) {
    // chỉ liệt kê TÊN biến (không lộ giá trị) để dễ chẩn đoán
    const seen = Object.keys(process.env).filter(k => /TOKEN|KV_|REDIS|UPSTASH|VAST|CRON/.test(k)).sort();
    send(res, 500, {
      error: "Thiếu biến môi trường: " + missing.join(", ") +
        ` (môi trường: ${process.env.VERCEL_ENV || "?"}; biến liên quan đang thấy: ${seen.join(", ") || "không có"})`,
    });
    return false;
  }
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
  hist: "vm:hist",                 // hash: id -> lịch sử thuê (không TTL, không xoá khi instance bị huỷ)
  hlock: id => `vm:hlock:${id}`,
  gone: id => `vm:gone:${id}`,     // máy đã bị xoá tay: chặn agent gửi lại trong 30 ngày
};

export async function forget(id, reason = "removed", at = null) {
  await markEnded(id, reason, at);
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
      if (s === undefined) { await forget(m.id, "destroyed", m.last_seen); removed.push(m.id); continue; }
      m.vast_status = s;
    }
    keep.push(m);
  }
  return { machines: keep, removed };
}


// ------------------------------------------------------------ lịch sử thuê
const GAP = 900; // >15 phút không thấy máy thì coi là một đoạn chạy mới (vd stop rồi start lại)

async function vastInstanceInfo(instanceId) {
  const key = process.env.VAST_API_KEY;
  if (!key || !instanceId) return null;
  try {
    const q = new URLSearchParams({ limit: "1", select_filters: JSON.stringify({ id: { eq: Number(instanceId) } }) });
    const r = await fetch(`https://console.vast.ai/api/v1/instances?${q}`, { headers: { Authorization: `Bearer ${key}` } });
    if (!r.ok) return null;
    const inst = ((await r.json()).instances || [])[0];
    return inst || null;
  } catch { return null; }
}

/** Gọi sau mỗi lần push; thực sự ghi tối đa 5 phút/lần cho mỗi máy để tiết kiệm lệnh Redis. */
export async function touchHistory(id, st) {
  const [lock] = await pipeline([["SET", K.hlock(id), "1", "EX", 300, "NX"]]);
  if (lock !== "OK") return;
  const now = Date.now() / 1000;
  const raw = await redis("HGET", K.hist, id);
  const h = raw ? JSON.parse(raw) : { id, instance_id: st.instance_id || null, segs: [[now, now]], runs: {} };
  h.name = st.name || h.name;
  h.hostname = st.hostname || h.hostname;
  if (st.gpus?.length) { h.gpu_name = st.gpus[0].name; h.gpu_count = st.gpus.length; }
  const last = h.segs[h.segs.length - 1];
  if (now - last[1] > GAP) h.segs.push([now, now]); else last[1] = now;
  if (h.segs.length > 200) h.segs.splice(0, h.segs.length - 200);
  h.last_seen = now;
  h.ended = null; h.end_reason = null;
  for (const r of st.runs || []) {
    h.runs[r.name] = { status: r.status, started: r.started, ended: r.ended, last: r.last || null };
  }
  const names = Object.keys(h.runs);
  if (names.length > 100) for (const n of names.slice(0, names.length - 100)) delete h.runs[n];
  if (h.dph == null && h.instance_id) {
    const inst = await vastInstanceInfo(h.instance_id);
    if (inst) {
      h.dph = inst.dph_total ?? null;
      if (inst.gpu_name) h.gpu_name = inst.gpu_name;
      if (inst.num_gpus) h.gpu_count = inst.num_gpus;
      if (inst.label) h.label = inst.label;
      // máy đã chạy trước khi agent bật: lùi mốc bắt đầu về lúc vast khởi động instance
      if (inst.start_date && h.segs.length === 1 && inst.start_date < h.segs[0][0]) h.segs[0][0] = inst.start_date;
    }
  }
  await redis("HSET", K.hist, id, JSON.stringify(h));
}

export async function markEnded(id, reason, at) {
  const raw = await redis("HGET", K.hist, id);
  if (!raw) return;
  const h = JSON.parse(raw);
  const t = at || h.last_seen;
  h.segs[h.segs.length - 1][1] = Math.max(h.segs[h.segs.length - 1][1], t || 0);
  h.ended = t; h.end_reason = reason;
  await redis("HSET", K.hist, id, JSON.stringify(h));
}


/** Xoá tay một máy: xoá sạch trạng thái, lệnh, log, lịch sử thuê, sự kiện; chặn agent của máy đó gửi lại. */
export async function purge(id) {
  const evs = (await redis("LRANGE", K.events, 0, -1)) || [];
  const keep = evs.filter(s => { try { return JSON.parse(s).machine !== id; } catch { return true; } });
  const cmds = [
    ["DEL", K.status(id), K.cmd(id), K.watch(id), K.res(id), K.hlock(id)],
    ["SREM", K.ids, id],
    ["HDEL", K.hist, id],
    ["SET", K.gone(id), "1", "EX", 30 * 86400],
  ];
  if (keep.length !== evs.length) {
    cmds.push(["DEL", K.events]);
    if (keep.length) cmds.push(["RPUSH", K.events, ...keep]);
  }
  await pipeline(cmds);
  return { events_removed: evs.length - keep.length };
}
