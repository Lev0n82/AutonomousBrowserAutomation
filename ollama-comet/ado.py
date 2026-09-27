"""Azure DevOps QA tools for the Ollama Comet browser agent.

Lets the agent work with an Azure DevOps organisation end to end:

* discover projects and repositories and locate test-case workbooks
  (``.xlsx``) checked into any git repository;
* inspect a workbook without Excel/openpyxl (zip + XML only) to see its
  sheets and whether it looks like a GRACE test;
* hand a workbook to the local GRACE API for execution (the run is
  reported back to Azure Test Plans by GRACE itself);
* create Azure Test Plans runs, publish per-test-point results and
  close the run — the same flow GRACE V2 uses (``publishToAdo``).

Configuration keys (``%LOCALAPPDATA%\OllamaComet\config.json``):

* ``ado_org``        – organisation URL, e.g. ``https://dev.azure.com/csc-ddsb`` (required)
* ``ado_pat``        – personal access token (required; never echoed back)
* ``ado_project``    – optional default project for tool calls
* ``ado_grace_api``  – GRACE API base, default ``http://localhost:5223``
* ``ado_grace_token``– optional Entra bearer token for the GRACE API

Guardrails mirrored from the Azure-Devops-QA skill:

* the PAT is read from settings only and is always masked in output;
* anything that executes a test or publishes results requires the
  caller to pass ``confirm: true``;
* writes are verified with a read-back GET where the API allows it;
* every REST call uses ``api-version=7.1``.

All errors are returned as ``{"error": ...}`` dicts — dispatch never
raises into the chat loop.
"""

import base64
import binascii
import io
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile

ADO_API_VERSION = "7.1"
DEFAULT_GRACE_API = "http://localhost:5223"
GRACE_API_TIMEOUT = 1800
RESULTS_CHUNK_SIZE = 200
MAX_WORKBOOK_BYTES = 15 * 1024 * 1024
MAX_TEST_FILE_LIMIT = 100

GRACE_MARKERS = (
    "testcasename",
    "stepname",
    "runenvironment",
    "runtime settings",
    "collection_files",
)

TEST_OUTCOMES = (
    "Passed",
    "Failed",
    "Blocked",
    "NotApplicable",
    "NotExecuted",
    "Inconclusive",
    "Timeout",
    "Aborted",
    "Paused",
    "InProgress",
)


class AdoError(RuntimeError):
    """Raised for configuration, network and API problems."""


def request_http(url, method="GET", body=None, headers=None, timeout=30):
    """Perform an HTTP request and return ``(status, text)``."""
    data = body
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, payload.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        try:
            payload = err.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            payload = ""
        return err.code, payload
    except Exception as err:
        raise AdoError(f"HTTP request failed: {err}") from err


def request_bytes(url, headers=None, timeout=120):
    """Perform an HTTP request and return ``(status, bytes)``."""
    request = urllib.request.Request(url, method="GET")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        try:
            payload = err.read()
        except Exception:  # pragma: no cover - defensive
            payload = b""
        return err.code, payload
    except Exception as err:
        raise AdoError(f"HTTP request failed: {err}") from err


def mask_secret(value, keep=3):
    """Mask a credential, keeping only a short readable prefix."""
    text = str(value or "")
    if len(text) <= keep:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep)


def _post_multipart(url, file_field, file_name, file_bytes, fields, headers=None, timeout=GRACE_API_TIMEOUT):
    """POST ``multipart/form-data`` with one file plus plain fields."""
    boundary = "OllamaCometFormBoundary7d1a2b3c4e5f6a7b"
    parts = []
    for name, value in (fields or {}).items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_name}"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    body = b"".join(parts)
    request_headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    request_headers.update(headers or {})
    return request_http(url, method="POST", body=body, headers=request_headers, timeout=timeout)


def _decode_json(text):
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


