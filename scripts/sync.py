#!/usr/bin/env python3
"""
LeetCode → GitHub Sync
Polls LeetCode for accepted submissions across all supported languages,
writes structured solution files, regenerates the README stats card,
and produces a commit message for the GitHub Actions workflow.

Required env vars (set as GitHub Secrets):
    LEETCODE_SESSION     — LEETCODE_SESSION cookie value
    LEETCODE_CSRF_TOKEN  — csrftoken cookie value

Optional env vars:
    LC_USERNAME          — LeetCode username   (default: Menchaca)
    LC_FETCH_LIMIT       — recent AC to check  (default: 20)
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

# ── Make generate_card importable regardless of working directory ─────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_card import generate_stats_card  # noqa: E402

# ─── Configuration ────────────────────────────────────────────────────────────
USERNAME:     str = os.environ.get("LC_USERNAME", "Menchaca")
FETCH_LIMIT:  int = int(os.environ.get("LC_FETCH_LIMIT", "20"))
GRAPHQL_URL:  str = "https://leetcode.com/graphql"
RATE_DELAY: float = 1.5          # seconds between API calls

# ─── Language Support ─────────────────────────────────────────────────────────
# Maps LeetCode's internal lang slug → (file extension, display name, comment style)
# Comment style: "block" = /* ... */   "hash" = # ...   "dash" = -- ...
LANG_MAP: dict[str, tuple[str, str, str]] = {
    "cpp":        (".cpp",   "C++",        "block"),
    "c":          (".c",     "C",          "block"),
    "java":       (".java",  "Java",       "block"),
    "python":     (".py",    "Python",     "hash"),
    "python3":    (".py",    "Python 3",   "hash"),
    "javascript": (".js",    "JavaScript", "block"),
    "typescript":  (".ts",    "TypeScript", "block"),
    "csharp":     (".cs",    "C#",         "block"),
    "go":         (".go",    "Go",         "block"),
    "rust":       (".rs",    "Rust",       "block"),
    "swift":      (".swift", "Swift",      "block"),
    "kotlin":     (".kt",    "Kotlin",     "block"),
    "ruby":       (".rb",    "Ruby",       "hash"),
    "scala":      (".scala", "Scala",      "block"),
    "php":        (".php",   "PHP",        "block"),
    "dart":       (".dart",  "Dart",       "block"),
    "racket":     (".rkt",   "Racket",     "hash"),
    "erlang":     (".erl",   "Erlang",     "hash"),
    "elixir":     (".ex",    "Elixir",     "hash"),
    "bash":       (".sh",    "Bash",       "hash"),
    "mysql":      (".sql",   "MySQL",      "dash"),
    "mssql":      (".sql",   "MS SQL",     "dash"),
    "oraclesql":  (".sql",   "Oracle SQL", "dash"),
    "postgresql":  (".sql",   "PostgreSQL", "dash"),
}

# Accept ALL languages — if LeetCode returns it, we sync it.
# Unknown slugs fall back to .txt with hash-style comments.
TARGET_LANGS: set | None = None

# Paths (relative to repo root — the workflow `cd`s there)
STATE_FILE      = Path(".sync-state.json")
SOLUTIONS_DIR   = Path("solutions")
ASSETS_DIR      = Path("assets")
COMMIT_MSG_FILE = Path(".commit-message.txt")

# State file schema version — bump when the shape changes
_STATE_VERSION = 2


# ─── HTML → Plain Text ───────────────────────────────────────────────────────
class _HTMLStripper(HTMLParser):
    """Converts LeetCode HTML problem descriptions to readable plain text."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("pre", "code"):
            self._parts.append("\n  ")
        elif tag == "li":
            self._parts.append("\n  - ")
        elif tag in ("p", "div"):
            self._parts.append("\n")
        elif tag == "br":
            self._parts.append("\n")
        elif tag == "sup":
            self._parts.append("^")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("pre", "code", "p", "div", "ul", "ol"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"\n{3,}", "\n\n", raw)        # collapse blank lines
        raw = re.sub(r"[ \t]+\n", "\n", raw)         # strip trailing spaces
        raw = re.sub(r"&nbsp;", " ", raw)             # leftover entities
        return raw.strip()


def strip_html(html: str) -> str:
    """Safely convert HTML to plain text; returns empty string on bad input."""
    if not html:
        return ""
    try:
        stripper = _HTMLStripper()
        stripper.feed(html)
        return stripper.get_text()
    except Exception:
        return "(description could not be parsed)"


