"""
Built-in tool implementations and OpenAI-compatible schemas.

Structure
---------
  PRIMITIVE_TOOLS   — run_command / read_file / write_file / list_files (always available)
  SKILL_TOOLS       — use_skill / list_skill_resources (always available)
  META_SKILL_TOOLS  — create_skill (always available — "god mode" skill creation)
  MEMORY_TOOLS      — remember / recall (always available)
  WEB_SEARCH_TOOL   — web_search (only when Tavily API key is configured)
  KNOWLEDGE_TOOL    — consult_knowledge_base (only when a RAG index is loaded)
  CRON_TOOLS        — cron_add / cron_remove / cron_list (only when CronScheduler is injected)

Agent._build_tools() assembles the right subset per session.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger(__name__)


# ── Virtual-environment detection ─────────────────────────────────────────────

_venv_dir: str | None = None


def _detect_venv() -> str | None:
    """Find the project's virtual environment directory.

    Priority:
      1. Already running inside a venv (sys.prefix != sys.base_prefix)
      2. .venv/ in CWD
      3. venv/ in CWD
    """
    if sys.prefix != sys.base_prefix:
        return sys.prefix

    for name in (".venv", "venv"):
        candidate = os.path.join(os.getcwd(), name)
        python = os.path.join(candidate, "bin", "python")
        if os.path.isfile(python):
            return candidate

    return None


def _venv_python() -> str:
    """Return the Python executable inside the detected venv, or sys.executable."""
    venv = _venv_dir or _detect_venv()
    if venv:
        candidate = os.path.join(venv, "bin", "python")
        if os.path.isfile(candidate):
            return candidate
    return sys.executable


def _venv_env() -> dict[str, str]:
    """Build an env dict that activates the project venv for subprocesses."""
    env = os.environ.copy()
    venv = _venv_dir or _detect_venv()
    if venv:
        venv_bin = os.path.join(venv, "bin")
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env.pop("PYTHONHOME", None)
    else:
        python_dir = os.path.dirname(sys.executable)
        env["PATH"] = f"{python_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def configure_venv(venv_dir: str | None = None) -> str | None:
    """Explicitly set or auto-detect the venv. Called by Agent.__init__."""
    global _venv_dir
    if venv_dir:
        _venv_dir = os.path.realpath(venv_dir)
    else:
        _venv_dir = _detect_venv()
    if _venv_dir:
        logger.info("[tools] Using venv: %s", _venv_dir)
    return _venv_dir


# ── Sandbox (path restriction) ───────────────────────────────────────────────

_sandbox_roots: list[str] = []
_READ_SAFE_ROOTS: list[str] | None = None

_DAILY_MEMORY_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_SECRET_CONTENT_RE = re.compile(
    r"(?i)(?:"
    r"\b(api[_-]?key|token|secret|password|bearer)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{8,}"
    r"|sk-[A-Za-z0-9_\-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{20,}"
    r"|\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"
    r"|gh[pousr]_[A-Za-z0-9_]{30,}"
    r"|AKIA[0-9A-Z]{16}"
    r")"
)
_MAX_SAFE_SEND_FILE_BYTES = 2 * 1024 * 1024


def set_sandbox(roots: list[str]) -> None:
    """Configure the allowed root directories for file-write operations.

    Called by Agent.__init__ to restrict write_file / create_skill to the
    project's working tree.  An empty list disables sandboxing (not recommended).
    """
    _sandbox_roots.clear()
    for r in roots:
        _sandbox_roots.append(os.path.realpath(r))


def _resolve_in_sandbox(path: str) -> str:
    """Resolve *path* to an absolute real path and verify it lives inside the sandbox.

    Returns the resolved path on success.
    Raises ``PermissionError`` if the path escapes every sandbox root.
    """
    resolved = os.path.realpath(os.path.abspath(path))

    if not _sandbox_roots:
        return resolved

    for root in _sandbox_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved

    raise PermissionError(
        f"Path '{path}' (resolved to '{resolved}') is outside the allowed directories: "
        + ", ".join(_sandbox_roots)
    )


def _read_safe_roots() -> list[str]:
    """Return the Phase 8 allowlist for LLM-readable files.

    Skills must remain readable because skill resources are part of normal
    agent execution. Raw memory, logs, sessions, and config are intentionally
    excluded from this allowlist.
    """
    global _READ_SAFE_ROOTS
    if _READ_SAFE_ROOTS is not None:
        return _READ_SAFE_ROOTS

    from .. import config as _cfg

    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [
        os.path.join(package_root, "templates", "skills"),
        os.path.join(str(_cfg.PYTHONCLAW_HOME), "context", "skills"),
        str(_cfg.files_dir()),
    ]
    _READ_SAFE_ROOTS = [os.path.realpath(root) for root in roots if os.path.isdir(root)]
    return _READ_SAFE_ROOTS


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _block_reason_for_read(path: str) -> str | None:
    """Return a clear block reason, or None when the read is allowed."""
    from .. import config as _cfg

    resolved = os.path.realpath(os.path.abspath(path))
    name = os.path.basename(resolved)
    lower_name = name.lower()
    lower_path = resolved.lower()
    home = os.path.realpath(str(_cfg.PYTHONCLAW_HOME))
    context = os.path.join(home, "context")
    memory_dir = os.path.join(context, "memory")
    logs_dir = os.path.join(context, "logs")
    sessions_dir = os.path.join(context, "sessions")
    compaction_dir = os.path.join(context, "compaction")

    if lower_name == ".env" or lower_name.endswith(".env") or ".env." in lower_name:
        return ".env files are not exposed to LLM tools."
    if resolved == os.path.realpath(str(_cfg.PYTHONCLAW_HOME / "pythonclaw.json")):
        return "Jarvis configuration may contain API keys or tokens."
    if any(term in lower_name for term in ("apikey", "api_key", "token", "secret", "password")):
        return "Files named like secrets, tokens, or API keys are not exposed."
    if _is_under(resolved, memory_dir):
        if _DAILY_MEMORY_LOG_RE.match(name):
            return "Daily memory logs are raw change logs and are not exposed to LLM tools."
        return "Raw legacy memory files are not exposed; use safe personalization context."
    if _is_under(resolved, logs_dir) or name == "history_detail.jsonl":
        return "Raw interaction logs and tool-call logs are not exposed to LLM tools."
    if _is_under(resolved, sessions_dir):
        return "Raw session transcripts are not exposed to LLM tools."
    if _is_under(resolved, compaction_dir):
        return "Compaction audit logs are not exposed to LLM tools."
    if lower_path.endswith("history_detail.jsonl"):
        return "history_detail.jsonl is not exposed to LLM tools."
    if not any(_is_under(resolved, root) for root in _read_safe_roots()):
        return "Path is outside the LLM-readable allowlist."
    return None


def _contains_secret_content(path: str) -> bool:
    """Scan allowed text files for obvious secret material before exposure."""
    try:
        size = os.path.getsize(path)
        if size > 2 * 1024 * 1024:
            return False
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return bool(_SECRET_CONTENT_RE.search(f.read()))
    except OSError:
        return False


def _shell_command_enabled() -> bool:
    """Return whether the unsafe legacy shell tool is explicitly enabled."""
    from .. import config as _cfg
    return _cfg.get_bool(
        "tools",
        "allow_shell_command",
        env="PYTHONCLAW_ALLOW_SHELL_COMMAND",
        default=False,
    )


def _sanitize_filename(name: str) -> str:
    """Strip path separators and '..' segments from a filename."""
    name = name.replace("..", "").replace("/", "").replace("\\", "")
    if not name:
        raise ValueError("Empty or invalid filename after sanitization.")
    return name


# ── Primitive tool implementations ────────────────────────────────────────────

def _files_dir() -> str:
    """Return the shared files directory, creating it if needed."""
    from .. import config as _cfg
    return str(_cfg.files_dir())


def run_command(command: str) -> str:
    """Execute a shell command only when explicitly enabled for debugging.

    Phase 8 privacy treats unrestricted shell as outside the LLM security
    boundary. It can read config, logs, memory, and secrets through many paths
    (python -c, cp, tar, dd, base64, nested sh, etc.), so production defaults
    to disabled. Prefer skill-specific Python helpers over shell.
    """
    try:
        if not _shell_command_enabled():
            return (
                "Blocked: run_command is disabled by default for Phase 8 "
                "privacy. Use approved skill helpers or set "
                "tools.allow_shell_command=true only in a trusted debug environment."
            )
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=60, env=_venv_env(), cwd=_files_dir(),
        )
        return result.stdout if result.returncode == 0 else f"Error (exit {result.returncode}):\n{result.stderr}"
    except Exception as exc:
        return f"Execution error: {exc}"


def cnyaud_monitor_data(
    action: str,
    period: str = "90d",
    threshold: float = 0.3,
    no_mark_seen: bool = False,
) -> str:
    """Run approved CNY/AUD monitor helpers without exposing a shell."""
    scripts = {
        "fetch_rate": "fetch_rate.py",
        "news_monitor": "news_monitor.py",
        "monitor_alert": "monitor_alert.py",
    }
    script_name = scripts.get(str(action or "").strip())
    if not script_name:
        return "Error: action must be one of fetch_rate, news_monitor, monitor_alert."

    skill_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "skills", "data", "cnyaud_monitor",
    )
    script = os.path.join(skill_dir, script_name)
    if not os.path.isfile(script):
        return f"Error: approved helper not found: {script_name}"

    cmd = [sys.executable, script, "--format", "json"]
    if action == "fetch_rate":
        if period not in {"7d", "30d", "90d", "1y"}:
            return "Error: period must be one of 7d, 30d, 90d, 1y."
        cmd.extend(["--period", period])
    elif action == "monitor_alert":
        cmd.extend(["--threshold", str(float(threshold))])
    elif action == "news_monitor" and no_mark_seen:
        cmd.append("--no-mark-seen")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=_venv_env(),
            cwd=_files_dir(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f"Error (exit {result.returncode}):\n{result.stderr.strip()}"
    except Exception as exc:
        return f"Execution error: {exc}"


def read_file(path: str) -> str:
    """Read and return a file from the Phase 8 safe allowlist."""
    try:
        if not os.path.exists(path):
            return f"Error: '{path}' not found."
        block_reason = _block_reason_for_read(path)
        if block_reason:
            return f"Blocked: {block_reason}"
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if _SECRET_CONTENT_RE.search(content):
            return "Blocked: file content appears to contain API keys, tokens, or secrets."
        return content
    except Exception as exc:
        return f"Read error: {exc}"


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.

    Writes are restricted to sandbox directories (configured via set_sandbox).
    """
    try:
        resolved = _resolve_in_sandbox(path)
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}"
    except PermissionError as exc:
        return f"Blocked: {exc}"
    except Exception as exc:
        return f"Write error: {exc}"


