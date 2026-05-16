#!/usr/bin/env python3
"""Analyze GitHub/Git commits from the past week.

This script clones or updates a GitHub repository, collects commits from a time
window, estimates implementation time from commit activity, and reviews code
quality using deterministic heuristics. Optionally, it can send a carefully
structured prompt to Google's Gemini API for an LLM-assisted narrative review.

Requires Python 3.14.2+ and Git on PATH. The script uses only the Python
standard library and loads project defaults from a .env file when present.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


UTC = dt.UTC
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
}
TEST_HINTS = ("test", "tests", "spec", "__tests__")


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    authored_at: dt.datetime
    subject: str
    body: str
    files_changed: int
    insertions: int
    deletions: int
    diff: str
    changed_files: tuple[str, ...]

    @property
    def total_lines(self) -> int:
        return self.insertions + self.deletions


@dataclasses.dataclass(frozen=True)
class QualityFinding:
    severity: str
    title: str
    evidence: str
    recommendation: str


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed with exit {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def ensure_repo(source: str, workdir: Path, branch: str | None) -> tuple[Path, bool]:
    """Return a local repository path, cloning/pulling when a remote URL is given."""
    source_path = Path(source).expanduser()
    if source_path.exists():
        repo = source_path.resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"{repo} is not a Git repository")
        run_git(repo, "fetch", "--all", "--prune", check=False)
        if branch:
            run_git(repo, "checkout", branch)
        run_git(repo, "pull", "--ff-only", check=False)
        return repo, False

    workdir.mkdir(parents=True, exist_ok=True)
    repo_name = re.sub(r"\.git$", "", source.rstrip("/ ").split("/")[-1]) or "repo"
    repo = workdir / repo_name
    if repo.exists():
        run_git(repo, "fetch", "--all", "--prune")
        if branch:
            run_git(repo, "checkout", branch)
        run_git(repo, "pull", "--ff-only", check=False)
    else:
        clone_args = ["clone", "--filter=blob:none"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend([source, str(repo)])
        run_git(Path.cwd(), *clone_args)
    return repo, True


def parse_git_datetime(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.strip()).astimezone(UTC)


def collect_commits(
    repo: Path, since: dt.datetime, until: dt.datetime, author: str | None
) -> list[Commit]:
    pretty = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1e"
    args = [
        "log",
        f"--since={since.isoformat()}",
        f"--until={until.isoformat()}",
        f"--pretty=format:{pretty}",
    ]
    if author:
        args.append(f"--author={author}")
    raw = run_git(repo, *args)
    commits: list[Commit] = []
    for record in raw.strip("\x1e\n").split("\x1e"):
        if not record.strip():
            continue
        parts = record.strip("\n").split("\x1f", 5)
        if len(parts) != 6:
            continue
        sha, name, email, authored_at, subject, body = parts
        numstat = run_git(repo, "show", "--numstat", "--format=", sha)
        files_changed = insertions = deletions = 0
        changed_files: list[str] = []
        for line in numstat.splitlines():
            columns = line.split("\t")
            if len(columns) < 3:
                continue
            add, delete, path = columns[0], columns[1], columns[2]
            files_changed += 1
            changed_files.append(path)
            if add.isdigit():
                insertions += int(add)
            if delete.isdigit():
                deletions += int(delete)
        diff = run_git(repo, "show", "--format=", "--unified=3", "--no-ext-diff", sha)
        commits.append(
            Commit(
                sha=sha,
                author_name=name,
                author_email=email,
                authored_at=parse_git_datetime(authored_at),
                subject=subject.strip(),
                body=body.strip(),
                files_changed=files_changed,
                insertions=insertions,
                deletions=deletions,
                diff=diff,
                changed_files=tuple(changed_files),
            )
        )
    return sorted(commits, key=lambda commit: commit.authored_at)


def estimate_hours(commits: list[Commit]) -> dict[str, Any]:
    """Estimate effort from timestamp clusters and diff size.

    The estimate is intentionally conservative: it combines observed active time
    between nearby commits with a small line/file-size prior so single-commit days
    still receive a reasonable lower-bound estimate.
    """
    if not commits:
        return {
            "estimated_hours": 0.0,
            "active_span_hours": 0.0,
            "method": "No commits found.",
        }

    commits_by_day: dict[str, list[Commit]] = {}
    for commit in commits:
        commits_by_day.setdefault(commit.authored_at.date().isoformat(), []).append(commit)

    active_hours = 0.0
    for day_commits in commits_by_day.values():
        day_commits.sort(key=lambda item: item.authored_at)
        active_hours += 0.35  # setup/context time per active day
        previous = day_commits[0].authored_at
        for commit in day_commits[1:]:
            gap = (commit.authored_at - previous).total_seconds() / 3600
            active_hours += min(max(gap, 0.08), 2.0)
            previous = commit.authored_at

    code_lines = sum(commit.total_lines for commit in commits)
    files = sum(commit.files_changed for commit in commits)
    size_prior = min(18.0, (code_lines / 90.0) + (files * 0.08))
    estimated = max(active_hours, size_prior)
    return {
        "estimated_hours": round(estimated, 2),
        "active_span_hours": round(active_hours, 2),
        "size_prior_hours": round(size_prior, 2),
        "method": (
            "max(active timestamp clusters capped at 2h gaps, diff-size prior of "
            "~90 changed lines/hour plus 0.08h/file)"
        ),
    }


def analyze_quality(commits: list[Commit]) -> tuple[int, list[QualityFinding], dict[str, Any]]:
    findings: list[QualityFinding] = []
    total_lines = sum(commit.total_lines for commit in commits)
    total_files = sum(commit.files_changed for commit in commits)
    test_files = sum(
        1
        for commit in commits
        for path in commit.changed_files
        if any(hint in path.lower().split("/") for hint in TEST_HINTS)
        or "test" in Path(path).name.lower()
    )
    code_files = sum(
        1
        for commit in commits
        for path in commit.changed_files
        if Path(path).suffix in CODE_EXTENSIONS
    )

    for commit in commits:
        short_sha = commit.sha[:8]
        if commit.total_lines > 900 or commit.files_changed > 25:
            findings.append(
                QualityFinding(
                    "medium",
                    "Large commit is harder to review safely",
                    f"{short_sha} changes {commit.total_lines} lines across {commit.files_changed} files.",
                    "Split broad work into smaller topic commits or document review strategy in the PR.",
                )
            )
        if not any(path for path in commit.changed_files if "test" in path.lower()) and commit.total_lines > 120:
            findings.append(
                QualityFinding(
                    "medium",
                    "Substantial code change lacks visible test updates",
                    f"{short_sha} changes {commit.total_lines} lines, but no changed path looks like a test.",
                    "Add or update automated tests, or explain why existing coverage is sufficient.",
                )
            )
        added_lines = [
            line[1:]
            for line in commit.diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        todo_count = sum(1 for line in added_lines if re.search(r"(?i)\b(todo|fixme|hack)\b", line))
        if todo_count:
            findings.append(
                QualityFinding(
                    "low",
                    "New TODO/FIXME/HACK comments need tracking",
                    f"{short_sha} adds {todo_count} TODO-like comment(s).",
                    "Link each TODO to an issue or resolve it before merging.",
                )
            )
        secret_hits = sum(1 for line in added_lines for pattern in SECRET_PATTERNS if pattern.search(line))
        if secret_hits:
            findings.append(
                QualityFinding(
                    "high",
                    "Possible secret committed",
                    f"{short_sha} has {secret_hits} added line(s) matching secret-like patterns.",
                    "Remove the secret, rotate it, and use environment variables or a secret manager.",
                )
            )
        complex_added = sum(
            1
            for line in added_lines
            if re.search(r"\b(if|for|while|case|catch|except|elif|switch)\b", line)
        )
        if complex_added > 45:
            findings.append(
                QualityFinding(
                    "medium",
                    "High added branching complexity",
                    f"{short_sha} adds roughly {complex_added} branch/control-flow lines.",
                    "Consider extracting smaller functions and adding edge-case tests.",
                )
            )

    score = 100
    for finding in findings:
        score -= {"high": 18, "medium": 8, "low": 3}[finding.severity]
    if code_files and test_files / max(code_files, 1) < 0.15 and total_lines > 80:
        score -= 8
        findings.append(
            QualityFinding(
                "medium",
                "Low apparent test-change ratio",
                f"Detected {test_files} test-like file changes for {code_files} code file changes.",
                "Increase test coverage for the changed behavior or cite existing coverage in the PR.",
            )
        )
    metrics = {
        "commits": len(commits),
        "files_changed": total_files,
        "lines_changed": total_lines,
        "insertions": sum(commit.insertions for commit in commits),
        "deletions": sum(commit.deletions for commit in commits),
        "test_like_files_changed": test_files,
        "code_files_changed": code_files,
    }
    return max(0, min(100, score)), findings, metrics


def build_llm_prompt(
    repo: Path,
    commits: list[Commit],
    score: int,
    findings: list[QualityFinding],
    hours: dict[str, Any],
) -> str:
    commit_summaries = []
    for commit in commits:
        commit_summaries.append(
            {
                "sha": commit.sha[:12],
                "authored_at_utc": commit.authored_at.isoformat(),
                "subject": commit.subject,
                "files_changed": commit.files_changed,
                "insertions": commit.insertions,
                "deletions": commit.deletions,
                "changed_files": list(commit.changed_files[:40]),
            }
        )
    deterministic = {
        "repository": str(repo),
        "quality_score": score,
        "estimated_hours": hours,
        "heuristic_findings": [dataclasses.asdict(item) for item in findings],
        "commit_summaries": commit_summaries,
    }
    return textwrap.dedent(
        f"""
        You are a senior code reviewer and engineering productivity analyst.
        Analyze ONLY the evidence in the JSON below. Do not invent missing context.

        Accuracy rules:
        1. Separate facts from estimates. Label uncertain claims as estimates.
        2. Use commit timestamps only as activity evidence; do not claim they prove all work time.
        3. Calibrate effort using both timestamp clusters and diff size; mention that planning,
           debugging, code review, and uncommitted work may be absent from Git history.
        4. For code quality, prioritize reviewability, maintainability, tests, risk, security,
           and complexity. Avoid judging purely by lines changed.
        5. Cite the exact commit SHA(s) or changed file paths that support each observation.
        6. Provide practical next steps ranked by impact.
        7. If evidence is insufficient, say what extra data would improve the analysis.

        Return Markdown with these headings:
        - Executive Summary
        - Code Quality Assessment
        - Estimated Engineering Hours
        - Evidence and Caveats
        - Highest-Impact Recommendations

        Evidence JSON:
        {json.dumps(deterministic, indent=2)}
        """
    ).strip()


def call_gemini(prompt: str, model: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    payload = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
            },
        }
    ).encode()
    quoted_model = urllib.parse.quote(model, safe="")
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{quoted_model}:generateContent",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Gemini request failed: {exc.code} {detail}") from exc

    chunks: list[str] = []
    for candidate in data.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if text := part.get("text"):
                chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()

    prompt_feedback = data.get("promptFeedback")
    if prompt_feedback:
        raise RuntimeError(f"Gemini returned no text. promptFeedback={prompt_feedback}")
    raise RuntimeError("Gemini returned no text candidates")


def render_markdown(
    repo: Path,
    commits: list[Commit],
    score: int,
    findings: list[QualityFinding],
    metrics: dict[str, Any],
    hours: dict[str, Any],
    llm_text: str | None,
) -> str:
    lines = [
        f"# Weekly Commit Analysis for `{repo.name}`",
        "",
        "## Summary",
        f"- Commits analyzed: **{metrics['commits']}**",
        f"- Code quality score: **{score}/100**",
        f"- Estimated engineering hours: **{hours['estimated_hours']}**",
        f"- Files changed: **{metrics['files_changed']}**",
        f"- Lines changed: **{metrics['lines_changed']}** (+{metrics['insertions']} / -{metrics['deletions']})",
        "",
        "## Estimated Hours Method",
        f"- {hours['method']}",
        f"- Active timestamp cluster hours: {hours.get('active_span_hours', 0)}",
        f"- Diff-size prior hours: {hours.get('size_prior_hours', 0)}",
        "- Caveat: Git history cannot see thinking time, debugging outside commits, meetings, or uncommitted work.",
        "",
        "## Commits",
    ]
    if not commits:
        lines.append("- No commits found in the selected window.")
    for commit in commits:
        lines.append(
            f"- `{commit.sha[:12]}` {commit.authored_at.isoformat()} — {commit.subject} "
            f"({commit.files_changed} files, +{commit.insertions}/-{commit.deletions})"
        )
    lines.extend(["", "## Code Quality Findings"])
    if not findings:
        lines.append(
            "- No notable heuristic issues found. Review the actual diff before relying "
            "on this as a final quality judgment."
        )
    for finding in findings:
        lines.extend(
            [
                f"### {finding.title} ({finding.severity})",
                f"- Evidence: {finding.evidence}",
                f"- Recommendation: {finding.recommendation}",
            ]
        )
    if llm_text:
        lines.extend(["", "## LLM-Assisted Analysis", llm_text])
    return "\n".join(lines) + "\n"


def load_env_file(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE pairs from a .env file without overriding existing environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean value from the environment."""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze GitHub/Git commits from the past week.")
    parser.add_argument(
        "repo",
        nargs="?",
        default=os.environ.get("ANALYZER_REPO"),
        help="GitHub URL or local Git repository path (or set ANALYZER_REPO in .env)",
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("ANALYZER_BRANCH") or None,
        help="Branch to checkout before analysis (or ANALYZER_BRANCH)",
    )
    parser.add_argument(
        "--author",
        default=os.environ.get("ANALYZER_AUTHOR") or None,
        help="Git author pattern to filter, e.g. an email or name (or ANALYZER_AUTHOR)",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=float(os.environ.get("ANALYZER_DAYS", "7")),
        help="Lookback window in days (default: 7, or ANALYZER_DAYS)",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path(os.environ.get("ANALYZER_WORKDIR", str(Path(tempfile.gettempdir()) / "weekly-commit-analysis"))),
        help="Clone/update work directory (default: temp weekly-commit-analysis, or ANALYZER_WORKDIR)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("ANALYZER_OUTPUT", "weekly_commit_analysis.md")),
        help="Markdown report output path (default: weekly_commit_analysis.md, or ANALYZER_OUTPUT)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path(os.environ["ANALYZER_JSON_OUTPUT"]) if os.environ.get("ANALYZER_JSON_OUTPUT") else None,
        help="Optional path for machine-readable JSON (or ANALYZER_JSON_OUTPUT)",
    )
    parser.add_argument(
        "--use-gemini",
        action="store_true",
        default=env_bool("ANALYZER_USE_GEMINI"),
        help="Call Gemini API for narrative analysis (or set ANALYZER_USE_GEMINI=true)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        help="Gemini model for --use-gemini (default: gemini-2.5-flash, or GEMINI_MODEL)",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        default=env_bool("ANALYZER_PRINT_PROMPT"),
        help="Print the analysis prompt without calling Gemini (or ANALYZER_PRINT_PROMPT=true)",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("repo is required unless ANALYZER_REPO is set in .env or the environment")
    return args


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    if sys.version_info < (3, 14, 2):
        raise RuntimeError("Python 3.14.2 or newer is required")
    args = parse_args(argv or sys.argv[1:])
    if shutil.which("git") is None:
        raise RuntimeError("git is required but was not found on PATH")

    until = dt.datetime.now(UTC)
    since = until - dt.timedelta(days=args.days)
    repo, _cloned = ensure_repo(args.repo, args.workdir, args.branch)
    commits = collect_commits(repo, since, until, args.author)
    score, findings, metrics = analyze_quality(commits)
    hours = estimate_hours(commits)
    prompt = build_llm_prompt(repo, commits, score, findings, hours)

    llm_text = None
    if args.print_prompt:
        print(prompt)
    if args.use_gemini:
        llm_text = call_gemini(prompt, args.model)

    report = render_markdown(repo, commits, score, findings, metrics, hours, llm_text)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {
                    "repository": str(repo),
                    "window": {"since": since.isoformat(), "until": until.isoformat()},
                    "metrics": metrics,
                    "quality_score": score,
                    "findings": [dataclasses.asdict(item) for item in findings],
                    "hours": hours,
                    "commits": [
                        {
                            "sha": commit.sha,
                            "author_name": commit.author_name,
                            "author_email": commit.author_email,
                            "authored_at": commit.authored_at.isoformat(),
                            "subject": commit.subject,
                            "files_changed": commit.files_changed,
                            "insertions": commit.insertions,
                            "deletions": commit.deletions,
                            "changed_files": list(commit.changed_files),
                        }
                        for commit in commits
                    ],
                    "llm_prompt": prompt,
                    "llm_analysis": llm_text,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    print(f"Wrote report to {args.output}")
    if args.json_output:
        print(f"Wrote JSON to {args.json_output}")
    print(
        f"Analyzed {metrics['commits']} commits; "
        f"score={score}/100; hours={hours['estimated_hours']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
