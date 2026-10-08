"use strict";
// Daily Python generation calls the same adapter as the HTTP service.
const { refresh, seedQuote } = require("./upstream.js");
const { shanghaiDate } = require("./premium_quotes.js");
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", async () => {
  try {
    const parsed = JSON.parse(input), catalog = Array.isArray(parsed) ? parsed : parsed.catalog;
    const previousRecords = {};
    for (const record of parsed.previousRecords || []) {
      try { previousRecords[record.code] = seedQuote(record, shanghaiDate(new Date())); } catch (_) { /* Invalid cached records cannot constrain live data. */ }
    }
    process.stdout.write(JSON.stringify(await refresh(catalog, { previousRecords })));
  }
  catch (error) { process.stderr.write(error.stack); process.exitCode = 1; }
});