def list_files(path: str | None = None) -> str:
    """List files only inside the Phase 8 readable allowlist."""
    try:
        path = path or _files_dir()
        resolved = os.path.realpath(os.path.abspath(path))
        if not any(_is_under(resolved, root) for root in _read_safe_roots()):
            return "Blocked: path is outside the LLM-readable allowlist."
        entries = []
        for name in sorted(os.listdir(resolved)):
            full = os.path.join(resolved, name)
            if _block_reason_for_read(full):
                continue
            entries.append(name)
        return "\n".join(entries)
    except Exception as exc:
        return f"List error: {exc}"


_MAX_SEND_FILE_BYTES = 100 * 1024 * 1024  # transport hard cap

# Channel-provided callback: send_file_fn(path, caption) → None
_file_sender: callable | None = None


def set_file_sender(fn: callable | None) -> None:
    """Register a callback for sending files to the current channel."""
    global _file_sender
    _file_sender = fn


def send_file(path: str, caption: str = "") -> str:
    """Send a file to the user via the active channel (Telegram/Discord/WhatsApp/Web)."""
    resolved = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(resolved):
        return f"Error: file not found: {path}"
    block_reason = _block_reason_for_read(resolved)
    if block_reason:
        return f"Blocked: {block_reason}"
    size = os.path.getsize(resolved)
    if size > _MAX_SAFE_SEND_FILE_BYTES:
        size_mb = size / (1024 * 1024)
        return (
            f"Blocked: file is too large for safe secret scanning "
            f"({size_mb:.1f} MB)."
        )
    if _contains_secret_content(resolved):
        return "Blocked: file content appears to contain API keys, tokens, or secrets."

    if size > _MAX_SEND_FILE_BYTES:
        size_mb = size / (1024 * 1024)
        return f"Error: file too large ({size_mb:.1f} MB). Maximum allowed is 100 MB."

    if _file_sender is None:
        name = os.path.basename(resolved)
        return (
            f"File ready: {name} ({size / 1024:.1f} KB). "
            "No active channel to send through — user can download it directly."
        )

    try:
        _file_sender(resolved, caption)
        name = os.path.basename(resolved)
        return f"File '{name}' ({size / 1024:.1f} KB) sent successfully."
    except Exception as exc:
        return f"Error sending file: {exc}"


