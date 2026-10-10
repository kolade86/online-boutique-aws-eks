"""Read-only GitHub tools: recent chart commits (deploys and template changes) and repo file reads."""

import base64
import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request

from tools import Tool, ToolError, bounded_int, optional_str, require_str

PATH = re.compile(r"[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*")
REF = re.compile(r"[A-Za-z0-9_.\-/]{1,100}")
DEFAULT_FILE_LINES = 120
MAX_FILE_LINES = 250
MAX_SEARCH_HITS = 50
MAX_PATCH_CHARS = 1500   # per file per commit; template diffs can be long


class GitHubReader:
    API = "https://api.github.com"

    def __init__(self, repo: str, token, branch: str, values_file: str,
                 timeout: float = 15.0, opener=urllib.request.urlopen):
        self.repo = repo
        self.branch = branch
        self.values_file = values_file
        # The chart directory: values-dev.yaml's folder (helm/online-boutique)
        self.chart_path = posixpath.dirname(values_file)
        self._token = token
        self._timeout = timeout
        self._open = opener

    def get(self, path: str, params=None):
        return self.send("GET", path, params=params)

    def send(self, method: str, path: str, body=None, params=None):
        """One GitHub REST call. Writes (POST/PUT) are made only by pr_tool."""
        url = f"{self.API}/repos/{self.repo}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "sreagent"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with self._open(request, timeout=self._timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.load(e).get("message", "")
            except Exception:
                pass
            raise ToolError(f"GitHub HTTP {e.code} for {method} {path}: "
                            f"{detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise ToolError(f"GitHub unreachable: {e.reason}") from None


def safe_path(path: str) -> str:
    """Repo-relative path only: no absolute paths, no '..' segments."""
    if not PATH.fullmatch(path) or ".." in path.split("/"):
        raise ToolError(f"Invalid repository path: {path!r}")
    return path


def make_tools(gh: GitHubReader) -> list[Tool]:
    def recent_chart_commits(args):
        limit = bounded_int(args, "limit", 5, 1, 10)
        commits = gh.get("/commits", {"path": gh.chart_path, "sha": gh.branch,
                                      "per_page": limit})
        out = []
        for c in commits:
            detail = gh.get(f"/commits/{c['sha']}")
            info = c["commit"]
            lines = [f"{c['sha'][:8]} {info['author']['date']} {info['author']['name']}: "
                     f"{info['message'].splitlines()[0]}"]
            for f in detail.get("files", []):
                if not f["filename"].startswith(gh.chart_path + "/"):
                    continue
                patch = f.get("patch", "")
                if len(patch) > MAX_PATCH_CHARS:
                    patch = patch[:MAX_PATCH_CHARS] + f"\n... [patch cut, {len(patch)} characters]"
                lines.append(f"--- {f['filename']} ({f.get('status', 'modified')})\n{patch}")
            out.append("\n".join(lines))
        return "\n\n".join(out) or f"No commits touch {gh.chart_path}."

    def read_repo_file(args):
        path = safe_path(require_str(args, "path"))
        ref = optional_str(args, "ref", REF) or gh.branch
        item = gh.get(f"/contents/{urllib.parse.quote(path)}", {"ref": ref})
        if isinstance(item, list):  # a directory
            return "\n".join(f"{i['type']}: {i['path']}" for i in item)
        if item.get("encoding") != "base64":
            raise ToolError(f"{path} is too large or not a regular file")
        lines = base64.b64decode(item["content"]).decode("utf-8", errors="replace").splitlines()

        # Large files (values.yaml is ~450 lines) would be cut by output
        # truncation, so return numbered lines a window at a time, or search.
        search = args.get("search")
        if isinstance(search, str) and search.strip():
            hits = [n for n, line in enumerate(lines, 1) if search.lower() in line.lower()]
            if not hits:
                return f"{search!r} not found in {path} ({len(lines)} lines)."
            return (f"{len(hits)} line(s) matching {search!r} in {path} ({len(lines)} lines):\n"
                    + "\n".join(f"{n}: {lines[n - 1]}" for n in hits[:MAX_SEARCH_HITS]))
        start = bounded_int(args, "start_line", 1, 1, max(1, len(lines)))
        count = bounded_int(args, "max_lines", DEFAULT_FILE_LINES, 1, MAX_FILE_LINES)
        window = lines[start - 1:start - 1 + count]
        end = start - 1 + len(window)
        more = f"; read on with start_line={end + 1}" if end < len(lines) else ""
        return (f"{path} lines {start}-{end} of {len(lines)}{more}\n"
                + "\n".join(f"{n}: {line}" for n, line in enumerate(window, start)))

    return [
        Tool("recent_chart_commits",
             f"List the most recent commits on {gh.branch} that changed the Helm chart "
             f"({gh.chart_path}/), newest first, with the diff of each chart file. Deploys "
             f"(image tag changes in {gh.values_file}) and template changes both show up "
             "here. Use it to see what changed, and when, before or after a problem.",
             {"type": "object", "properties": {
                 "limit": {"type": "integer", "description": "Number of commits (1-10, default 5)"}}},
             recent_chart_commits),
        Tool("read_repo_file",
             "Read a file (or list a directory) from the application's Git repository, "
             "e.g. helm/online-boutique/values.yaml for the chart defaults. Returns "
             f"numbered lines, {DEFAULT_FILE_LINES} at a time. For a large file, first "
             "use search to find the line you need (e.g. search='  emailservice:'), "
             "then read from there with start_line.",
             {"type": "object", "properties": {
                 "path": {"type": "string", "description": "Repository-relative path"},
                 "ref": {"type": "string", "description": f"Branch, tag or commit (default {gh.branch})"},
                 "search": {"type": "string", "description": "Return only the lines containing this text (case-insensitive), with line numbers"},
                 "start_line": {"type": "integer", "description": "First line to return (default 1)"},
                 "max_lines": {"type": "integer", "description": f"Lines to return (default {DEFAULT_FILE_LINES}, max {MAX_FILE_LINES})"}},
              "required": ["path"]},
             read_repo_file),
    ]
