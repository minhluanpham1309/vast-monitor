// POST /api/command — gửi lệnh cho agent (cần VIEW_TOKEN)
//   {id, type:"kill", pid, sig}                 -> agent gửi signal ở lần push kế tiếp
//   {id, type:"watch", kind:"file"|"tmux", target, lines} -> agent đẩy nội dung file / pane, chu kỳ 5s
//   {id, type:"unwatch"}
//   {id, type:"clear_events"}
import { K, checkEnv, isViewer, pipeline, readBody, send } from "./_lib.js";

export default async function handler(req, res) {
  if (!checkEnv(res)) return;
  if (req.method !== "POST") return send(res, 405, { error: "POST only" });
  if (!isViewer(req)) return send(res, 401, { error: "unauthorized" });

  let b;
  try { b = await readBody(req); } catch { return send(res, 400, { error: "bad json" }); }
  const id = String(b.id || "");
  try {
    if (b.type === "kill") {
      const pid = parseInt(b.pid, 10);
      const sig = ["TERM", "INT", "KILL"].includes(b.sig) ? b.sig : "TERM";
      if (!id || !pid) return send(res, 400, { error: "id/pid" });
      const cmd = { type: "kill", pid, sig, cid: Math.random().toString(36).slice(2, 10), at: Date.now() / 1000 };
      await pipeline([["RPUSH", K.cmd(id), JSON.stringify(cmd)], ["EXPIRE", K.cmd(id), 3600]]);
      return send(res, 200, { ok: true, cid: cmd.cid });
    }
    if (b.type === "watch") {
      const kind = b.kind === "tmux" ? "tmux" : "file";
      const target = String(b.target || "").slice(0, 500);
      const lines = Math.max(10, Math.min(parseInt(b.lines, 10) || 200, 3000));
      if (!id || !target) return send(res, 400, { error: "id/target" });
      await pipeline([["SET", K.watch(id), JSON.stringify({ kind, target, lines }), "EX", 90]]);
      return send(res, 200, { ok: true });
    }
    if (b.type === "unwatch") {
      await pipeline([["DEL", K.watch(id)]]);
      return send(res, 200, { ok: true });
    }
    if (b.type === "clear_events") {
      await pipeline([["DEL", K.events]]);
      return send(res, 200, { ok: true });
    }
    send(res, 400, { error: "unknown type" });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
}
