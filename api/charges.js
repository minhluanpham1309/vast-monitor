// GET /api/charges?from=<unix>&to=<unix> — hoá đơn thật từ vast.ai (kể cả máy đã destroy), cần VIEW_TOKEN.
// Dựa trên GET https://console.vast.ai/api/v0/charges (vastai show invoices-v1 --charges).
import { checkEnv, isViewer, query, send } from "./_lib.js";

const cache = new Map(); // key -> {at, data}

function parseRow(r) {
  const [kind, idStr] = String(r.source || "").split("-");
  const gpuItems = (r.items || []).filter(i => i.type === "gpu");
  let hours = 0, rate = null;
  for (const it of gpuItems) {
    const m = /([\d.]+)\s*hours?\s*at\s*\$([\d.]+)\s*\/\s*hour/i.exec(it.description || "");
    if (m) { hours += Number(m[1]); rate = Number(m[2]); }
  }
  if (!gpuItems.length) {
    const m = /([\d.]+)\s*hours?\s*at\s*\$([\d.]+)\s*\/\s*hour/i.exec(r.description || "");
    if (m) { hours = Number(m[1]); rate = Number(m[2]); }
  }
  const byType = {};
  for (const it of r.items || []) byType[it.type] = (byType[it.type] || 0) + (Number(it.amount) || 0);
  return {
    type: r.type,
    source: r.source,
    instance_id: kind === "instance" || kind === "volume" ? idStr : null,
    start: r.start, end: r.end,
    amount: Number(r.amount) || 0,
    description: r.description || "",
    label: r.metadata?.label || null,
    gpu_hours: hours || null,
    rate,
    by_type: byType,               // {gpu, disk, bwd, bwu}
    items: (r.items || []).map(i => ({ type: i.type, amount: i.amount, description: i.description })),
  };
}

export default async function handler(req, res) {
  if (!checkEnv(res)) return;
  if (!isViewer(req)) return send(res, 401, { error: "unauthorized" });
  const key = process.env.VAST_API_KEY;
  if (!key) return send(res, 200, { available: false, reason: "Chưa đặt VAST_API_KEY trên Vercel" });

  const q = query(req);
  const from = Math.floor(Number(q.from)), to = Math.floor(Number(q.to));
  if (!(from > 0 && to > from)) return send(res, 400, { error: "from/to" });
  const ck = `${from}-${to}`;
  const hit = cache.get(ck);
  if (hit && Date.now() - hit.at < 5 * 60_000) return send(res, 200, hit.data);

  const results = [];
  let after = null;
  try {
    for (let page = 0; page < 20; page++) {
      const p = new URLSearchParams({
        select_filters: JSON.stringify({ day: { gte: from, lte: to - 1 }, type: { in: ["instance", "volume"] } }),
        format: "table", limit: "500", latest_first: "true",
      });
      if (after) p.set("after_token", after);
      const r = await fetch(`https://console.vast.ai/api/v0/charges?${p}`, { headers: { Authorization: `Bearer ${key}` } });
      if (!r.ok) {
        const txt = (await r.text()).slice(0, 300);
        const hint = r.status === 401 || r.status === 404 ? " — key sai/đã xoá, hoặc Vercel chưa Redeploy sau khi đổi VAST_API_KEY"
          : r.status === 403 ? " — key không có quyền billing (hoặc thiếu header)"
          : r.status === 429 ? " — gọi quá nhiều, thử lại sau ít phút" : "";
        const tail = key.length > 8 ? `, key …${key.slice(-4)}` : "";
        const reason = `vast.ai trả lỗi ${r.status}${hint}${tail}. Phản hồi: ${txt || "(trống)"}`;
        return send(res, 200, { available: false, reason });
      }
      const j = await r.json();
      for (const row of j.results || []) results.push(parseRow(row));
      after = j.next_token;
      if (!after) break;
    }
  } catch (e) {
    return send(res, 200, { available: false, reason: "Không gọi được vast.ai: " + (e.message || e) });
  }
  const data = { available: true, from, to, results };
  cache.set(ck, { at: Date.now(), data });
  if (cache.size > 50) cache.delete(cache.keys().next().value);
  send(res, 200, data);
}
