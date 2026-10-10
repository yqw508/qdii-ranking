"""Read-only CI identity and publication-access checks with sanitized diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENV_ID = "run-cool-d2gy0iw957219659c"
APP_ID = "1432808015"
OWNER_UIN = "100048848339"
FUNCTION = "qdii-premium-api"
COLLECTION = "qdii_premium_cache"


class AccessError(Exception):
    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", code) else "CLI_ERROR"
        super().__init__(self.code)


def parse_response(output: str) -> dict:
    """Ignore CLI banners, but never print raw output or upstream error messages."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", output):
        try:
            value, _ = decoder.raw_decode(output[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and ("data" in value or "error" in value):
            return value
    raise AccessError("INVALID_CLI_RESPONSE")


def cli_json(args: list[str]) -> dict:
    executable = shutil.which("tcb")
    if not executable:
        raise AccessError("CLI_NOT_FOUND")
    try:
        result = subprocess.run(
            [executable, *args, "--json"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        raise AccessError("CLI_TIMEOUT") from None
    except OSError:
        raise AccessError("CLI_EXECUTION_FAILED") from None
    response = parse_response(result.stdout + "\n" + result.stderr)
    if result.returncode or "error" in response:
        error = response.get("error")
        code = error.get("code", "CLI_ERROR") if isinstance(error, dict) else "CLI_ERROR"
        raise AccessError(str(code))
    data = response.get("data")
    if not isinstance(data, dict):
        raise AccessError("INVALID_CLI_RESPONSE")
    return data


def identifier(value) -> str:
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise AccessError("INVALID_ACCOUNT_IDENTITY")
    return value


def check_access(run=cli_json, emit=print) -> dict:
    evidence = {"schema_version": 1, "status": "blocked", "identity": {}, "checks": []}
    stage = "identity"
    try:
        account = run(["api", "cam", "GetUserAppId", "--api-version", "2019-01-16"])
        identity = {key: identifier(account.get(key)) for key in ("AppId", "Uin", "OwnerUin")}
        evidence["identity"] = identity
        emit("CloudBase CI identity: " + json.dumps(identity, sort_keys=True))
        if identity["AppId"] != APP_ID or identity["OwnerUin"] != OWNER_UIN:
            raise AccessError("ACCOUNT_MISMATCH")
        evidence["checks"].append({"check": stage, "status": "passed"})

        stage = "function"
        details = run([
            "api", "scf", "GetFunction", "--api-version", "2018-04-16",
            "--env-id", ENV_ID, "--body", json.dumps({
                "Namespace": ENV_ID, "FunctionName": FUNCTION, "ShowCode": "FALSE",
            }),
        ])
        if details.get("FunctionName") != FUNCTION:
            raise AccessError("FUNCTION_IDENTITY_MISMATCH")
        if details.get("Type") != "HTTP":
            raise AccessError("FUNCTION_TYPE_MISMATCH")
        evidence["checks"].append({"check": stage, "status": "passed"})
        emit(f"Verified HTTP function {FUNCTION}")

        stage = "cache_permission"
        permissions = run(["permission", "get", "collection:" + COLLECTION, "--env-id", ENV_ID])
        entries = permissions.get("PermissionList")
        if not isinstance(entries, list):
            raise AccessError("INVALID_PERMISSION_RESPONSE")
        matching = [item for item in entries if isinstance(item, dict)
                    and item.get("Resource") == COLLECTION and item.get("ResourceType") == "collection"]
        if len(matching) != 1 or matching[0].get("Permission") != "ADMINONLY":
            raise AccessError("CACHE_PERMISSION_MISMATCH")
        evidence["checks"].append({"check": stage, "status": "passed"})
        emit(f"Verified {COLLECTION} remains ADMINONLY")
        evidence["status"] = "passed"
        emit("Read-only checks passed; write permissions must still pass the production deployment.")
    except AccessError as exc:
        evidence.update(failed_check=stage, error_code=exc.code)
        evidence["checks"].append({"check": stage, "status": "blocked", "error_code": exc.code})
        emit(f"Deployment access blocked at {stage}: {exc.code}")
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/qdii-ranking/deployment-access-check.json"))
    args = parser.parse_args(argv)
    evidence = check_access()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as out:
            out.write("## CloudBase deployment access\n\n")
            out.write("CI identity: `" + json.dumps(evidence["identity"], sort_keys=True) + "`\n\n")
            out.write(f"Result: {evidence['status']}\n")
            if evidence["status"] != "passed":
                out.write(f"Failed check: {evidence['failed_check']} ({evidence['error_code']})\n")
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