# ─── State Management ─────────────────────────────────────────────────────────
_EMPTY_STATE: dict = {
    "version":    _STATE_VERSION,
    "synced_ids": [],       # submission ID strings already committed
    "problems":   {},       # titleSlug → metadata dict
    "tag_counts": {},       # topic tag name → solved count
    "last_run":   None,     # ISO-8601 timestamp
}


def load_state() -> dict:
    """Load persisted state; return a clean default if missing or corrupt."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            # Back-fill any keys added in newer versions
            for key, default in _EMPTY_STATE.items():
                data.setdefault(key, type(default)() if isinstance(default, (list, dict)) else default)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  WARN  State file unreadable ({exc}); starting fresh.")
    return json.loads(json.dumps(_EMPTY_STATE))  # deep copy


def save_state(state: dict) -> None:
    state["version"] = _STATE_VERSION
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ─── LeetCode Session ────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    """Build an authenticated requests.Session for the LeetCode GraphQL API."""
    lc_session = os.environ.get("LEETCODE_SESSION", "").strip()
    csrf_token = os.environ.get("LEETCODE_CSRF_TOKEN", "").strip()

    if not lc_session or not csrf_token:
        print("ERROR  LEETCODE_SESSION and/or LEETCODE_CSRF_TOKEN are not set.", file=sys.stderr)
        print("       Add them in GitHub → Settings → Secrets → Actions.", file=sys.stderr)
        sys.exit(1)

    sess = requests.Session()
    sess.cookies.set("LEETCODE_SESSION", lc_session, domain="leetcode.com")
    sess.cookies.set("csrftoken", csrf_token, domain="leetcode.com")
    sess.headers.update({
        "Content-Type": "application/json",
        "Referer":      "https://leetcode.com/",
        "x-csrftoken":  csrf_token,
        "User-Agent":   "Mozilla/5.0 (compatible; LeetCode-Sync/2.0)",
    })
    return sess


# ─── GraphQL Helper ───────────────────────────────────────────────────────────
def gql(session: requests.Session, query: str, variables: dict, label: str) -> dict | None:
    """Execute a GraphQL query with exponential-backoff retries (max 3)."""
    for attempt in range(3):
        try:
            resp = session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                timeout=30,
            )
            if resp.status_code == 403:
                print(f"  ERROR  403 Forbidden on [{label}] — session cookies are likely expired.")
                return None
            resp.raise_for_status()
            body = resp.json()
            if "errors" in body:
                print(f"  WARN  GraphQL error [{label}]: {body['errors']}")
                return None
            return body.get("data")
        except requests.RequestException as exc:
            wait = 2 ** (attempt + 1)
            print(f"  WARN  [{label}] attempt {attempt + 1}/3 failed: {exc}  (retry in {wait}s)")
            time.sleep(wait)

    print(f"  ERROR  All retries exhausted for [{label}].")
    return None


# ─── GraphQL Queries ──────────────────────────────────────────────────────────
Q_RECENT_AC = """
query recentAcSubmissionList($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
    lang
  }
}
"""

Q_SUBMISSION_DETAILS = """
query submissionDetails($submissionId: Int!) {
  submissionDetails(id: $submissionId) {
    runtime
    runtimePercentile
    memory
    memoryPercentile
    code
    lang { name verboseName }
    question {
      questionId
      title
      titleSlug
      difficulty
      content
      topicTags { name }
    }
  }
}
"""

Q_USER_STATS = """
query getUserStats($username: String!) {
  matchedUser(username: $username) {
    submitStats: submitStatsGlobal {
      acSubmissionNum { difficulty count }
    }
  }
  allQuestionsCount { difficulty count }
}
"""


# ─── API Wrappers ─────────────────────────────────────────────────────────────
def fetch_recent_ac(session: requests.Session) -> list[dict]:
    data = gql(session, Q_RECENT_AC, {"username": USERNAME, "limit": FETCH_LIMIT}, "recentAc")
    return (data or {}).get("recentAcSubmissionList") or []


def fetch_submission_details(session: requests.Session, sub_id: str) -> dict | None:
    data = gql(session, Q_SUBMISSION_DETAILS, {"submissionId": int(sub_id)}, f"details/{sub_id}")
    return (data or {}).get("submissionDetails")


def fetch_user_stats(session: requests.Session) -> dict | None:
    return gql(session, Q_USER_STATS, {"username": USERNAME}, "userStats")


# ─── Formatting ───────────────────────────────────────────────────────────────
def fmt_runtime(runtime, percentile) -> str:
    if runtime is None:
        return "N/A"
    rt = f"{runtime} ms" if isinstance(runtime, (int, float)) else str(runtime)
    if percentile is not None:
        rt += f"  (beats {float(percentile):.1f}%)"
    return rt


def fmt_memory(memory, percentile) -> str:
    if memory is None:
        return "N/A"
    if isinstance(memory, (int, float)):
        mb = memory
        if mb > 1_000_000:
            mb /= 1_048_576          # raw bytes → MB
        elif mb > 10_000:
            mb /= 1_024             # KB → MB
        mem = f"{mb:.1f} MB"
    else:
        mem = str(memory)
    if percentile is not None:
        mem += f"  (beats {float(percentile):.1f}%)"
    return mem


def wrap_description(text: str, width: int = 74, indent: str = "  ") -> str:
    """Word-wrap a plain-text description for the file header comment."""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            out.append("")
            continue
        if len(stripped) <= width:
            out.append(indent + stripped)
        else:
            out.append(textwrap.fill(
                stripped, width=width,
                initial_indent=indent,
                subsequent_indent=indent,
            ))
    if len(out) > 80:
        out = out[:78] + ["", f"{indent}[... truncated — see full problem at URL below]"]
    return "\n".join(out)


# ─── Solution File Builder ────────────────────────────────────────────────────
def _comment_wrap(text: str, style: str) -> str:
    """Wrap a multi-line string in the appropriate comment syntax."""
    if style == "block":
        return f"/*\n{text}\n*/\n"
    elif style == "dash":
        return "\n".join(f"-- {line}" if line.strip() else "--" for line in text.splitlines()) + "\n"
    else:  # hash
        return "\n".join(f"# {line}" if line.strip() else "#" for line in text.splitlines()) + "\n"


def _separator_line(style: str) -> str:
    """Return a visual separator comment for the 'Solution' section."""
    if style == "block":
        return "// ── Solution ────────────────────────────────────────────────────────────────"
    elif style == "dash":
        return "-- ── Solution ────────────────────────────────────────────────────────────────"
    else:
        return "# ── Solution ─────────────────────────────────────────────────────────────────"


def build_solution_file(details: dict, submitted_at: str, lang_slug: str) -> str:
    """Render a complete solution file with structured header and code."""
    q          = details.get("question") or {}
    q_id       = q.get("questionId", "?")
    title      = q.get("title", "Unknown")
    slug       = q.get("titleSlug", "unknown")
    difficulty = q.get("difficulty", "Unknown")
    tags       = [t["name"] for t in q.get("topicTags") or []]
    code       = (details.get("code") or "").rstrip()

    _, lang_name, comment_style = LANG_MAP.get(lang_slug, (".txt", lang_slug, "hash"))

    description = strip_html(q.get("content") or "")
    runtime_str = fmt_runtime(details.get("runtime"), details.get("runtimePercentile"))
    memory_str  = fmt_memory(details.get("memory"), details.get("memoryPercentile"))
    tag_str     = ", ".join(tags) if tags else "—"
    url         = f"https://leetcode.com/problems/{slug}/"

    header_body = f"""\
