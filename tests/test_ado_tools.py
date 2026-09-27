# -*- coding: utf-8 -*-
"""Unit tests for ``ollama-comet/ado.py`` (Azure DevOps QA tools).

``request_http`` and ``request_bytes`` are patched with a scripted fake so the
module's REST calls are verified end-to-end without touching Azure DevOps.
"""
import binascii
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
_ADO_SPEC = importlib.util.spec_from_file_location(
    "autonomous_browser_ado", ROOT / "ollama-comet" / "ado.py"
)
ado = importlib.util.module_from_spec(_ADO_SPEC)
_ADO_SPEC.loader.exec_module(ado)


CONFIG = {
    "ado_org": "https://dev.azure.com/csc-ddsb",
    "ado_pat": "SUPERSECRETPAT",
    "ado_project": "AutomationAndAccessibility",
    "ado_grace_api": "http://localhost:5223",
    "ado_grace_token": "GRACETOKEN123",
}


def make_xlsx(sheet_names, strings, marker=None):
    """Build a tiny in-memory .xlsx (zip of two XML parts)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        sheets = "".join(f'<sheet name="{name}" sheetId="{i}" />'
                         for i, name in enumerate(sheet_names, 1))
        archive.writestr(
            "xl/workbook.xml",
            f"<workbook><sheets>{sheets}</sheets></workbook>",
        )
        items = "".join(f"<si><t>{text}</t></si>" for text in strings)
        archive.writestr(
            "xl/sharedStrings.xml", f"<sst>{items}</sst>"
        )
    return buffer.getvalue()


class FakeHttp:
    """Scripted stand-in for ado.request_http."""

    def __init__(self, routes):
        self.routes = [tuple(route) for route in routes]
        self.calls = []

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30):
        self.calls.append(
            {"url": url, "method": method, "body": body, "headers": dict(headers or {}), "timeout": timeout}
        )
        if not self.routes:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        status, body_value = self.routes.pop(0)
        if isinstance(body_value, (dict, list)):
            body_value = json.dumps(body_value)
        return status, body_value


def _json_body(call):
    """Decode a recorded request body (request_http receives dicts pre-encode)."""
    body = call["body"]
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    elif not isinstance(body, str):
        body = json.dumps(body)
    return json.loads(body)


class AdoToolTestsBase(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp([])
        self._http_patch = patch.object(ado, "request_http", self.http)
        self._bytes_patch = patch.object(ado, "request_bytes", self.http)
        self._http_patch.start()
        self._bytes_patch.start()

    def tearDown(self):
        self._http_patch.stop()
        self._bytes_patch.stop()


class MaskSecretTests(unittest.TestCase):
    def test_short_value_fully_masked(self):
        self.assertEqual(ado.mask_secret("ab"), "**")
        self.assertEqual(ado.mask_secret(""), "")

    def test_long_value_keeps_prefix(self):
        self.assertEqual(ado.mask_secret("SUPERSECRETPAT"), "SUP" + "*" * 11)


class RequireConfigTests(unittest.TestCase):
    def test_missing_org(self):
        client = ado.AdoClient({"ado_pat": "p"})
        with self.assertRaises(ado.AdoError) as ctx:
            client.require_config()
        self.assertIn("ado_org", str(ctx.exception))

    def test_org_without_scheme(self):
        client = ado.AdoClient({"ado_org": "dev.azure.com/x", "ado_pat": "p"})
        with self.assertRaises(ado.AdoError) as ctx:
            client.require_config()
        self.assertIn("ado_org", str(ctx.exception))

    def test_missing_pat(self):
        client = ado.AdoClient({"ado_org": "https://dev.azure.com/x"})
        with self.assertRaises(ado.AdoError) as ctx:
            client.require_config()
        self.assertIn("ado_pat", str(ctx.exception))


class AdoConnectTests(AdoToolTestsBase):
    def test_connect_lists_projects_and_masks_pat(self):
        self.http.routes = [(200, {"value": [{"name": "P1"}, {"name": "P2"}]})]
        result = ado.dispatch_tool("AdoConnect", {}, CONFIG)
        self.assertTrue(result["connected"])
        self.assertEqual(result["projects"], ["P1", "P2"])
        self.assertEqual(result["grace_api"], "http://localhost:5223")
        self.assertEqual(result["credentials"]["pat"], ado.mask_secret("SUPERSECRETPAT"))
        self.assertNotIn("SUPERSECRETPAT", json.dumps(result))

    def test_connect_http_error_becomes_error_dict(self):
        self.http.routes = [(401, {"message": "TF400813: unauthorized"})]
        result = ado.dispatch_tool("AdoConnect", {}, CONFIG)
        self.assertIn("401", result["error"])

    def test_missing_config_short_circuits(self):
        result = ado.dispatch_tool("AdoConnect", {}, {"ado_org": "", "ado_pat": ""})
        self.assertIn("error", result)
        self.assertEqual(self.http.calls, [])


class RepoDiscoveryTests(AdoToolTestsBase):
    def test_list_repositories_requires_project(self):
        config = dict(CONFIG, ado_project="")
        result = ado.dispatch_tool("AdoListRepositories", {}, config)
        self.assertIn("No project specified", result["error"])

    def test_list_repositories_returns_shapes(self):
        self.http.routes = [
            (200, {"value": [{"name": "R1", "id": "g1", "defaultBranch": "refs/heads/main"}]})
        ]
        result = ado.dispatch_tool(
            "AdoListRepositories", {"project": "AutomationAndAccessibility"}, CONFIG
        )
        self.assertEqual(result["repositories"][0]["name"], "R1")
        self.assertIn("/AutomationAndAccessibility/_apis/git/repositories", self.http.calls[0]["url"])
        self.assertIn("api-version=7.1", self.http.calls[0]["url"])

    def test_find_test_files_filters_and_limits(self):
        items = {
            "value": [
                {"path": "/docs", "isFolder": True},
                {"path": "/img/pic.png", "size": 10},
                {"path": "/tests/TC01.xlsx", "size": 111},
                {"path": "/tests/TC02.XLSX", "size": 222},
                {"path": "/tests/TC03.xlsx", "size": 333},
            ]
        }
        self.http.routes = [(200, items)]
        result = ado.dispatch_tool(
            "AdoFindTestFiles", {"repo": "RepoOne", "project": "P", "limit": 2}, CONFIG
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual([f["path"] for f in result["test_files"]], ["/tests/TC01.xlsx", "/tests/TC02.XLSX"])
        self.assertEqual(result["test_files"][0]["repo"], "RepoOne")
        self.assertIn("items?scopePath=%2F&recursionLevel=Full&api-version=7.1", self.http.calls[0]["url"])

    def test_find_test_files_skips_failing_repos(self):
        self.http.routes = [
            (200, {"value": [{"name": "Good"}, {"name": "Bad"}]}),
            (200, {"value": [{"path": "/t/TC1.xlsx", "size": 1}]}),
            (404, {"message": "no repo"}),
        ]
        result = ado.dispatch_tool("AdoFindTestFiles", {"project": "P"}, CONFIG)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["test_files"][0]["repo"], "Good")


class DownloadTests(AdoToolTestsBase):
    def test_download_success(self):
        self.http.routes = [(200, b"XLSXBYTES")]
        data = ado.AdoClient(CONFIG).download_test_file("RepoOne", "/t/TC1.xlsx", "P")
        self.assertEqual(data, b"XLSXBYTES")
        call = self.http.calls[0]
        self.assertIn("items?scopePath=%2Ft%2FTC1.xlsx&recursionLevel=None", call["url"])

    def test_download_http_error(self):
        self.http.routes = [(404, b"")]
        with self.assertRaises(ado.AdoError):
            ado.AdoClient(CONFIG).download_test_file("RepoOne", "/t/TC1.xlsx", "P")


class InspectTests(AdoToolTestsBase):
    def test_inspect_returns_summary(self):
        workbook = make_xlsx(["Runtime Settings", "Steps"], ["TestCaseName", "StepName", "step one"])
        self.http.routes = [(200, workbook)]
        result = ado.dispatch_tool(
            "AdoInspectTestFile", {"repo": "RepoOne", "path": "/t/TC1.xlsx", "project": "P"}, CONFIG
        )
        self.assertEqual(result["sheets"], ["Runtime Settings", "Steps"])
        self.assertTrue(result["grace_like"])
        self.assertIn("TestCaseName", result["preview_strings"])
        self.assertIn("step one", result["preview_strings"])

    def test_inspect_requires_repo_and_path(self):
        result = ado.dispatch_tool("AdoInspectTestFile", {"repo": "RepoOne"}, CONFIG)
        self.assertIn("requires 'repo' and 'path'", result["error"])

    def test_inspect_rejects_oversized_workbook(self):
        self.http.routes = [(200, b"x" * (ado.MAX_WORKBOOK_BYTES + 1))]
        result = ado.dispatch_tool(
            "AdoInspectTestFile", {"repo": "RepoOne", "path": "/t/TC1.xlsx"}, CONFIG
        )
        self.assertIn("larger than", result["error"])

    def test_inspect_rejects_invalid_xlsx(self):
        self.http.routes = [(200, b"not a zip")]
        result = ado.dispatch_tool(
            "AdoInspectTestFile", {"repo": "RepoOne", "path": "/t/TC1.xlsx"}, CONFIG
        )
        self.assertIn("not a valid .xlsx", result["error"])


class ParseXlsxTests(unittest.TestCase):
    def test_missing_markers_reported(self):
        workbook = make_xlsx(["Sheet1"], ["nothing relevant"])
        summary = ado.parse_xlsx_summary(workbook)
        self.assertFalse(summary["grace_like"])

    def test_invalid_bytes_raise(self):
        with self.assertRaises(ado.AdoError):
            ado.parse_xlsx_summary(b"junk")

    def test_no_shared_strings(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook><sheets></sheets></workbook>")
        summary = ado.parse_xlsx_summary(buffer.getvalue())
        self.assertEqual(summary["sheets"], [])
        self.assertFalse(summary["grace_like"])


class TestPlanToolTests(AdoToolTestsBase):
    def test_list_plans(self):
        self.http.routes = [(200, {"value": [{"id": 1, "name": "Plan A", "state": "Active"}]})]
        result = ado.dispatch_tool("AdoListTestPlans", {}, CONFIG)
        self.assertEqual(result["test_plans"][0]["name"], "Plan A")

    def test_list_suites(self):
        self.http.routes = [(200, {"value": [{"id": 9, "name": "S", "suiteType": "StaticTestSuite"}]})]
        result = ado.dispatch_tool("AdoListSuites", {"plan_id": 12}, CONFIG)
        self.assertEqual(result["suites"][0]["id"], 9)
        self.assertIn("testplan/plans/12/suites?api-version=7.1", self.http.calls[0]["url"])

    def test_list_test_points_shapes(self):
        payload = {
            "value": [
                {"testPoint": {"id": 10, "state": "Active", "results": {"outcome": "Passed", "testCaseId": 101}}},
                {"testPoint": {"id": 11, "state": "Active", "outcome": "Failed", "results": {}}},
                {"id": 12, "results": {"outcome": "Blocked"}},
            ]
        }
        self.http.routes = [(200, payload)]
        result = ado.dispatch_tool(
            "AdoListTestPoints", {"plan_id": 12, "suite_id": 34}, CONFIG
        )
        points = result["test_points"]
        self.assertEqual(points[0]["id"], 10)
        self.assertEqual(points[0]["outcome"], "Passed")
        self.assertEqual(points[0]["test_case"], 101)
        self.assertEqual(points[1]["outcome"], "Failed")
        self.assertEqual(points[2]["id"], 12)
        self.assertIn(
            "testpoints?includePointDetails=true&api-version=7.1", self.http.calls[0]["url"]
        )

    def test_list_test_points_filters_outcome(self):
        payload = {
            "value": [
                {"testPoint": {"id": 10, "results": {"outcome": "Passed"}}},
                {"testPoint": {"id": 11, "outcome": "Failed"}},
            ]
        }
        self.http.routes = [(200, payload)]
        result = ado.dispatch_tool(
            "AdoListTestPoints", {"plan_id": 12, "suite_id": 34, "outcome": "failed"}, CONFIG
        )
        self.assertEqual([p["id"] for p in result["test_points"]], [11])


class RunManagementTests(AdoToolTestsBase):
    def test_list_runs_top_param(self):
        self.http.routes = [(200, {"value": [{"id": 5, "name": "R", "state": "Completed",
                                              "result": "Passed", "isAutomated": True,
                                              "url": "http://u"}]})]
        result = ado.dispatch_tool("AdoListTestRuns", {"top": 5}, CONFIG)
        self.assertEqual(result["test_runs"][0]["id"], 5)
        self.assertIn("test/runs?$top=5&api-version=7.1", self.http.calls[0]["url"])

    def test_create_run_body(self):
        self.http.routes = [(200, {"id": 77, "name": "GRACE automated run",
                                   "state": "InProgress", "webAccessUrl": "http://web"})]
        result = ado.AdoClient(CONFIG).create_test_run(12, [1, 2], name="My run")
        self.assertEqual(result["id"], 77)
        self.assertEqual(result["web_url"], "http://web")
        body = _json_body(self.http.calls[0])
        self.assertEqual(body["plan"]["id"], 12)
        self.assertEqual(body["pointIds"], [1, 2])
        self.assertTrue(body["isAutomated"])
        self.assertEqual(body["name"], "My run")


class PublishFlowTests(AdoToolTestsBase):
    def _results(self):
        return [
            {"outcome": "Passed", "point_id": 1, "comment": "ok"},
            {"outcome": "Failed", "point_id": 2, "errorMessage": "boom"},
            {"outcome": "Passed", "point_id": 3},
        ]

    def test_full_publish_flow_chunks_results(self):
        self.http.routes = [
            (200, {"id": 77, "name": "GRACE automated run", "state": "InProgress"}),
            (200, {"count": 2, "value": [{}, {}]}),
            (200, {"count": 1, "value": [{}]}),
            (200, {"id": 77, "state": "Completed"}),
            (200, {"id": 77, "name": "R", "state": "Completed",
                   "passedTests": 2, "unanalyzedTests": 1, "totalTests": 3}),
        ]
        with patch.object(ado, "RESULTS_CHUNK_SIZE", 2):
            result = ado.dispatch_tool(
                "AdoPublishTestRun",
                {"confirm": True, "plan_id": 12, "point_ids": [1, 2, 3],
                 "results": self._results()},
                CONFIG,
            )
        self.assertEqual(result["run"]["id"], 77)
        self.assertEqual(result["published"]["results_published"], 3)
        self.assertEqual(result["state_after_close"]["state"], "Completed")
        self.assertEqual(result["verified"]["passed"], 2)
        self.assertEqual(result["verified"]["total"], 3)
        posts = [c for c in self.http.calls if c["method"] == "POST"]
        self.assertEqual(len(posts), 3)  # create run + 2 chunks
        first_chunk = _json_body(posts[1])
        self.assertEqual(len(first_chunk), 2)
        self.assertEqual(first_chunk[0]["testPoint"]["id"], 1)
        self.assertEqual(first_chunk[1]["outcome"], "Failed")
        patches = [c for c in self.http.calls if c["method"] == "PATCH"]
        self.assertEqual(_json_body(patches[0])["state"], "Completed")
        verify = self.http.calls[-1]
        self.assertEqual(verify["method"], "GET")
        self.assertIn("test/runs/77", verify["url"])

    def test_publish_requires_confirmation(self):
        result = ado.dispatch_tool(
            "AdoPublishTestRun",
            {"plan_id": 12, "point_ids": [1], "results": [{"outcome": "Passed", "point_id": 1}]},
            CONFIG,
        )
        self.assertIn("confirm=true", result["error"])
        self.assertEqual(self.http.calls, [])

    def test_publish_rejects_invalid_outcome(self):
        result = ado.dispatch_tool(
            "AdoPublishTestRun",
            {"confirm": True, "plan_id": 12, "point_ids": [1],
             "results": [{"outcome": "SuperPassed", "point_id": 1}]},
            CONFIG,
        )
        self.assertIn("is not valid", result["error"])
        self.assertEqual(self.http.calls, [])

    def test_publish_requires_point_id(self):
        result = ado.dispatch_tool(
            "AdoPublishTestRun",
            {"confirm": True, "plan_id": 12, "point_ids": [1],
             "results": [{"outcome": "Passed"}]},
            CONFIG,
        )
        self.assertIn("point", result["error"])
        self.assertEqual(self.http.calls, [])

    def test_publish_requires_plan_and_points(self):
        result = ado.dispatch_tool(
            "AdoPublishTestRun", {"confirm": True, "results": []}, CONFIG
        )
        self.assertIn("plan_id", result["error"])

    def test_run_grace_requires_confirmation(self):
        result = ado.dispatch_tool(
            "AdoRunGraceTest", {"env": "EDCS-9", "browser": "chromium", "repo": "R", "path": "/t.xlsx"},
            CONFIG,
        )
        self.assertIn("confirm=true", result["error"])
        self.assertEqual(self.http.calls, [])

    def test_run_grace_requires_env_and_browser(self):
        result = ado.dispatch_tool("AdoRunGraceTest", {"confirm": True}, CONFIG)
        self.assertIn("env", result["error"])

    def test_run_grace_requires_a_source(self):
        result = ado.dispatch_tool(
            "AdoRunGraceTest", {"confirm": True, "env": "EDCS-9", "browser": "chromium"}, CONFIG
        )
        self.assertIn("local_path", result["error"])
        self.assertIn("repo", result["error"])


class GraceRunTests(AdoToolTestsBase):
    def _run(self, config=None, path=None):
        arguments = {
            "confirm": True,
            "env": "EDCS-9",
            "browser": "chromium",
            "browser_version": "131",
            "local_path": str(path),
        }
        return ado.dispatch_tool("AdoRunGraceTest", arguments, config or dict(CONFIG))

    def test_run_grace_sends_multipart_with_bearer(self):
        self.http.routes = [(200, {"testRunId": 42, "status": "running"})]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "TC01.xlsx"
            path.write_bytes(b"workbook-bytes")
            result = self._run(path=path)
        call = self.http.calls[0]
        body = call["body"]
        self.assertEqual(call["method"], "POST")
        self.assertIn("api/test/run", call["url"])
        self.assertIn(b'name="env"', body)
        self.assertIn(b"EDCS-9", body)
        self.assertIn(b'name="browser"', body)
        self.assertIn(b"chromium", body)
        self.assertIn(b'name="browserVersion"', body)
        self.assertIn(b"131", body)
        self.assertIn(b'filename="TC01.xlsx"', body)
        self.assertIn(b"workbook-bytes", body)
        self.assertTrue(call["headers"]["Authorization"].startswith("Bearer "))
        self.assertIn("multipart/form-data", call["headers"]["Content-Type"])
        self.assertEqual(result["test_run_id"], 42)
        self.assertIn("AdoListTestRuns", result["note"])

    def test_run_grace_without_token_omits_header(self):
        self.http.routes = [(200, {"id": 7, "status": "running"})]
        stripped = {k: v for k, v in CONFIG.items() if k != "ado_grace_token"}
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "TC01.xlsx"
            path.write_bytes(b"data")
            result = self._run(config=stripped, path=path)
        self.assertNotIn("Authorization", self.http.calls[0]["headers"])
        self.assertEqual(result["test_run_id"], 7)

    def test_run_grace_401_message(self):
        self.http.routes = [(401, {"message": "token rejected"})]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "TC01.xlsx"
            path.write_bytes(b"data")
            result = self._run(path=path)
        self.assertIn("401", result["error"])
        self.assertIn("ado_grace_token", result["error"])

    def test_run_grace_error_status(self):
        self.http.routes = [(500, {"message": "boom"})]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "TC01.xlsx"
            path.write_bytes(b"data")
            result = self._run(path=path)
        self.assertIn("500", result["error"])

    def test_run_grace_from_repo_download(self):
        self.http.routes = [(200, b"workbook-bytes"), (200, {"testRunId": 9})]
        result = ado.dispatch_tool(
            "AdoRunGraceTest",
            {"confirm": True, "env": "EDCS-9", "browser": "chromium",
             "repo": "RepoOne", "path": "/t/TC1.xlsx"},
            CONFIG,
        )
        self.assertEqual(result["test_run_id"], 9)
        download = self.http.calls[0]
        self.assertIn("items?scopePath=%2Ft%2FTC1.xlsx&recursionLevel=None", download["url"])
        body = self.http.calls[1]["body"]
        self.assertIn(b'filename="TC1.xlsx"', body)


class UnknownToolTests(AdoToolTestsBase):
    def test_unknown_tool(self):
        result = ado.dispatch_tool("AdoNonsense", {}, CONFIG)
        self.assertEqual(result["error"], "Unknown ADO tool: AdoNonsense")


if __name__ == "__main__":
    unittest.main()