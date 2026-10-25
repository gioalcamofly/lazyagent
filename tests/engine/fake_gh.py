"""A fake ``gh`` for the GitHub Actions hub (engine/actions.py): canned
``gh api -i`` responses with ETags, 304s as gh prints them, jobs and
``gh run view --log-failed`` output. Never the real GitHub."""

from __future__ import annotations

import json
from typing import Any

SHA_A = "a" * 40
SHA_B = "b" * 40
CREATED = "2026-10-24T10:00:00Z"


def run_json(
    run_id: int, branch: str, sha: str, *, conclusion: str | None = "failure",
    status: str = "completed", workflow: str = "Tests", workflow_id: int = 1,
    created: str = CREATED, attempt: int = 1,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": workflow,
        "workflow_id": workflow_id,
        "head_sha": sha,
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "created_at": created,
        "run_attempt": attempt,
    }


def jobs_json(*failed: tuple[str, str], passed: tuple[str, ...] = ("lint",)) -> dict[str, Any]:
    jobs = [
        {"name": name, "conclusion": "failure", "html_url": f"https://github.com/o/r/job/{i}",
         "steps": [{"name": "Set up job", "conclusion": "success"},
                   {"name": step, "conclusion": "failure"}]}
        for i, (name, step) in enumerate(failed)
    ]
    jobs += [{"name": name, "conclusion": "success", "steps": []} for name in passed]
    return {"total_count": len(jobs), "jobs": jobs}


def http(status: int, body: Any = "", headers: dict[str, str] | None = None) -> tuple[int, str, str]:
    """``gh api -i`` output: exit code, stdout (status line, headers, body),
    stderr. A 304 exits 1 with ``gh: HTTP 304`` on stderr, as gh does."""
    reason = {200: "OK", 304: "Not Modified", 403: "Forbidden", 404: "Not Found",
              429: "Too Many Requests"}.get(status, "")
    head = [f"HTTP/2.0 {status} {reason}"] + [f"{k}: {v}" for k, v in (headers or {}).items()]
    text = body if isinstance(body, str) else json.dumps(body)
    out = "\r\n".join(head) + "\r\n\r\n" + text
    if status >= 300:
        return 1, out, f"gh: HTTP {status}"
    return 0, out, ""


class FakeGh:
    """``runs`` answers ``actions/runs`` with ETag ``etag``; an
    ``If-None-Match`` equal to it gets a 304. ``jobs[run_id]`` and
    ``logs[run_id]`` answer the rest; ``queue`` overrides the next calls."""

    def __init__(self, runs: list[dict[str, Any]] | None = None) -> None:
        self.runs = runs or []
        self.etag = '"e1"'
        self.jobs: dict[int, dict[str, Any]] = {}
        self.logs: dict[int, str] = {}
        self.calls: list[list[str]] = []
        self.queue: list[tuple[int, str, str] | Exception] = []
        # sha → its own runs (``actions/runs?head_sha=``), in pages of
        # ``per_page``, ETag ``"h-<sha[:4]>"``.
        self.head_runs: dict[str, list[dict[str, Any]]] = {}
        self.per_page = 100

    def set_runs(self, runs: list[dict[str, Any]], etag: str) -> None:
        self.runs, self.etag = runs, etag

    def runs_calls(self) -> list[list[str]]:
        """The repo's window calls (not a head's own)."""
        return [c for c in self.calls
                if any("actions/runs?" in a and "head_sha=" not in a for a in c)]

    def head_calls(self) -> list[list[str]]:
        return [c for c in self.calls if any("head_sha=" in a for a in c)]

    async def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if self.queue:
            nxt = self.queue.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if args[:2] == ["run", "view"]:
            log = self.logs.get(int(args[2]))
            if log is None:
                return 1, "", "failed to get run log: not found"
            return 0, log, ""
        path = args[-1]
        if "head_sha=" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            runs = self.head_runs.get(sha, [])
            page = int(path.split("page=")[-1]) if "&page=" in path else 1
            etag = f'"h-{sha[:4]}-{len(runs)}"'
            sent = next((a.split(": ", 1)[1] for a in args if a.startswith("If-None-Match")), None)
            if sent == etag:
                return http(304, "", {"ETag": etag})
            chunk = runs[(page - 1) * self.per_page: page * self.per_page]
            return http(200, {"total_count": len(runs), "workflow_runs": chunk}, {"ETag": etag})
        if "actions/runs?" in path:
            sent = next((a.split(": ", 1)[1] for a in args if a.startswith("If-None-Match")), None)
            if sent == self.etag:
                return http(304, "", {"ETag": self.etag})
            return http(200, {"total_count": len(self.runs), "workflow_runs": self.runs},
                        {"ETag": self.etag})
        if "/jobs" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            if run_id not in self.jobs:
                return http(404, {"message": "Not Found"})
            return http(200, self.jobs[run_id])
        return http(404, {"message": "Not Found"})


def failed_log(job: str, step: str, lines: list[str]) -> str:
    return "\n".join(f"{job}\t{step}\t2026-10-24T10:01:{i % 60:02d}.1234567Z {line}"
                     for i, line in enumerate(lines))