╔══════════════════════════════════════════════════════════════════════════════╗
║  LeetCode #{q_id}  ·  {title}
║  Difficulty : {difficulty}
║  Topics     : {tag_str}
╚══════════════════════════════════════════════════════════════════════════════╝

  Problem
  ─────────────────────────────────────────────────────────────────────────────
{wrap_description(description)}

  URL       : {url}

  Submission
  ─────────────────────────────────────────────────────────────────────────────
  Runtime   : {runtime_str}
  Memory    : {memory_str}
  Language  : {lang_name}
  Date      : {submitted_at}"""

    header = _comment_wrap(header_body, comment_style)
    separator = _separator_line(comment_style)

    return f"{header}\n{separator}\n\n{code}\n"


# ─── Write Solution ───────────────────────────────────────────────────────────
def write_solution(details: dict, timestamp: int, lang_slug: str, state: dict) -> tuple[Path, str]:
    """Write the solution file and update in-memory state. Returns (path, title)."""
    q          = details.get("question") or {}
    slug       = q.get("titleSlug", "unknown")
    difficulty = q.get("difficulty", "Easy")
    title      = q.get("title", "Unknown")
    tags       = [t["name"] for t in q.get("topicTags") or []]

    submitted_at = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    ext, lang_name, _ = LANG_MAP.get(lang_slug, (".txt", lang_slug, "hash"))

    folder = SOLUTIONS_DIR / difficulty
    folder.mkdir(parents=True, exist_ok=True)

    # Include language in filename to avoid collisions when same problem
    # is solved in multiple languages (e.g. two-sum.cpp and two-sum.py)
    filepath = folder / f"{slug}{ext}"
    filepath.write_text(build_solution_file(details, submitted_at, lang_slug), encoding="utf-8")

    # Track this problem
    is_new = slug not in state["problems"]
    state["problems"][slug] = {
        "title":      title,
        "difficulty": difficulty,
        "questionId": q.get("questionId"),
        "tags":       tags,
        "language":   lang_name,
        "date":       submitted_at,
    }
    if is_new:
        for tag in tags:
            state["tag_counts"][tag] = state["tag_counts"].get(tag, 0) + 1

    return filepath, title


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print("  LeetCode Sync v2.0")
    print("=" * 60)

    state   = load_state()
    session = make_session()

    # 1 ── Fetch recent accepted submissions ──────────────────────────────────
    print(f"\n  Fetching last {FETCH_LIMIT} accepted submissions for '{USERNAME}'...")
    recent = fetch_recent_ac(session)

    if not recent:
        print("  INFO   No submissions returned from LeetCode.")
        print("         Possible causes:")
        print("           - Cookies expired or incorrect")
        print("           - No accepted submissions on this account")
    else:
        print(f"  OK     {len(recent)} submission(s) retrieved.")
        # ── Diagnostic: show every submission the API returned ────────────
        print("\n  DEBUG  Raw submissions from API:")
        for s in recent:
            sid   = s.get("id", "?")
            stit  = s.get("title", "?")
            slang = s.get("lang", "?")
            sts   = s.get("timestamp", "?")
            in_synced = str(sid) in set(state.get("synced_ids") or [])
            lang_ok   = (TARGET_LANGS is None or slang in TARGET_LANGS)
            status    = "SKIP (already synced)" if in_synced else ("SKIP (lang filtered)" if not lang_ok else "NEW")
            print(f"           [{sid}] {stit}  lang={slang}  ts={sts}  → {status}")
        print()

    # 2 ── Filter to new supported-language submissions ─────────────────────
    synced = set(state.get("synced_ids") or [])
    new_subs = [
        s for s in recent
        if str(s["id"]) not in synced
        and (TARGET_LANGS is None or s.get("lang") in TARGET_LANGS)
    ]

    if not new_subs:
        print("\n  INFO   No new submissions to sync.")
    else:
        print(f"\n  FOUND  {len(new_subs)} new submission(s) to process:\n")

    # 3 ── Process each submission ────────────────────────────────────────────
    committed: list[str] = []

    for sub in new_subs:
        sub_id    = str(sub["id"])
        sub_title = sub.get("title", sub_id)
        sub_lang  = sub.get("lang", "unknown")
        _, lang_display, _ = LANG_MAP.get(sub_lang, (".txt", sub_lang, "hash"))
        print(f"    -> [{sub_id}] {sub_title}  ({lang_display})")

        time.sleep(RATE_DELAY)
        details = fetch_submission_details(session, sub_id)

        if not details:
            print(f"       SKIP  Could not fetch details (will retry next run).")
            continue

        filepath, title = write_solution(details, int(sub.get("timestamp") or 0), sub_lang, state)
        state["synced_ids"].append(sub_id)

        diff = (details.get("question") or {}).get("difficulty", "?")
        rt   = fmt_runtime(details.get("runtime"), details.get("runtimePercentile"))
        mem  = fmt_memory(details.get("memory"), details.get("memoryPercentile"))

        print(f"       OK    {filepath}")
        print(f"             Runtime: {rt}  |  Memory: {mem}")
        committed.append(f"{title} ({diff})")

    # 4 ── Save state ─────────────────────────────────────────────────────────
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print("\n  State saved.")

    # 5 ── Refresh stats card ─────────────────────────────────────────────────
    print("\n  Generating stats card...")
    time.sleep(RATE_DELAY)
    stats_data = fetch_user_stats(session)

    if stats_data:
        ASSETS_DIR.mkdir(exist_ok=True)
        svg = generate_stats_card(USERNAME, stats_data, state)
        card_path = ASSETS_DIR / "stats-card.svg"
        card_path.write_text(svg, encoding="utf-8")
        print(f"  OK     Stats card written -> {card_path}")
    else:
        print("  WARN   Could not fetch stats — card not updated this run.")

    # 6 ── Write commit message ───────────────────────────────────────────────
    if committed:
        n   = len(committed)
        msg = f"feat: add {n} solution{'s' if n != 1 else ''}\n\n"
        msg += "\n".join(f"  - {e}" for e in committed)
    else:
        msg = "chore: refresh stats card"
    COMMIT_MSG_FILE.write_text(msg, encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  Done. {len(committed)} new solution(s) synced.")
    print("=" * 60)


if __name__ == "__main__":
    main()
