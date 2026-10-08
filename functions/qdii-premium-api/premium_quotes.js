(function (global) {
  "use strict";
  function finiteNumber(value, label, code) {
    if (value == null || value === "" || value === "-" || typeof value === "boolean") {
      throw new Error(`${code} ${label} is missing`);
    }
    const number = Number(value);
    if (!Number.isFinite(number)) {
      throw new Error(`${code} ${label} is missing or non-finite`);
    }
    return number;
  }

  function shanghaiDate(value) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }

  function shanghaiDateTime(value) {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
  }

  function normalizeQuote(raw, entry, asOf) {
    const code = entry.code;
    if (!raw || String(raw.f12 || "") !== code) {
      throw new Error(`quote code differs from requested ETF ${code}`);
    }
    const price = finiteNumber(raw.f2, "price", code);
    const sourceDiscount = finiteNumber(raw.f402, "discount rate", code);
    const changePct = finiteNumber(raw.f3, "change percentage", code);
    const turnoverCny = finiteNumber(raw.f6, "turnover", code);
    const hasIopv = raw.f441 != null && raw.f441 !== "" && raw.f441 !== "-";
    const referenceType = hasIopv ? "iopv" : entry.referenceType;
    const referenceValueCny = hasIopv
      ? finiteNumber(raw.f441, "IOPV", code)
      : finiteNumber(entry.referenceValueCny, "NAV", code);
    const referenceDate = referenceType === "nav" ? String(entry.referenceDate || "") : null;
    if (!hasIopv && referenceType !== "nav") {
      throw new Error(`${code} has neither IOPV nor a verified NAV reference`);
    }
    if (referenceType === "nav" && (!/^\d{4}-\d{2}-\d{2}$/.test(referenceDate) || referenceDate > asOf)) {
      throw new Error(`${code} NAV reference date is invalid or in the future`);
    }
    if (price <= 0 || referenceValueCny <= 0 || turnoverCny < 0) {
      throw new Error(`${code} price, reference value, or turnover is outside its valid range`);
    }
    const quoteDateRaw = String(raw.f297 || "");
    if (!/^\d{8}$/.test(quoteDateRaw)) {
      throw new Error(`${code} quote date is invalid`);
    }
    const quoteDate = `${quoteDateRaw.slice(0, 4)}-${quoteDateRaw.slice(4, 6)}-${quoteDateRaw.slice(6, 8)}`;
    const updatedTimestamp = finiteNumber(raw.f124, "timestamp", code);
    const updatedDate = new Date(updatedTimestamp * 1000);
    if (!Number.isFinite(updatedTimestamp) || Number.isNaN(updatedDate.getTime())) {
      throw new Error(`${code} update timestamp is invalid`);
    }
    if (quoteDate > asOf || shanghaiDate(updatedDate) > asOf) {
      throw new Error(`${code} quote contains future data`);
    }
    const premiumPct = -sourceDiscount;
    const calculatedPremium = (price / referenceValueCny - 1) * 100;
    const tolerance = Math.max(0.05, (0.0005 / referenceValueCny) * 100 + 0.01);
    if (Math.abs(premiumPct - calculatedPremium) > tolerance) {
      throw new Error(`${code} premium differs from price/${referenceType.toUpperCase()} calculation`);
    }
    return {
      code,
      name: String(raw.f14 || entry.name || "").trim(),
      benchmarkGroup: entry.benchmarkGroup,
      marketPriceCny: price,
      referenceType,
      referenceValueCny,
      referenceDate,
      premiumPct,
      changePct,
      turnoverCny,
      quoteDate,
      updatedAt: updatedDate.toISOString(),
      updatedText: shanghaiDateTime(updatedDate),
    };
  }

  function normalizeResponse(payload, entries, asOf) {
    const rows = payload && payload.data && payload.data.diff;
    if (!Array.isArray(rows)) {
      throw new Error("行情响应缺少记录列表");
    }
    const entryByCode = new Map(entries.map((entry) => [entry.code, entry]));
    const seen = new Set();
    const valid = new Map();
    const errors = [];
    rows.forEach((raw) => {
      const code = String((raw && raw.f12) || "");
      const entry = entryByCode.get(code);
      if (!entry) return;
      if (seen.has(code)) {
        valid.delete(code);
        errors.push(`${code} 返回重复`);
        return;
      }
      seen.add(code);
      if (
        entry.category === "qdii" &&
        ["f2", "f402", "f3", "f6"].some((field) =>
          raw[field] == null || raw[field] === "" || raw[field] === "-"
        )
      ) {
        errors.push(`${code} 缺少行情字段`);
        return;
      }
      try {
        valid.set(code, normalizeQuote(raw, entry, asOf));
      } catch (error) {
        errors.push(error.message);
      }
    });
    entries.forEach((entry) => {
      if (!seen.has(entry.code)) errors.push(`${entry.code} 缺失`);
    });
    return { valid, errors };
  }

  const api = { normalizeQuote, normalizeResponse, shanghaiDate, shanghaiDateTime };
  if (typeof module !== "undefined") module.exports = api;
  global.QdiiPremiumQuotes = api;
})(globalThis);
