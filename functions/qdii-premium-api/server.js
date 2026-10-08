"use strict";
const http = require("node:http");
const { randomUUID } = require("node:crypto");
const { createService } = require("./service.js");
const { createStore } = require("./store.js");
const SITE = "https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com";

function handler(service, log = console.error) {
  return async (req, res) => {
    const requestId = randomUUID();
    const headers = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
      "Vary": "Origin", "X-Request-Id": requestId };
    if (req.headers.origin === SITE) {
      if (process.env.GATEWAY_CORS !== "1") headers["Access-Control-Allow-Origin"] = SITE;
      headers["Access-Control-Allow-Methods"] = "GET, OPTIONS";
      headers["Access-Control-Expose-Headers"] = "X-Request-Id";
    }
    function send(code, body) { res.writeHead(code, headers); res.end(JSON.stringify(body)); }
    if (req.headers.origin && req.headers.origin !== SITE) return send(403, { error: "ORIGIN_DENIED", request_id: requestId });
    const url = new URL(req.url, SITE);
    if (url.pathname !== "/premium/v1/quotes") return send(404, { error: "NOT_FOUND", request_id: requestId });
    if (req.method === "OPTIONS") return send(204, null);
    if (req.method !== "GET") return send(405, { error: "METHOD_NOT_ALLOWED", request_id: requestId });
    if ([...url.searchParams.keys()].some(k => k !== "catalog") || url.searchParams.getAll("catalog").length !== 1)
      return send(400, { error: "INVALID_REQUEST", request_id: requestId });
    try {
      const payload = await service.get(url.searchParams.get("catalog"), requestId);
      if (payload.retry_after_seconds) headers["Retry-After"] = String(payload.retry_after_seconds);
      send(payload.http_status || (payload.status === "unavailable" ? 503 : 200), payload);
    } catch (error) {
      log(JSON.stringify({ request_id: requestId, error: error.message }));
      send(503, { schema_version: 1, status: "unavailable", error: "SERVICE_UNAVAILABLE", request_id: requestId });
    }
  };
}
if (require.main === module) {
  const cloudbase = require("@cloudbase/node-sdk");
  const db = cloudbase.init({ env: process.env.SCF_NAMESPACE || "run-cool-d2gy0iw957219659c" }).database();
  const snapshot = require("./seed.json");
  const service = createService({ snapshot, store: createStore(db) });
  http.createServer(handler(service)).listen(9000, "0.0.0.0");
}
module.exports = { handler, SITE };