AVAILABLE_TOOLS: dict[str, callable] = {
    "run_command": run_command,
    "cnyaud_monitor_data": cnyaud_monitor_data,
    "read_file": read_file,
    "write_file": write_file,
    "list_files": list_files,
    "send_file": send_file,
}



# ── Schema helpers ────────────────────────────────────────────────────────────

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ── Primitive tool schemas ────────────────────────────────────────────────────



PRIMITIVE_TOOLS: list[dict] = [
    _fn(
        "cnyaud_monitor_data",
        (
            "Approved CNY/AUD monitor helper. Use this instead of shell commands "
            "to fetch exchange-rate JSON, check RSS news, or run threshold checks."
        ),
        {
            "action": {
                "type": "string",
                "enum": ["fetch_rate", "news_monitor", "monitor_alert"],
                "description": "Which approved monitor helper to run.",
            },
            "period": {
                "type": "string",
                "enum": ["7d", "30d", "90d", "1y"],
                "description": "History period for fetch_rate.",
                "default": "90d",
            },
            "threshold": {
                "type": "number",
                "description": "Percentage threshold for monitor_alert.",
                "default": 0.3,
            },
            "no_mark_seen": {
                "type": "boolean",
                "description": "For news_monitor, avoid marking RSS articles as seen.",
                "default": False,
            },
        },
        ["action"],
    ),
    _fn(
        "run_command",
        (
            "Legacy shell command tool. Disabled by default for Phase 8 privacy; "
            "use approved skill helpers instead."
        ),
        {"command": {"type": "string", "description": "The shell command to execute."}},
        ["command"],
    ),
    _fn(
        "read_file",
        (
            "Read a safe file from the allowlist. Intended for skill resources "
            "and shared generated files; config, secrets, raw memory, sessions, "
            "and logs are blocked."
        ),
        {"path": {"type": "string", "description": "Path to the file."}},
        ["path"],
    ),
    _fn(
        "write_file",
        "Write content to a file (must be within the project directory). Creates parent directories automatically.",
        {
            "path": {"type": "string", "description": "Path to the file to write (must be within project root)."},
            "content": {"type": "string", "description": "The content to write."},
        },
        ["path", "content"],
    ),
    _fn(
        "list_files",
        "List files in a directory. Use to discover available scripts or files.",
        {"path": {"type": "string", "description": "Directory path inside the readable allowlist. Defaults to the shared files directory."}},
        [],
    ),
    _fn(
        "send_file",
        "Send a file to the user via the active channel. Max 100 MB. Use when the user asks to download or receive a file.",
        {
            "path": {"type": "string", "description": "Absolute or relative path to the file to send."},
            "caption": {"type": "string", "description": "Optional caption or description for the file.", "default": ""},
        },
        ["path"],
    ),
]


