// The endpoint behind serpmaven's usage ping (src/serpmaven/usage.py).
//
// Takes POST /v1/ping with the ping's JSON and writes one data point to a Workers
// Analytics Engine dataset: the version, OS, Python version, command, proxy kind and the
// country Cloudflare reads from the request. The machine's random id is the index, so a
// machine counts once. The client's address is not stored. Anything else is answered 404.

const KINDS = new Set(["nodemaven", "nodemaven-demo", "url", "none"]);
const COMMANDS = new Set(["search", "serve", "mcp"]);

function clip(value, max) {
  return typeof value === "string" ? value.slice(0, max) : "";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/v1/ping") {
      return new Response("not found", { status: 404 });
    }
    const length = Number(request.headers.get("content-length") || "0");
    if (length > 1024) {
      return new Response("too large", { status: 413 });
    }
    let ping;
    try {
      ping = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }
    if (!COMMANDS.has(ping.cmd) || !KINDS.has(ping.proxy)) {
      return new Response("bad ping", { status: 400 });
    }
    const id = /^[0-9a-f]{32}$/.test(ping.id || "") ? ping.id : "none";
    env.USAGE.writeDataPoint({
      indexes: [id],
      blobs: [
        clip(ping.v, 20),
        clip(ping.os, 20),
        clip(ping.py, 8),
        ping.cmd,
        ping.proxy,
        clip(request.cf && request.cf.country, 2),
      ],
      doubles: [1],
    });
    return new Response(null, { status: 204 });
  },
};
