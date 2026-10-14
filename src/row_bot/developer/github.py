from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from row_bot.developer.executables import resolve_github_cli
from row_bot.developer.sandbox import ApprovalDecision, decide_action
from row_bot.developer.state import ApprovalMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GhStatus:
    installed: bool
    authenticated: bool
    version: str = ""
    user: str = ""
    message: str = ""
    path: str = ""


@dataclass(frozen=True)
class GhResult:
    ok: bool
    ran: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    url: str = ""
    decision: ApprovalDecision | None = None


def _run(args: list[str], *, cwd: str | None = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get_gh_status(timeout: int = 6) -> GhStatus:
    """Return local GitHub CLI status without requiring Row-Bot-specific auth."""
    gh_path = resolve_github_cli()
    if not gh_path:
        return GhStatus(False, False, message="GitHub CLI is not installed.")

    try:
        version_proc = _run([gh_path, "--version"], timeout=timeout)
    except Exception as exc:
        return GhStatus(True, False, message=f"Unable to run gh: {exc}", path=gh_path)

    version_line = (version_proc.stdout or version_proc.stderr).splitlines()
    version = version_line[0].strip() if version_line else "gh"

    try:
        auth_proc = _run([gh_path, "auth", "status", "-h", "github.com"], timeout=timeout)
    except Exception as exc:
        return GhStatus(True, False, version=version, message=f"Unable to check gh auth: {exc}", path=gh_path)

    auth_output = f"{auth_proc.stdout}\n{auth_proc.stderr}"
    authenticated = auth_proc.returncode == 0
    user = ""
    for line in auth_output.splitlines():
        stripped = line.strip()
        if "Logged in to github.com account" in stripped:
            user = stripped.rsplit(" ", 1)[-1].strip("()")
            break

    message = "Authenticated with GitHub CLI." if authenticated else "Run `gh auth login` to connect GitHub."
    return GhStatus(True, authenticated, version=version, user=user, message=message, path=gh_path)


def push_current_branch(
    workspace_path: str,
    approval_mode: ApprovalMode,
    *,
    confirmed: bool = False,
    timeout: int = 120,
) -> GhResult:
    """Push HEAD to origin after passing the thread approval mode."""
    decision = decide_action(approval_mode, "git_push")
    if decision.requires_approval and confirmed:
        decision = ApprovalDecision("allow", "User explicitly approved this push.")
    if not decision.allowed:
        return GhResult(False, False, stderr=decision.reason, decision=decision)

    root = Path(workspace_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace path does not exist: {root}")

    proc = _run(["git", "push", "-u", "origin", "HEAD"], cwd=str(root), timeout=timeout)
    return GhResult(
        ok=proc.returncode == 0,
        ran=True,
        stdout=proc.stdout[-20000:],
        stderr=proc.stderr[-20000:],
        returncode=proc.returncode,
        decision=decision,
    )


def create_pull_request(
    workspace_path: str,
    approval_mode: ApprovalMode,
    *,
    title: str = "",
    body: str = "",
    draft: bool = False,
    confirmed: bool = False,
    timeout: int = 120,
) -> GhResult:
    """Create a pull request through gh CLI after passing approval policy."""
    decision = decide_action(approval_mode, "git_pr")
    if decision.requires_approval and confirmed:
        decision = ApprovalDecision("allow", "User explicitly approved pull request creation.")
    if not decision.allowed:
        return GhResult(False, False, stderr=decision.reason, decision=decision)

    gh_path = resolve_github_cli()
    if not gh_path:
        return GhResult(False, False, stderr="GitHub CLI is not installed.", decision=decision)

    root = Path(workspace_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace path does not exist: {root}")

    args = [gh_path, "pr", "create"]
    if title:
        args.extend(["--title", title])
    body_file = ""
    if body:
        # A file, not an argument: a multi-line body survives Windows command shims and length limits (F20).
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(body)
            body_file = handle.name
        args.extend(["--body-file", body_file])
    if not title and not body:
        args.append("--fill")
    if draft:
        args.append("--draft")

    try:
        proc = _run(args, cwd=str(root), timeout=timeout)
    finally:
        if body_file:
            os.unlink(body_file)
    output = f"{proc.stdout}\n{proc.stderr}"
    url = ""
    for token in output.split():
        if token.startswith("https://github.com/") and "/pull/" in token:
            url = token.strip()
            break

    return GhResult(
        ok=proc.returncode == 0,
        ran=True,
        stdout=proc.stdout[-20000:],
        stderr=proc.stderr[-20000:],
        returncode=proc.returncode,
        url=url,
        decision=decision,
    )


# A pull request a branch opened through Row-Bot (F20 or the Git tab), so the Git tab shows it
# rather than an empty new-pull-request form (B336). Only GitHub pull request links are kept.
_PULL_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/([0-9]{1,9})$")
_PULL_LOCK = threading.Lock()
_PULL_LIMIT = 200


def _pull_requests_path() -> Path:
    from row_bot.developer import storage

    return Path(storage.DEVELOPER_DIR) / "pull_requests.json"


def _read_pull_requests() -> list[dict]:
    path = _pull_requests_path()
    try:
        rows = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, ValueError):
        logger.warning("Could not read the remembered pull requests", exc_info=True)
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def remember_pull_request(workspace_id: str, branch: str, url: str, *, title: str = "",
                          draft: bool = False) -> dict | None:
    """Remember the pull request this workspace's branch has on GitHub (B336)."""
    url = str(url or "").strip()
    match = _PULL_URL.match(url)
    if not workspace_id or not branch or not match:
        return None
    record = {"workspace_id": str(workspace_id), "branch": str(branch)[:256], "url": url,
              "title": str(title or "").strip()[:200], "draft": bool(draft),
              "opened_at": datetime.now().isoformat(timespec="seconds")}
    path = _pull_requests_path()
    with _PULL_LOCK:
        rows = [row for row in _read_pull_requests()
                if (row.get("workspace_id"), row.get("branch")) != (record["workspace_id"], record["branch"])]
        rows = [*rows, record][-_PULL_LIMIT:]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        temporary.replace(path)
    return branch_pull_request(workspace_id, branch)


def branch_pull_request(workspace_id: str, branch: str) -> dict | None:
    """The pull request remembered for this workspace's branch: number, title, link, draft, when (B336)."""
    if not workspace_id or not branch:
        return None
    for row in reversed(_read_pull_requests()):
        if row.get("workspace_id") != workspace_id or row.get("branch") != branch:
            continue
        match = _PULL_URL.match(str(row.get("url") or ""))
        if not match:
            return None
        return {"number": int(match.group(1)), "title": str(row.get("title") or "")[:200],
                "url": str(row["url"]), "draft": row.get("draft") is True,
                "opened_at": str(row.get("opened_at") or "")[:80]}
    return None


def view_branch_pull_request(workspace_path: str, branch: str, *, timeout: int = 30) -> dict | None:
    """The branch's open pull request as GitHub has it (``gh pr view``), or None.

    Only after an explicit, approved pull request action has already contacted GitHub:
    the Git tab itself never calls GitHub to read it.
    """
    gh_path = resolve_github_cli()
    if not gh_path or not branch:
        return None
    try:
        proc = _run([gh_path, "pr", "view", branch, "--json", "number,title,url,isDraft,state"],
                    cwd=str(Path(workspace_path).expanduser().resolve()), timeout=timeout)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(data, dict) or str(data.get("state") or "").upper() != "OPEN":
        return None
    url = str(data.get("url") or "").strip()
    if not _PULL_URL.match(url):
        return None
    return {"url": url, "title": str(data.get("title") or "")[:200], "draft": data.get("isDraft") is True}


def record_pull_request_result(workspace_id: str, workspace_path: str, branch: str, result: GhResult, *,
                               title: str, draft: bool) -> dict | None:
    """After ``gh pr create``: remember the pull request it opened, or the one the branch already had."""
    if not result.url:
        return None
    if result.ok:
        return remember_pull_request(workspace_id, branch, result.url, title=title, draft=draft)
    # "A pull request for this branch already exists": GitHub has the details.
    known = view_branch_pull_request(workspace_path, branch) or {"url": result.url}
    return remember_pull_request(workspace_id, branch, known["url"], title=known.get("title", ""),
                                 draft=bool(known.get("draft")))