# ── Skill tool schemas ───────────────────────────────────────────────────────
# Level 2: Agent triggers a skill to load its full instructions into context.
# Level 3: Agent reads/runs bundled resources via read_file / run_command.

SKILL_TOOLS: list[dict] = [
    _fn(
        "use_skill",
        (
            "Activate a skill by name. "
            "This loads the skill's detailed instructions and workflow into context. "
            "Only call this when you've identified the right skill from the catalog "
            "in the system prompt."
        ),
        {"skill_name": {"type": "string", "description": "Exact skill name from the catalog."}},
        ["skill_name"],
    ),
    _fn(
        "list_skill_resources",
        (
            "List resource files bundled with a skill (scripts, schemas, reference docs). "
            "Use after activating a skill to discover what files are available."
        ),
        {"skill_name": {"type": "string", "description": "Name of the activated skill."}},
        ["skill_name"],
    ),
]


# ── Memory tool schemas ──────────────────────────────────────────────────────

MEMORY_TOOLS: list[dict] = [
    _fn(
        "remember",
        "Store a piece of information in long-term memory.",
        {
            "key": {"type": "string", "description": "Topic or category to store under."},
            "content": {"type": "string", "description": "The information to remember."},
        },
        ["key", "content"],
    ),
    _fn(
        "recall",
        (
            "Search safe personalization memory using semantic + keyword retrieval. "
            "Pass a descriptive query to get relevant whitelisted fields. "
            "Use query='*' to retrieve the safe personalization context."
        ),
        {"query": {"type": "string", "description": "Topic or question to search memory for. Use '*' for all memories."}},
        ["query"],
    ),
    _fn(
        "memory_get",
        (
            "Return the safe Phase 8 personalization context. "
            "Raw memory files and daily logs are not exposed."
        ),
        {"path": {"type": "string", "description": "Optional; use 'safe_personalization_context'."}},
        [],
    ),
    _fn(
        "memory_list_files",
        "List safe memory tool targets. Raw memory files and daily logs are hidden.",
        {},
        [],
    ),
    _fn(
        "forget",
        "Delete a memory entry by key from long-term memory.",
        {"key": {"type": "string", "description": "The key to remove from memory."}},
        ["key"],
    ),
    _fn(
        "update_index",
        (
            "Update the INDEX.md system info file. "
            "Use this to store curated environment info, "
            "API notes, and configuration that should "
            "persist across sessions."
        ),
        {
            "content": {
                "type": "string",
                "description": "Full Markdown content for INDEX.md.",
            },
        },
        ["content"],
    ),
]


