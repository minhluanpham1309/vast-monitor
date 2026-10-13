// POST /api/push  — agent trên máy vast gửi trạng thái, nhận lại lệnh chờ xử lý.
import { K, STATUS_TTL, checkEnv, isAgent, pipeline, readBody, send, touchHistory } from "./_lib.js";

const MAX_BYTES = 900_000;

export default async function handler(req, res) {
  if (!checkEnv(res)) return;
  if (req.method !== "POST") return send(res, 405, { error: "POST only" });
  if (!isAgent(req)) return send(res, 401, { error: "unauthorized" });

  let body;
  try { body = await readBody(req); } catch { return send(res, 400, { error: "bad json" }); }
  const id = String(body.id || "").replace(/[^\w.\-]/g, "").slice(0, 64);
  if (!id) return send(res, 400, { error: "missing id" });

  // máy đã bị xoá tay trên dashboard -> không ghi gì, bảo agent dừng
  try {
    const [gone] = await pipeline([["GET", K.gone(id)]]);
    if (gone) return send(res, 200, { ok: true, stop: true, reason: "removed from dashboard", commands: [], watch: null });
  } catch (e) { return send(res, 500, { error: String(e.message || e) }); }

  const { events = [], cmd_results = [], ...status } = body;
  status.id = id;
  status.last_seen = Date.now() / 1000;
  let json = JSON.stringify(status);
  // quá lớn thì giảm dần: biểu đồ run đã xong -> log -> watch -> config; không bao giờ cắt cụt chuỗi JSON
  const shrink = [
    () => { for (const r of status.runs || []) if (r.status !== "running" && Object.keys(r.series || {}).length) { r.series = {}; r.series_dropped = true; } },
    () => { status.logs = (status.logs || []).map(l => ({ ...l, text: String(l.text || "").slice(-4000) })); },
    () => { if (status.watch_result) status.watch_result.text = String(status.watch_result.text || "").slice(-20000); },
    () => { for (const r of status.runs || []) r.config = undefined; status.log_files = (status.log_files || []).slice(0, 30); },
    () => { for (const r of status.runs || []) if (r.status === "running") for (const k in r.series || {}) r.series[k] = r.series[k].filter((_, i, a) => i % 2 === 0 || i === a.length - 1); },
  ];
  for (const step of shrink) { if (json.length <= MAX_BYTES) break; step(); json = JSON.stringify(status); }
  if (json.length > MAX_BYTES) return send(res, 413, { error: "payload quá lớn" });

  const cmds = [
    ["SET", K.status(id), json, "EX", STATUS_TTL],
    ["SADD", K.ids, id],
    ["LRANGE", K.cmd(id), 0, -1],
    ["DEL", K.cmd(id)],
    ["GET", K.watch(id)],
  ];
  for (const e of events.slice(0, 50)) cmds.push(["LPUSH", K.events, JSON.stringify({ ...e, machine: id, name: status.name })]);
  if (events.length) cmds.push(["LTRIM", K.events, 0, 199]);
  for (const r of cmd_results.slice(0, 20)) cmds.push(["LPUSH", K.res(id), JSON.stringify(r)]);
  if (cmd_results.length) cmds.push(["LTRIM", K.res(id), 0, 19], ["EXPIRE", K.res(id), 3600]);

  try {
    const out = await pipeline(cmds);
    const commands = (out[2] || []).map(s => { try { return JSON.parse(s); } catch { return null; } }).filter(Boolean);
    const watch = out[4] ? JSON.parse(out[4]) : null;
    try { await touchHistory(id, status); } catch (e) { console.error("history", e); }
    send(res, 200, { ok: true, commands, watch });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
}