class AdoClient:
    """Thin Azure DevOps REST 7.1 client driven by the settings block."""

    def __init__(self, config):
        config = config or {}
        self.org = str(config.get("ado_org", "")).strip().rstrip("/")
        self.pat = str(config.get("ado_pat", "")).strip()
        self.project = str(config.get("ado_project", "")).strip()
        self.grace_api = (
            str(config.get("ado_grace_api", "")).strip().rstrip("/")
            or DEFAULT_GRACE_API
        )
        self.grace_token = str(config.get("ado_grace_token", "")).strip()

    # -- setup ----------------------------------------------------------
    def require_config(self):
        if not self.org or not self.org.startswith("http"):
            raise AdoError(
                "Azure DevOps is not configured. Set ado_org "
                "(e.g. https://dev.azure.com/<org>) in settings."
            )
        if not self.pat:
            raise AdoError(
                "Azure DevOps is not configured. Create a personal access "
                "token and store it as ado_pat in settings."
            )

    def headers(self, extra=None):
        token = base64.b64encode(f":{self.pat}".encode("utf-8")).decode("ascii")
        base = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        base.update(extra or {})
        return base

    def url(self, path, project=None, extra_query=None):
        base = self.org
        if project:
            base += "/" + urllib.parse.quote(str(project), safe="")
        url = f"{base}/_apis/{path.lstrip('/')}"
        params = [f"api-version={ADO_API_VERSION}"]
        if extra_query:
            params.insert(0, extra_query)
        return url + "?" + "&".join(params)

    def masked_credentials(self):
        return {"organisation": self.org, "pat": mask_secret(self.pat)}

    # -- discovery ------------------------------------------------------
    def list_projects(self):
        status, text = request_http(self.url("projects"), headers=self.headers())
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} listing projects: {payload.get('message', text[:200])}")
        names = [item.get("name") for item in payload.get("value", []) if item.get("name")]
        return {"status": status, "projects": names}

    def list_repositories(self, project=None):
        project = project or self.project
        if not project:
            raise AdoError("No project specified. Pass project or set ado_project in settings.")
        status, text = request_http(self.url("git/repositories", project), headers=self.headers())
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} listing repositories: {payload.get('message', text[:200])}")
        repos = [
            {"name": item.get("name"), "id": item.get("id"), "default_branch": item.get("defaultBranch")}
            for item in payload.get("value", [])
            if item.get("name")
        ]
        return {"project": project, "repositories": repos}

    def find_test_files(self, project=None, repo=None, limit=25):
        """Locate ``.xlsx`` test-case workbooks inside git repositories."""
        project = project or self.project
        if not project:
            raise AdoError("No project specified. Pass project or set ado_project in settings.")
        limit = max(1, min(int(limit or 25), MAX_TEST_FILE_LIMIT))
        if repo:
            repositories = [{"name": repo, "id": repo}]
        else:
            repositories = self.list_repositories(project)["repositories"]
        found = []
        for repository in repositories:
            if len(found) >= limit:
                break
            query = urllib.parse.urlencode(
                {"scopePath": "/", "recursionLevel": "Full", "api-version": ADO_API_VERSION}
            )
            item_url = f"{self.org}/{urllib.parse.quote(str(project), safe='')}/_apis/git/repositories/{urllib.parse.quote(str(repository['id']), safe='')}/items?{query}"
            status, text = request_http(item_url, headers=self.headers())
            payload = _decode_json(text)
            if status >= 400:
                continue
            for item in payload.get("value", []):
                if item.get("isFolder"):
                    continue
                path = str(item.get("path", ""))
                if not path.lower().endswith(".xlsx"):
                    continue
                found.append(
                    {
                        "repo": repository.get("name"),
                        "path": path,
                        "size_bytes": item.get("size"),
                    }
                )
                if len(found) >= limit:
                    break
        return {"project": project, "count": len(found), "test_files": found}

    def download_test_file(self, repo, path, project=None):
        """Download a workbook from a git repository and return its bytes."""
        project = project or self.project
        if not project:
            raise AdoError("No project specified. Pass project or set ado_project in settings.")
        query = urllib.parse.urlencode(
            {"scopePath": path, "recursionLevel": "None", "includeContent": "true", "api-version": ADO_API_VERSION}
        )
        download_url = (
            f"{self.org}/{urllib.parse.quote(str(project), safe='')}/_apis/git/repositories/"
            f"{urllib.parse.quote(str(repo), safe='')}/items?{query}"
        )
        headers = self.headers()
        headers["Accept"] = "application/octet-stream"
        status, payload = request_bytes(download_url, headers=headers)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} downloading {path}.")
        if not payload:
            raise AdoError(f"Azure DevOps returned an empty file for {path}.")
        return payload

    # -- test plan operations -------------------------------------------
    def list_test_plans(self, project=None):
        project = project or self.project
        if not project:
            raise AdoError("No project specified. Pass project or set ado_project in settings.")
        status, text = request_http(self.url("testplan/plans", project), headers=self.headers())
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} listing test plans: {payload.get('message', text[:200])}")
        plans = [
            {"id": item.get("id"), "name": item.get("name"), "state": item.get("state")}
            for item in payload.get("value", [])
            if item.get("id")
        ]
        return {"project": project, "test_plans": plans}

    def list_suites(self, plan_id, project=None):
        project = project or self.project
        status, text = request_http(
            self.url(f"testplan/plans/{int(plan_id)}/suites", project), headers=self.headers()
        )
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} listing suites: {payload.get('message', text[:200])}")
        suites = [
            {"id": item.get("id"), "name": item.get("name"), "suite_type": item.get("suiteType")}
            for item in payload.get("value", [])
            if item.get("id")
        ]
        return {"plan_id": int(plan_id), "suites": suites}

    def list_test_points(self, plan_id, suite_id, project=None, outcome=None):
        project = project or self.project
        status, text = request_http(
            self.url(
                f"testplan/plans/{int(plan_id)}/suites/{int(suite_id)}/testpoints",
                project,
                extra_query="includePointDetails=true",
            ),
            headers=self.headers(),
        )
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} listing test points: {payload.get('message', text[:200])}")
        points = []
        for item in payload.get("value", []):
            point = item.get("testPoint") or item
            results = (point.get("results") or {})
            entry = {
                "id": point.get("id"),
                "outcome": point.get("outcome") or results.get("outcome"),
                "state": point.get("state"),
                "test_case": (point.get("testCase") or {}).get("id") if isinstance(point.get("testCase"), dict) else point.get("testCase") or results.get("testCaseId"),
            }
            if entry["test_case"] is None:
                entry["test_case"] = results.get("testCaseTitle") or results.get("testPointTitle")
            if outcome and str(entry.get("outcome") or "").lower() != str(outcome).lower():
                continue
            points.append(entry)
        return {"plan_id": int(plan_id), "suite_id": int(suite_id), "test_points": points}

    def list_test_runs(self, project=None, top=25):
        project = project or self.project
        if not project:
            raise AdoError("No project specified. Pass project or set ado_project in settings.")
        status, text = request_http(
            self.url("test/runs", project, extra_query=f"$top={int(top or 25)}"),
            headers=self.headers(),
        )
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} listing test runs: {payload.get('message', text[:200])}")
        runs = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "state": item.get("state"),
                "result": item.get("result"),
                "is_automated": item.get("isAutomated"),
                "url": item.get("url"),
            }
            for item in payload.get("value", [])
            if item.get("id")
        ]
        return {"project": project, "test_runs": runs}

    def create_test_run(self, plan_id, point_ids, name=None, project=None):
        project = project or self.project
        body = {
            "name": name or "GRACE automated run",
            "plan": {"id": int(plan_id)},
            "pointIds": [int(pid) for pid in point_ids],
            "isAutomated": True,
            "state": "InProgress",
        }
        status, text = request_http(
            self.url("test/runs", project), method="POST", body=body, headers=self.headers()
        )
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} creating the test run: {payload.get('message', text[:200])}")
        return {
            "id": payload.get("id"),
            "name": payload.get("name"),
            "state": payload.get("state"),
            "web_url": payload.get("webAccessUrl") or payload.get("url"),
        }

    def publish_results(self, run_id, results, project=None):
        project = project or self.project
        if not results:
            raise AdoError("No results provided to publish.")
        published = 0
        for start in range(0, len(results), RESULTS_CHUNK_SIZE):
            chunk = results[start : start + RESULTS_CHUNK_SIZE]
            status, text = request_http(
                self.url(f"test/runs/{int(run_id)}/results", project),
                method="POST",
                body=chunk,
                headers=self.headers(),
            )
            payload = _decode_json(text)
            if status >= 400:
                raise AdoError(
                    f"Azure DevOps returned {status} publishing results: {payload.get('message', text[:200])}"
                )
            published += len(payload.get("value", chunk)) if isinstance(payload, dict) else len(chunk)
        return {"run_id": int(run_id), "results_published": published}

    def complete_run(self, run_id, project=None):
        project = project or self.project
        status, text = request_http(
            self.url(f"test/runs/{int(run_id)}", project),
            method="PATCH",
            body={"state": "Completed"},
            headers=self.headers(),
        )
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} completing the run: {payload.get('message', text[:200])}")
        return {"id": payload.get("id"), "state": payload.get("state")}

    def get_test_run(self, run_id, project=None):
        project = project or self.project
        status, text = request_http(
            self.url(f"test/runs/{int(run_id)}", project), headers=self.headers()
        )
        payload = _decode_json(text)
        if status >= 400:
            raise AdoError(f"Azure DevOps returned {status} reading run {run_id}.")
        return {
            "id": payload.get("id"),
            "name": payload.get("name"),
            "state": payload.get("state"),
            "passed": payload.get("passedTests"),
            "failed": payload.get("unanalyzedTests"),
            "total": payload.get("totalTests"),
        }

    # -- GRACE execution -------------------------------------------------
    def run_grace_test(self, file_name, file_bytes, env, browser, browser_version=None):
        fields = {"env": str(env), "browser": str(browser)}
        if browser_version:
            fields["browserVersion"] = str(browser_version)
        headers = {}
        if self.grace_token:
            headers["Authorization"] = f"Bearer {self.grace_token}"
        url = f"{self.grace_api}/api/test/run"
        status, text = _post_multipart(
            url,
            file_field="file",
            file_name=file_name,
            file_bytes=file_bytes,
            fields=fields,
            headers=headers,
        )
        payload = _decode_json(text)
        if status == 401:
            raise AdoError(
                "GRACE API rejected the request (401). Provide an Entra bearer "
                "token in ado_grace_token, or run GRACE locally without auth."
            )
        if status >= 400:
            raise AdoError(f"GRACE API returned {status}: {payload.get('message', text[:200])}")
        run_id = payload.get("testRunId", payload.get("id"))
        return {
            "test_run_id": run_id,
            "status": payload.get("status", "running"),
            "note": (
                "GRACE V1 reports results to Azure Test Plans itself (workbook "
                "Runtime Settings sheet). Poll with AdoListTestRuns to watch outcomes."
            ),
        }


