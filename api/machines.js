// GET    /api/machines        — danh sách máy + sự kiện (cần VIEW_TOKEN)
// DELETE /api/machines?id=..  — xoá hẳn một máy: trạng thái, log, lịch sử thuê, sự kiện; agent của máy đó bị chặn
import { K, checkEnv, isViewer, pipeline, purge, query, reconcile, redis, send } from "./_lib.js";

export default async function handler(req, res) {
  if (!checkEnv(res)) return;
  if (!isViewer(req)) return send(res, 401, { error: "unauthorized" });

  try {
    if (req.method === "DELETE") {
      const { id } = query(req);
      if (!id) return send(res, 400, { error: "missing id" });
      const r = await purge(id);
      return send(res, 200, { ok: true, ...r });
    }
    if (req.method !== "GET") return send(res, 405, { error: "method" });

    const ids = (await redis("SMEMBERS", K.ids)) || [];
    const cmds = [["LRANGE", K.events, 0, 99]];
    for (const id of ids) cmds.push(["GET", K.status(id)], ["LRANGE", K.res(id), 0, 19], ["GET", K.watch(id)]);
    const out = await pipeline(cmds);

    const events = (out[0] || []).map(s => JSON.parse(s));
    let machines = [];
    const gone = [];
    ids.forEach((id, i) => {
      const raw = out[1 + i * 3];
      if (!raw) { gone.push(id); return; }         // hết TTL
      const m = JSON.parse(raw);
      m.cmd_results = (out[2 + i * 3] || []).map(s => JSON.parse(s));
      m.watch_req = out[3 + i * 3] ? JSON.parse(out[3 + i * 3]) : null;
      machines.push(m);
    });
    if (gone.length) await redis("SREM", K.ids, ...gone);

    const r = await reconcile(machines);
    machines = r.machines.sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));
    send(res, 200, {
      now: Date.now() / 1000,
      machines,
      events,
      removed: r.removed.concat(gone),
      vast_api: Boolean(process.env.VAST_API_KEY),
    });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
}
