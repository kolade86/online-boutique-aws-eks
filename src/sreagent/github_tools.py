"""Read-only GitHub tools: deploy history of values-dev.yaml and repo file reads."""

import base64
import json
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


class GitHubReader:
    API = "https://api.github.com"

    def __init__(self, repo: str, token, branch: str, values_file: str,
                 timeout: float = 15.0, opener=urllib.request.urlopen):
        self.repo = repo
        self.branch = branch
        self.values_file = values_file
        self._token = token
        self._timeout = timeout
        self._open = opener

    def get(self, path: str, params=None):
        url = f"{self.API}/repos/{self.repo}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "sreagent"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            with self._open(urllib.request.Request(url, headers=headers),
                            timeout=self._timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise ToolError(f"GitHub HTTP {e.code} for {path}: {e.reason}") from None
        except urllib.error.URLError as e:
            raise ToolError(f"GitHub unreachable: {e.reason}") from None


def safe_path(path: str) -> str:
    """Repo-relative path only: no absolute paths, no '..' segments."""
    if not PATH.fullmatch(path) or ".." in path.split("/"):
        raise ToolError(f"Invalid repository path: {path!r}")
    return path


def make_tools(gh: GitHubReader) -> list[Tool]:
    def recent_values_commits(args):
        limit = bounded_int(args, "limit", 5, 1, 10)
        commits = gh.get("/commits", {"path": gh.values_file, "sha": gh.branch,
                                      "per_page": limit})
        out = []
        for c in commits:
            detail = gh.get(f"/commits/{c['sha']}")
            patch = next((f.get("patch", "") for f in detail.get("files", [])
                          if f["filename"] == gh.values_file), "")
            info = c["commit"]
            out.append(f"{c['sha'][:8]} {info['author']['date']} {info['author']['name']}: "
                       f"{info['message'].splitlines()[0]}\n{patch}")
        return "\n\n".join(out) or f"No commits touch {gh.values_file}."

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
        Tool("recent_values_commits",
             f"List the most recent commits on {gh.branch} that changed {gh.values_file} "
             "(each deploy is one of these), with the diff of that file. Use it to see "
             "which image tag was deployed when, and what changed just before a problem.",
             {"type": "object", "properties": {
                 "limit": {"type": "integer", "description": "Number of commits (1-10, default 5)"}}},
             recent_values_commits),
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