def parse_xlsx_summary(file_bytes):
    """Peek inside an ``.xlsx`` (zip + XML) without external libraries."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(file_bytes))
    except (zipfile.BadZipFile, binascii.Error):
        raise AdoError("The file is not a valid .xlsx workbook.")
    with archive:
        names = archive.namelist()
        sheets = []
        unique = []
        grace_like = False
        if "xl/workbook.xml" in names:
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            for sheet in workbook.iter():
                if sheet.tag.rsplit("}", 1)[-1] == "sheet" and sheet.get("name"):
                    sheets.append(sheet.get("name"))
        if "xl/sharedStrings.xml" in names:
            try:
                tree = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            except ElementTree.ParseError:
                tree = None
            strings = []
            if tree is not None:
                for element in tree.iter():
                    if element.tag.rsplit("}", 1)[-1] == "t" and element.text:
                        strings.append(element.text.strip())
            unique = []
            for text in strings:
                if text and text not in unique:
                    unique.append(text)
            lowered = " ".join(strings).lower()
            grace_like = any(marker in lowered for marker in GRACE_MARKERS)
    return {"sheets": sheets, "preview_strings": unique[:40], "grace_like": grace_like}


def dispatch_tool(name, arguments, config, allow_sensitive=True, executor=None):
    """Route an ``Ado*`` tool call. Errors are returned, never raised."""
    arguments = arguments or {}
    client = AdoClient(config)
    try:
        client.require_config()
    except AdoError as err:
        return {"error": str(err)}

    try:
        if name == "AdoConnect":
            result = client.list_projects()
            if result["status"] >= 400:
                raise AdoError(
                    f"Azure DevOps returned {result['status']} — check ado_org and ado_pat."
                )
            return {
                "connected": True,
                "credentials": client.masked_credentials(),
                "projects": result["projects"],
                "grace_api": client.grace_api,
            }

        if name == "AdoListRepositories":
            return client.list_repositories(arguments.get("project"))

        if name == "AdoFindTestFiles":
            return client.find_test_files(
                project=arguments.get("project"),
                repo=arguments.get("repo"),
                limit=arguments.get("limit", 25),
            )

        if name == "AdoInspectTestFile":
            repo = arguments.get("repo")
            path = arguments.get("path")
            if not repo or not path:
                raise AdoError("AdoInspectTestFile requires 'repo' and 'path'.")
            file_bytes = client.download_test_file(repo, path, arguments.get("project"))
            if len(file_bytes) > MAX_WORKBOOK_BYTES:
                raise AdoError(
                    f"Workbook is {len(file_bytes)} bytes — larger than the "
                    f"{MAX_WORKBOOK_BYTES} byte inspection limit."
                )
            summary = parse_xlsx_summary(file_bytes)
            summary.update({"repo": repo, "path": path})
            return summary

        if name == "AdoListTestPlans":
            return client.list_test_plans(arguments.get("project"))

        if name == "AdoListSuites":
            plan_id = arguments.get("plan_id") or arguments.get("planId")
            if not plan_id:
                raise AdoError("AdoListSuites requires 'plan_id'.")
            return client.list_suites(plan_id, arguments.get("project"))

        if name == "AdoListTestPoints":
            plan_id = arguments.get("plan_id") or arguments.get("planId")
            suite_id = arguments.get("suite_id") or arguments.get("suiteId")
            if not plan_id or not suite_id:
                raise AdoError("AdoListTestPoints requires 'plan_id' and 'suite_id'.")
            return client.list_test_points(
                plan_id, suite_id, arguments.get("project"), arguments.get("outcome")
            )

        if name == "AdoListTestRuns":
            return client.list_test_runs(arguments.get("project"), arguments.get("top", 25))

        if name == "AdoRunGraceTest":
            if arguments.get("confirm") is not True:
                raise AdoError(
                    "AdoRunGraceTest executes a test. Re-issue with confirm=true "
                    "once the user approves."
                )
            env = arguments.get("env")
            browser = arguments.get("browser")
            if not env or not browser:
                raise AdoError("AdoRunGraceTest requires 'env' (e.g. EDCS-9) and 'browser'.")
            local_path = arguments.get("local_path")
            repo = arguments.get("repo")
            path = arguments.get("path")
            if local_path:
                file_path = pathlib.Path(local_path)
                if not file_path.exists():
                    raise AdoError(f"Local file not found: {local_path}")
                file_name = file_path.name
                file_bytes = file_path.read_bytes()
            elif repo and path:
                file_bytes = client.download_test_file(repo, path, arguments.get("project"))
                file_name = str(path).rsplit("/", 1)[-1]
            else:
                raise AdoError("AdoRunGraceTest needs either 'repo'+'path' or 'local_path'.")
            return client.run_grace_test(
                file_name,
                file_bytes,
                env,
                browser,
                arguments.get("browser_version"),
            )

        if name == "AdoPublishTestRun":
            if arguments.get("confirm") is not True:
                raise AdoError(
                    "AdoPublishTestRun writes an Azure Test Plans run. Re-issue "
                    "with confirm=true once the user approves."
                )
            plan_id = arguments.get("plan_id") or arguments.get("planId")
            point_ids = arguments.get("point_ids") or arguments.get("pointIds") or []
            results = arguments.get("results") or []
            if not plan_id or not point_ids or not results:
                raise AdoError(
                    "AdoPublishTestRun requires 'plan_id', 'point_ids' and 'results'."
                )
            prepared = []
            for entry in results:
                outcome = str(entry.get("outcome", "")).strip()
                if not outcome:
                    raise AdoError("Each result needs an 'outcome' (e.g. Passed).")
                if outcome not in TEST_OUTCOMES:
                    raise AdoError(
                        f"Outcome '{outcome}' is not valid. Use one of: "
                        + ", ".join(TEST_OUTCOMES)
                    )
                point_ref = entry.get("point_id") or entry.get("pointId") or entry.get("id")
                if point_ref is None:
                    raise AdoError("Each result needs the test point id it belongs to.")
                row = {
                    "testPoint": {"id": int(point_ref)},
                    "outcome": outcome,
                    "state": "Completed",
                }
                if entry.get("duration_ms") is not None:
                    row["durationInMs"] = int(entry["duration_ms"])
                if entry.get("comment"):
                    row["comment"] = str(entry["comment"])
                if entry.get("error"):
                    row["errorMessage"] = str(entry["error"])
                prepared.append(row)
            created = client.create_test_run(
                plan_id, point_ids, arguments.get("name"), arguments.get("project")
            )
            published = client.publish_results(created["id"], prepared, arguments.get("project"))
            completed = client.complete_run(created["id"], arguments.get("project"))
            verified = client.get_test_run(created["id"], arguments.get("project"))
            return {
                "run": created,
                "published": published,
                "state_after_close": completed,
                "verified": verified,
            }

        return {"error": f"Unknown ADO tool: {name}"}
    except AdoError as err:
        return {"error": str(err)}
    except Exception as err:  # pragma: no cover - defensive catch-all
        return {"error": f"ADO tool '{name}' failed: {err}"}