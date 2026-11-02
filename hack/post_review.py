#!/usr/bin/env python3
# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Post a review the review agent wrote as a GitHub pull request review.

The agent runs in a VM that writes its output as a tar: the review under
review/, as summary.md and one file per inline comment under review/comments/,
each starting with front matter that gives its path and line. The agent runs
the PR's code, so its output is untrusted. This reads the tar's members in
memory, without extracting it, checks the review's shape and size, drops any
comment that fails a check, and always posts a COMMENT review: the reviewer
never approves or requests changes. GitHub rejects a whole review if one
comment targets a line outside the diff, so comments on such lines move into
the summary instead. The summary ends with a marker recording the commit the
review covered, which the next review reads to review only what's new. The
summary keeps to its 1,000 character limit by dropping later paragraphs. A
review from an agent that didn't exit cleanly may be incomplete, so it gets no
marker.

A re-review passes --since, the commit the last review covered. Its inline
comments may then only target lines the PR adds that also changed since that
commit, so a re-review can't reopen the parts of the PR it already reviewed.
Comments on other lines are dropped rather than moved into the summary.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import typing

# The bar decides how many comments a review has. This only stops a runaway.
MAX_COMMENTS = 25
MAX_SUMMARY = 20_000
MAX_COMMENT = 4_000
# The skill limits the summary to the verdict and why, under this many
# characters. Paragraphs past it are dropped.
MAX_SUMMARY_PROSE = 1_000
# GitHub limits a review body to 65,536 characters. This leaves room for the
# marker.
MAX_BODY = 65_000

# The skill aims for a comment's prose, outside any code block, to stay under
# this many characters. A comment over it is posted anyway, since a long
# comment beats a lost finding, but logged so the skill can be tuned.
TARGET_PROSE = 600

# The review's last line is a marker recording the commit it covered, and the
# next review reads it to scope itself. So the agent can't write one, the
# review escapes every HTML comment opener in what the agent wrote.
MARKER_FORMAT = "<!-- modelplane-review sha={sha} -->"
FRONT_MATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
CODE_BLOCK = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


class Comment(typing.NamedTuple):
    """An inline review comment."""

    path: str
    line: int
    body: str


class Review(typing.NamedTuple):
    """A review summary and its inline comments."""

    summary: str
    comments: list[Comment]
    # Whether the agent exited cleanly. A review from a run that didn't may be
    # incomplete.
    complete: bool


# Lines that start a new markdown block, rather than continuing a paragraph.
BLOCK_START = re.compile(r"^\s*([-*+] |\d+[.)] |>|#|\||<)")


def unwrap(text: str) -> str:
    """text with hard-wrapped paragraphs and list items joined into single lines.

    GitHub renders a newline in a review comment as a line break, so text the
    agent wrapped at 80 columns would show ragged. Code blocks keep their lines.
    """
    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        joinable = out and out[-1].strip() and not out[-1].lstrip().startswith("```")
        if not fenced and joinable and line.strip() and not BLOCK_START.match(line):
            out[-1] = out[-1].rstrip() + " " + line.strip()
            continue
        out.append(line)
    return "\n".join(out)


def unmark(text: str) -> str:
    """text with every HTML comment opener escaped, so it renders as written
    and can't pass for the review's marker."""
    return text.replace("<!--", "&lt;!--")


def parse_comment(raw: str) -> Comment:
    """The comment in raw, or ValueError if it isn't a well-formed comment
    file."""
    match = FRONT_MATTER.match(raw)
    if match is None:
        raise ValueError("comment has no front matter")
    meta = dict(line.split(":", 1) for line in match["meta"].splitlines() if ":" in line)
    body = unwrap(unmark(match["body"])).strip()
    path, line = unmark(meta.get("path", "")).strip(), meta.get("line", "").strip()
    if not path or not line.isdigit() or int(line) < 1 or not body:
        raise ValueError(f"comment needs a path, a line and a body: {match['meta']!r}")
    if len(body) > MAX_COMMENT:
        raise ValueError(f"comment on {path}:{line} is longer than {MAX_COMMENT} characters")
    if (n := len(CODE_BLOCK.sub("", body).strip())) > TARGET_PROSE:
        sys.stderr.write(f"comment on {path}:{line} has {n} characters of prose, over the {TARGET_PROSE} target\n")
    return Comment(path, int(line), body)


def parse(output: tarfile.TarFile) -> Review:
    """The review in the VM's output, or ValueError if it isn't the shape the
    agent was told to write."""
    files: dict[str, str] = {}
    complete = False
    for member in output.getmembers():
        name = member.name.removeprefix("./")
        if member.isfile() and name == "exit" and member.size < 16:  # noqa: PLR2004  # an exit status, a few bytes.
            f = output.extractfile(member)
            complete = f is not None and f.read().strip() == b"0"
            continue
        if not member.isfile() or not name.startswith("review/"):
            continue
        if member.size > MAX_SUMMARY:
            raise ValueError(f"{name} is larger than {MAX_SUMMARY} bytes")
        f = output.extractfile(member)
        if f is not None:
            files[name.removeprefix("review/")] = f.read().decode()
    summary = files.get("summary.md", "")
    if not summary.strip():
        raise ValueError("summary.md is missing or empty")
    if len(summary) > MAX_SUMMARY:
        raise ValueError(f"summary is longer than {MAX_SUMMARY} characters")
    comments = sorted(n for n in files if n.startswith("comments/") and n.endswith(".md"))
    if len(comments) > MAX_COMMENTS:
        raise ValueError(f"review has {len(comments)} comments, more than {MAX_COMMENTS}")
    # A comment that fails a check is dropped rather than losing the review.
    parsed = []
    for c in comments:
        try:
            parsed.append(parse_comment(files[c]))
        except ValueError as e:
            sys.stderr.write(f"dropping {c}: {e}\n")
    return Review(unwrap(unmark(summary)), parsed, complete)


