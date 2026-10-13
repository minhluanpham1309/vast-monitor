// GET    /api/history          — toàn bộ lịch sử thuê máy (cần VIEW_TOKEN)
// POST   /api/history          — sửa tay {id, dph?, name?, note?}
// DELETE /api/history?id=...   — xoá một bản ghi
import { K, checkEnv, isViewer, query, readBody, redis, send } from "./_lib.js";

export default async function handler(req, res) {
  if (!checkEnv(res)) return;
  if (!isViewer(req)) return send(res, 401, { error: "unauthorized" });
  try {
    if (req.method === "GET") {
      const flat = (await redis("HGETALL", K.hist)) || [];
      const items = [];
      // Upstash trả HGETALL dạng mảng [k1, v1, k2, v2, ...]
      for (let i = 0; i < flat.length; i += 2) {
        try { items.push(JSON.parse(flat[i + 1])); } catch {}
      }
      return send(res, 200, { now: Date.now() / 1000, items });
    }
    if (req.method === "DELETE") {
      const { id } = query(req);
      if (!id) return send(res, 400, { error: "missing id" });
      await redis("HDEL", K.hist, id);
      return send(res, 200, { ok: true });
    }
    if (req.method === "POST") {
      const b = await readBody(req);
      const raw = await redis("HGET", K.hist, String(b.id || ""));
      if (!raw) return send(res, 404, { error: "not found" });
      const h = JSON.parse(raw);
      if (b.dph !== undefined) h.dph = b.dph === null || b.dph === "" ? null : Number(b.dph);
      if (typeof b.name === "string" && b.name.trim()) h.name = b.name.trim().slice(0, 120);
      if (typeof b.note === "string") h.note = b.note.slice(0, 500);
      await redis("HSET", K.hist, h.id, JSON.stringify(h));
      return send(res, 200, { ok: true, item: h });
    }
    send(res, 405, { error: "method" });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
}
