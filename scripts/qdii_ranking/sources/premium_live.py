"""Reuse the service's bounded Node adapter for daily quote generation."""
import json
import shutil
import subprocess
from pathlib import Path

from ..errors import DataError


def fetch_live_quotes(entries, previous_records=None):
    node = shutil.which("node")
    if not node:
        raise DataError("Node.js is required for the shared premium adapter")
    script = Path(__file__).resolve().parents[3] / "functions/qdii-premium-api/daily.js"
    try:
        result = subprocess.run([node, str(script)], input=json.dumps({'catalog': entries, 'previousRecords': previous_records or []}),
                                encoding="utf-8", capture_output=True, timeout=30, check=True)
        payload = json.loads(result.stdout)
        if not isinstance(payload.get("quotes"), dict) or not isinstance(payload.get("errors"), list):
            raise ValueError("invalid shared adapter response")
        return payload
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        raise DataError(f"Shared premium adapter failed: {exc}") from exc


def quote_rows_from_live(payload):
    rows = {}
    for code, q in payload["quotes"].items():
        from datetime import datetime
        if code != q["code"]:
            raise DataError("Shared premium adapter returned conflicting code")
        rows[code] = {"f12": code, "f14": q["name"], "f2": q["marketPriceCny"],
                      "f3": q["changePct"], "f6": q["turnoverCny"], "f402": -q["premiumPct"],
                      "f441": q["referenceValueCny"] if q["referenceType"] == "iopv" else "-",
                      "f297": q["quoteDate"].replace("-", ""),
                      "f124": datetime.fromisoformat(q["updatedAt"].replace("Z", "+00:00")).timestamp(),
                      "quote_source": q["quoteSource"], "adapter_version": q["adapterVersion"],
                      "quote_source_url": q["quoteSourceUrl"], "quote_delay_minutes": q["quoteDelayMinutes"],
                      "_reference": {"reference_value_type": q["referenceType"],
                                     "reference_value_cny": q["referenceValueCny"],
                                     "reference_value_date": q["referenceDate"],
                                     "reference_value_source_url": f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=1"}}
    return rows