# ── Web search tool (Tavily) ──────────────────────────────────────────────────

_tavily_client = None
_tavily_api_key = None


def _get_tavily_client():
    """Return a cached TavilyClient, rebuilding only when the API key changes."""
    global _tavily_client, _tavily_api_key
    from .. import config
    api_key = config.get_str("tavily", "apiKey", env="TAVILY_API_KEY")
    if not api_key:
        return None
    if _tavily_client is None or _tavily_api_key != api_key:
        from tavily import TavilyClient
        _tavily_client = TavilyClient(api_key)
        _tavily_api_key = api_key
    return _tavily_client


def web_search(
    query: str,
    *,
    search_depth: str = "basic",
    topic: str = "general",
    max_results: int = 3,
    time_range: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> str:
    """Search the web using the Tavily API and return formatted results."""
    try:
        from tavily import TavilyClient  # noqa: F401
    except ImportError:
        return (
            "Error: tavily-python is not installed. "
            "Install it with: pip install tavily-python"
        )

    client = _get_tavily_client()
    if client is None:
        return "Error: Tavily API key not configured (set TAVILY_API_KEY or tavily.apiKey in pythonclaw.json)"

    try:
        kwargs: dict = {
            "query": query,
            "search_depth": search_depth,
            "topic": topic,
            "max_results": max_results,
            "include_answer": True,
        }
        if time_range:
            kwargs["time_range"] = time_range
        if include_domains:
            kwargs["include_domains"] = include_domains
        if exclude_domains:
            kwargs["exclude_domains"] = exclude_domains

        response = client.search(**kwargs)
    except Exception as exc:
        logger.warning("[web_search] Tavily API error: %s", exc)
        return f"Web search error: {exc}"

    parts: list[str] = []

    answer = response.get("answer")
    if answer:
        parts.append(f"**Summary:** {answer}\n")

    results = response.get("results", [])
    if results:
        parts.append("**Sources:**")
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            content = r.get("content", "")
            if len(content) > 300:
                content = content[:300] + "..."
            parts.append(f"\n{i}. [{title}]({url})")
            if content:
                parts.append(f"   {content}")

    if not parts:
        return "No results found."

    return "\n".join(parts)


AVAILABLE_TOOLS["web_search"] = web_search


WEB_SEARCH_TOOL: dict = _fn(
    "web_search",
    (
        "Search the web for real-time information using the Tavily API. "
        "Use this when you need up-to-date information, current events, "
        "facts you're unsure about, or anything that benefits from live web data."
    ),
    {
        "query": {
            "type": "string",
            "description": "The search query. Be specific for better results.",
        },
        "search_depth": {
            "type": "string",
            "enum": ["basic", "advanced"],
            "description": "Search depth: 'basic' (fast) or 'advanced' (more thorough).",
            "default": "basic",
        },
        "topic": {
            "type": "string",
            "enum": ["general", "news", "finance"],
            "description": "Search category: 'general', 'news', or 'finance'.",
            "default": "general",
        },
        "max_results": {
            "type": "integer",
            "description": "Number of results to return (1-10). Use 2-3 for most queries.",
            "default": 3,
        },
        "time_range": {
            "type": "string",
            "enum": ["day", "week", "month", "year"],
            "description": "Filter results by recency. Omit for no time filter.",
        },
    },
    ["query"],
)


# ── Knowledge base tool schema (conditional) ─────────────────────────────────

KNOWLEDGE_TOOL: dict = _fn(
    "consult_knowledge_base",
    "Search the knowledge base for relevant information using hybrid retrieval.",
    {"query": {"type": "string", "description": "Specific question or topic to look up."}},
    ["query"],
)


# ── Meta-skill: create_skill ("God Mode") ────────────────────────────────────

def create_skill(
    name: str,
    description: str,
    instructions: str,
    category: str = "",
    resources: dict[str, str] | None = None,
    dependencies: list[str] | None = None,
) -> str:
    """Create a new skill on disk and install its dependencies.

    This is the "god mode" tool — the agent uses it to extend its own
    capabilities at runtime.  After creation, the caller must invalidate
    the SkillRegistry cache so the new skill appears in the catalog.

    All paths are validated against the sandbox.  Resource filenames are
    sanitized to prevent directory traversal.
    """
    from .. import config as _cfg
    skills_dir = os.path.join(str(_cfg.PYTHONCLAW_HOME), "context", "skills")
    _resolve_in_sandbox(skills_dir)
    os.makedirs(skills_dir, exist_ok=True)

    # Build target directory (sanitize name and category)
    safe_name = _sanitize_filename(name.replace(" ", "_").lower())
    if category:
        safe_category = _sanitize_filename(category.replace(" ", "_").lower())
        skill_dir = os.path.join(skills_dir, safe_category, safe_name)
        cat_dir = os.path.join(skills_dir, safe_category)
        cat_md = os.path.join(cat_dir, "CATEGORY.md")
        if not os.path.isfile(cat_md):
            os.makedirs(cat_dir, exist_ok=True)
            with open(cat_md, "w", encoding="utf-8") as f:
                f.write(f"---\nname: {safe_category}\ndescription: Auto-created category for {category} skills.\n---\n")
    else:
        skill_dir = os.path.join(skills_dir, safe_name)

    _resolve_in_sandbox(skill_dir)
    os.makedirs(skill_dir, exist_ok=True)

    # Write SKILL.md
    skill_md_content = (
        f"---\nname: {safe_name}\n"
        f"description: >\n  {description}\n"
        f"---\n\n{instructions}\n"
    )
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    with open(skill_md_path, "w", encoding="utf-8") as f:
        f.write(skill_md_content)

    # Write resource files (filenames are sanitized to prevent traversal)
    written_files = ["SKILL.md"]
    if resources:
        for filename, content in resources.items():
            safe_fn = _sanitize_filename(filename)
            fpath = os.path.join(skill_dir, safe_fn)
            _resolve_in_sandbox(fpath)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            if safe_fn.endswith((".sh", ".py")):
                os.chmod(fpath, 0o755)
            written_files.append(safe_fn)

    # Install dependencies (into the project venv)
    dep_results: list[str] = []
    if dependencies:
        pip_python = _venv_python()
        for dep in dependencies:
            try:
                proc = subprocess.run(
                    [pip_python, "-m", "pip", "install", dep],
                    capture_output=True, text=True, timeout=120,
                    env=_venv_env(),
                )
                if proc.returncode == 0:
                    dep_results.append(f"  ✓ {dep}")
                else:
                    dep_results.append(f"  ✗ {dep}: {proc.stderr.strip()}")
            except Exception as exc:
                dep_results.append(f"  ✗ {dep}: {exc}")

    # Build result summary
    parts = [
        f"Skill '{safe_name}' created at {skill_dir}/",
        f"Files: {', '.join(written_files)}",
    ]
    if dep_results:
        parts.append("Dependencies:\n" + "\n".join(dep_results))
    parts.append("Registry will be refreshed — the skill is now available via use_skill().")

    return "\n".join(parts)


AVAILABLE_TOOLS["create_skill"] = create_skill


META_SKILL_TOOLS: list[dict] = [
    _fn(
        "create_skill",
        (
            "Create a brand-new skill on the fly when no existing skill can handle the user's request. "
            "This writes a SKILL.md and optional resource scripts to the skills directory, "
            "installs pip dependencies, and makes the skill immediately available. "
            "Use this when you need a capability that doesn't exist yet."
        ),
        {
            "name": {
                "type": "string",
                "description": "Skill name (lowercase, underscores). E.g. 'weather_forecast'.",
            },
            "description": {
                "type": "string",
                "description": "One-line description of what the skill does and when to use it.",
            },
            "instructions": {
                "type": "string",
                "description": (
                    "Full Markdown instructions for the skill body (the content after the YAML frontmatter). "
                    "Include ## Instructions, usage examples, and ## Resources sections."
                ),
            },
            "category": {
                "type": "string",
                "description": "Optional category folder (e.g. 'data', 'dev', 'web'). Empty for flat layout.",
                "default": "",
            },
            "resources": {
                "type": "object",
                "description": (
                    "Map of filename → file content for bundled scripts. "
                    "E.g. {\"fetch.py\": \"import requests\\n...\", \"config.yaml\": \"...\"}."
                ),
                "additionalProperties": {"type": "string"},
            },
            "dependencies": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of pip packages to install. E.g. [\"requests\", \"beautifulsoup4\"].",
            },
        },
        ["name", "description", "instructions"],
    ),
]


# ── Cron tool schemas (conditional) ──────────────────────────────────────────

CRON_TOOLS: list[dict] = [
    _fn(
        "cron_add",
        (
            "Schedule a recurring LLM task. "
            "Use standard 5-field cron syntax: 'min hour day month weekday'. "
            "Example: '0 9 * * *' = 9 am daily."
        ),
        {
            "job_id": {"type": "string", "description": "Unique job identifier (no spaces)."},
            "cron": {"type": "string", "description": "5-field cron expression, e.g. '0 9 * * *'."},
            "prompt": {"type": "string", "description": "The prompt the agent will run on each trigger."},
            "deliver_to_chat_id": {
                "type": "integer",
                "description": "Optional Telegram chat_id to deliver the result to.",
            },
            "telegram_user_id": {
                "type": "integer",
                "description": (
                    "Optional explicit Telegram user id for safe personalization. "
                    "Do not use group or channel chat ids."
                ),
            },
        },
        ["job_id", "cron", "prompt"],
    ),
    _fn(
        "cron_remove",
        "Remove a previously scheduled cron job by its ID.",
        {"job_id": {"type": "string", "description": "The job ID to remove."}},
        ["job_id"],
    ),
    _fn(
        "cron_list",
        "List all currently scheduled cron jobs (both static and dynamic).",
        {},
        [],
    ),
]