def commentable(patch: str) -> set[int]:
    """The lines of a file's new version that a review comment can target: the
    added and context lines of its patch."""
    lines: set[int] = set()
    line = 0
    for text in patch.split("\n"):
        if match := HUNK.match(text):
            line = int(match["start"])
            continue
        if text.startswith("-") or text.startswith("\\"):
            continue
        lines.add(line)
        line += 1
    return lines


def added(patch: str) -> set[int]:
    """The lines of a file's new version that its patch adds."""
    lines: set[int] = set()
    line = 0
    for text in patch.split("\n"):
        if match := HUNK.match(text):
            line = int(match["start"])
            continue
        if text.startswith("-") or text.startswith("\\"):
            continue
        if text.startswith("+"):
            lines.add(line)
        line += 1
    return lines


def changed(diff: str) -> set[int]:
    """The lines of a file's new version that a zero-context git diff of that
    one file changes."""
    out: set[int] = set()
    for text in diff.split("\n"):
        if match := HUNK.match(text):
            start, count = int(match["start"]), int(match["count"] or 1)
            out.update(range(start, start + count))
    return out


def place(
    review: Review, diff: dict[str, set[int]], sha: str, scope: dict[str, set[int]] | None = None
) -> tuple[str, list[Comment]]:
    """The review body and the comments GitHub accepts inline. Without a scope,
    a comment on a line outside the diff moves into the body. With one, only
    comments on lines in the scope are posted, and the rest are dropped."""
    allowed = diff if scope is None else scope
    inline = [c for c in review.comments if c.line in allowed.get(c.path, set())]
    outside = [c for c in review.comments if c not in inline]
    body = ""
    for para in review.summary.strip().split("\n\n"):
        if body and len(body) + len(para) > MAX_SUMMARY_PROSE:
            sys.stderr.write(f"dropping a summary paragraph past {MAX_SUMMARY_PROSE} characters\n")
            continue
        body = f"{body}\n\n{para}" if body else para
    for c in outside if scope is None else []:
        moved = f"\n\n`{c.path}` line {c.line}: {c.body}"
        if len(body) + len(moved) > MAX_BODY:
            sys.stderr.write(f"dropping a comment on {c.path}:{c.line}, which doesn't fit in the review body\n")
            continue
        body += moved
    for c in outside if scope is not None else []:
        sys.stderr.write(f"dropping a comment on {c.path}:{c.line}, which hasn't changed since the last review\n")
    # Without the marker, the next review covers the whole PR rather than
    # skipping work an incomplete run never finished.
    if review.complete:
        body += "\n\n" + MARKER_FORMAT.format(sha=sha)
    else:
        sys.stderr.write("the agent didn't exit cleanly, so this review doesn't record what it covered\n")
    return body, inline


def gh(*args: str, stdin: str | None = None) -> str:
    """Run gh, returning its output."""
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def main() -> int:
    """Post the review."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--since", help="the commit the last review covered, for a re-review")
    parser.add_argument("--git-dir", type=pathlib.Path, help="a repository holding --sha and --since")
    parser.add_argument("output", type=pathlib.Path, help="the tar the review VM wrote")
    args = parser.parse_args()

    try:
        with tarfile.open(args.output) as output:
            review = parse(output)
    except (ValueError, tarfile.TarError, UnicodeDecodeError) as e:
        sys.stderr.write(f"invalid review: {e}\n")
        return 1

    base = gh("api", f"repos/{args.repo}/pulls/{args.pr}", "--jq", ".base.sha").strip()
    compare = json.loads(gh("api", f"repos/{args.repo}/compare/{base}...{args.sha}"))
    diff = {f["filename"]: commentable(f.get("patch", "")) for f in compare["files"]}

    scope = None
    if args.since:
        # One diff per file, so git never has to quote a path in a header.
        scope = {}
        for f in compare["files"]:
            since = subprocess.run(
                [
                    "git",
                    f"--git-dir={args.git_dir}",
                    "diff",
                    "-U0",
                    "--no-color",
                    "--no-ext-diff",
                    args.since,
                    args.sha,
                    "--",
                    f["filename"],
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            scope[f["filename"]] = added(f.get("patch", "")) & changed(since)

    body, inline = place(review, diff, args.sha, scope)
    payload = {
        "commit_id": args.sha,
        "event": "COMMENT",
        "body": body,
        "comments": [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in inline],
    }
    gh(
        "api",
        "--method",
        "POST",
        f"repos/{args.repo}/pulls/{args.pr}/reviews",
        "--input",
        "-",
        stdin=json.dumps(payload),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
