import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import check_deployment_access as access


class DeploymentAccessTests(unittest.TestCase):
    def responses(self):
        return [
            {"AppId": 1432808015, "OwnerUin": 100048848339, "Uin": 100000000001},
            {"FunctionName": access.FUNCTION, "Type": "HTTP", "Environment": {"secret": "do-not-print"}},
            {"PermissionList": [{"Resource": access.COLLECTION, "ResourceType": "collection", "Permission": "ADMINONLY"}]},
        ]

    def run_check(self, responses):
        with patch.object(access, "cli_json", side_effect=responses) as run:
            output = []
            result = access.check_access(run=run, emit=output.append)
        return result, run, "\n".join(output)

    def test_success_checks_are_read_only_and_do_not_expose_function_details(self):
        result, run, output = self.run_check(self.responses())
        self.assertEqual("passed", result["status"])
        self.assertEqual(3, run.call_count)
        command = run.call_args_list[1].args[0]
        body = json.loads(command[command.index("--body") + 1])
        self.assertEqual("FALSE", body["ShowCode"])
        self.assertEqual(access.ENV_ID, body["Namespace"])
        self.assertNotIn("do-not-print", output + json.dumps(result))

    def test_wrong_app_or_owner_stops_before_reading_function(self):
        for key in ("AppId", "OwnerUin"):
            responses = self.responses()
            responses[0][key] = 1
            result, run, _ = self.run_check(responses)
            self.assertEqual("ACCOUNT_MISMATCH", result["error_code"])
            self.assertEqual(1, run.call_count)

    def test_denied_function_keeps_ci_identity_and_stops(self):
        result, run, _ = self.run_check([self.responses()[0], access.AccessError("UnauthorizedOperation")])
        self.assertEqual("100000000001", result["identity"]["Uin"])
        self.assertEqual("function", result["failed_check"])
        self.assertEqual("UnauthorizedOperation", result["error_code"])
        self.assertEqual(2, run.call_count)

    def test_non_http_or_wrong_function_blocks(self):
        for key, value, error in [("Type", "Event", "FUNCTION_TYPE_MISMATCH"),
                                  ("FunctionName", "other", "FUNCTION_IDENTITY_MISMATCH")]:
            responses = self.responses()
            responses[1][key] = value
            result, run, _ = self.run_check(responses)
            self.assertEqual(error, result["error_code"])
            self.assertEqual(2, run.call_count)

    def test_public_missing_duplicate_or_other_collection_permission_blocks(self):
        for entries in [[], [{"Resource": access.COLLECTION, "ResourceType": "collection", "Permission": "READONLY"}],
                        [{"Resource": "other", "ResourceType": "collection", "Permission": "ADMINONLY"}],
                        self.responses()[2]["PermissionList"] * 2]:
            responses = self.responses()
            responses[2] = {"PermissionList": entries}
            result, _, _ = self.run_check(responses)
            self.assertEqual("CACHE_PERMISSION_MISMATCH", result["error_code"])

    def test_cli_failure_reports_only_safe_error_code(self):
        response = subprocess.CompletedProcess([], 1, stdout=json.dumps({"error": {
            "code": "UnauthorizedOperation", "message": "do-not-print", "secret": "do-not-print"}}), stderr="")
        with patch.object(access.shutil, "which", return_value="tcb"), patch.object(access.subprocess, "run", return_value=response):
            with self.assertRaises(access.AccessError) as caught:
                access.cli_json(["api", "scf", "GetFunction"])
        self.assertEqual("UnauthorizedOperation", str(caught.exception))

    def test_cli_accepts_banner_and_rejects_malformed_output(self):
        self.assertEqual({"data": {"x": 1}}, access.parse_response('CLI banner\n{"data":{"x":1}}\n'))
        with self.assertRaises(access.AccessError):
            access.parse_response("do-not-print")

    def test_failed_main_writes_sanitized_diagnostics_and_returns_failure(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "check.json"
            with patch.object(access, "check_access", return_value={"schema_version": 1, "identity": {},
                    "status": "blocked", "failed_check": "identity", "error_code": "ACCOUNT_MISMATCH"}), patch.dict(access.os.environ, {}, clear=True):
                self.assertEqual(1, access.main(["--output", str(target)]))
            self.assertEqual("blocked", json.loads(target.read_text())["status"])
