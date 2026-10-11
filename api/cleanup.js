// GET /api/cleanup — Vercel Cron gọi mỗi ngày: xoá máy đã bị huỷ trên vast.ai
// dù không ai mở dashboard. Vercel tự gửi "Authorization: Bearer $CRON_SECRET".
import { K, checkEnv, isViewer, pipeline, reconcile, redis, send } from "./_lib.js";

export default async function handler(req, res) {
  if (!checkEnv(res)) return;
  const auth = req.headers["authorization"] || "";
  const cronOk = process.env.CRON_SECRET && auth === `Bearer ${process.env.CRON_SECRET}`;
  if (!cronOk && !isViewer(req)) return send(res, 401, { error: "unauthorized" });
  try {
    const ids = (await redis("SMEMBERS", K.ids)) || [];
    const raws = await pipeline(ids.map(id => ["GET", K.status(id)]));
    const machines = [], gone = [];
    ids.forEach((id, i) => (raws[i] ? machines.push(JSON.parse(raws[i])) : gone.push(id)));
    if (gone.length) await redis("SREM", K.ids, ...gone);
    const r = await reconcile(machines);
    send(res, 200, { ok: true, removed: r.removed.concat(gone), remaining: r.machines.length });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
}
