"""
FastAPI application for the Jarvis Web Dashboard.

Provides REST endpoints for config/skills/status inspection, a config
save endpoint for editing settings from the browser, and a WebSocket
endpoint for real-time chat with the agent.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..core.agent import Agent
from ..core.llm.base import LLMProvider
from ..core.persistent_agent import PersistentAgent
from ..core.session_store import SessionStore
from ..core.skill_loader import SkillRegistry
from .fx_research_debug import build_phase10_debug_payload

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_agent: Agent | None = None
_provider: LLMProvider | None = None
_store: SessionStore | None = None
_start_time: float = 0.0
_build_provider_fn = None
_active_bots: list = []
_chat_lock: asyncio.Lock | None = None
_fastapi_app: FastAPI | None = None

WEB_SESSION_ID = "web:dashboard"


def _web_memory_api_enabled() -> bool:
    """Return whether the memory REST API is enabled.

    Phase 8 user data must not be exposed by unauthenticated network APIs.
    The endpoint is disabled by default; local debugging must opt in.
    """
    return config.get_bool("web", "enableMemoryApi", default=False)


def _web_raw_memory_api_enabled() -> bool:
    """Return whether raw legacy memory export is explicitly enabled."""
    return config.get_bool("web", "enableRawMemoryApi", default=False)


def _fx_research_debug_enabled() -> bool:
    """Return whether the browser FX research debugger is enabled."""
    return config.get_bool(
        "web", "enableFxResearchDebug",
        env="JARVIS_ENABLE_FX_RESEARCH_DEBUG",
        default=False,
    )


def _admin_token() -> str:
    """Read the dashboard admin token from the environment only.

    Do not source this from pythonclaw.json: anyone with config API access
    could otherwise rotate or clear the token through the dashboard itself.
    """
    return os.environ.get("JARVIS_WEB_ADMIN_TOKEN", "")


def _has_admin_token(request: Request) -> bool:
    """Validate the optional admin token for sensitive web APIs."""
    expected = _admin_token()
    if not expected:
        return False
    supplied = request.headers.get("x-jarvis-admin-token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return secrets.compare_digest(supplied, expected)


def _admin_required_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": (
                "Admin token required. Set JARVIS_WEB_ADMIN_TOKEN for "
                "trusted local debugging."
            ),
        },
        status_code=403,
    )


def _require_admin(request: Request):
    """Guard sensitive dashboard APIs.

    Config, identity, and raw memory endpoints can change model behavior or
    reveal Phase 8 user data. They must never be exposed without an admin token,
    even when the dashboard is accidentally bound to a public interface.
    """
    if not _has_admin_token(request):
        return _admin_required_response()
    return None


def _safe_context_to_fields(context: str) -> dict[str, str]:
    """Parse MemoryManager safe boot context into JSON fields."""
    fields: dict[str, str] = {}
    for line in context.splitlines():
        line = line.strip()
        if not line.startswith("- **") or "**:" not in line:
            continue
        label, value = line[4:].split("**:", 1)
        key = label.strip("* ").lower().replace(" ", "_")
        fields[key] = value.strip()
    return fields


def _get_chat_lock() -> asyncio.Lock:
    """Lazily create the web chat lock (must be done inside the event loop)."""
    global _chat_lock
    if _chat_lock is None:
        _chat_lock = asyncio.Lock()
    return _chat_lock


def create_app(provider: LLMProvider | None, *, build_provider_fn=None) -> FastAPI:
    """Build and return the FastAPI app.

    Parameters
    ----------
    provider          : LLM provider (may be None if not yet configured)
    build_provider_fn : callable that rebuilds the provider from config
                        (used after config save to hot-reload the provider)
    """
    global _provider, _store, _start_time, _build_provider_fn, _fastapi_app
    _provider = provider
    _store = SessionStore()
    _start_time = time.time()
    _build_provider_fn = build_provider_fn

    app = FastAPI(title="Jarvis Dashboard", docs_url=None, redoc_url=None)
    _fastapi_app = app

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.add_api_route("/", _serve_index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/config", _api_config_get, methods=["GET"])
    app.add_api_route("/api/config", _api_config_save, methods=["POST"])
    app.add_api_route("/api/skills", _api_skills, methods=["GET"])
    app.add_api_route("/api/status", _api_status, methods=["GET"])
    app.add_api_route("/api/memories", _api_memories, methods=["GET"])
    app.add_api_route("/api/identity", _api_identity, methods=["GET"])
    app.add_api_route("/api/identity/soul", _api_save_soul, methods=["POST"])
    app.add_api_route("/api/identity/persona", _api_save_persona, methods=["POST"])
    app.add_api_route("/api/identity/tools", _api_get_tools_notes, methods=["GET"])
    app.add_api_route("/api/identity/tools", _api_save_tools_notes, methods=["POST"])
    app.add_api_route("/api/memory/index", _api_get_index, methods=["GET"])
    app.add_api_route("/api/memory/index", _api_save_index, methods=["POST"])
    app.add_api_route("/api/transcribe", _api_transcribe, methods=["POST"])
    app.add_api_route("/api/marketplace/search", _api_marketplace_search, methods=["POST"])
    app.add_api_route("/api/marketplace/browse", _api_marketplace_browse, methods=["GET"])
    app.add_api_route("/api/marketplace/install", _api_marketplace_install, methods=["POST"])
    app.add_api_route("/api/marketplace/stats", _api_marketplace_stats, methods=["GET"])
    # Legacy aliases
    app.add_api_route("/api/skillhub/search", _api_marketplace_search, methods=["POST"])
    app.add_api_route("/api/skillhub/browse", _api_marketplace_browse, methods=["GET"])
    app.add_api_route("/api/skillhub/install", _api_marketplace_install, methods=["POST"])
    app.add_api_route("/api/channels", _api_channels_status, methods=["GET"])
    app.add_api_route("/api/channels/restart", _api_channels_restart, methods=["POST"])
    app.add_api_route("/api/files/clear", _api_clear_files, methods=["POST"])
    app.add_api_route("/api/files", _api_list_files, methods=["GET"])
    app.add_api_route("/debug/fx_research", _debug_fx_research_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/debug/fx_research", _api_debug_fx_research, methods=["POST"])
    app.add_api_websocket_route("/ws/chat", _ws_chat)

    return app


def _get_agent() -> Agent | None:
    """Lazy-init the shared web agent with persistent sessions."""
    global _agent
    if _agent is not None:
        return _agent
    if _provider is None:
        return None
    try:
        verbose = config.get("agent", "verbose", default=False)
        _agent = PersistentAgent(
            provider=_provider,
            verbose=bool(verbose),
            store=_store,
            session_id=WEB_SESSION_ID,
        )
    except Exception as exc:
        logger.warning("[Web] Agent init failed: %s", exc)
        return None
    return _agent


def _reset_agent() -> None:
    """Discard the current agent so the next call rebuilds it."""
    global _agent
    _agent = None


# ── HTML ──────────────────────────────────────────────────────────────────────

async def _serve_index():
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ── REST API ──────────────────────────────────────────────────────────────────

def _mask_secrets(obj: Any, _parent_key: str = "") -> Any:
    """Recursively mask values whose key contains 'apikey' or 'token'."""
    if isinstance(obj, dict):
        return {k: _mask_secrets(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_secrets(v) for v in obj]
    if isinstance(obj, str) and obj:
        key_lower = _parent_key.lower()
        if any(s in key_lower for s in ("apikey", "token", "secret", "password")):
            if len(obj) > 8:
                return obj[:4] + "*" * (len(obj) - 8) + obj[-4:]
            return "****"
    return obj


def _secret_keys_present(obj: Any, _parent_key: str = "") -> dict[str, str]:
    """Walk config and return a flat map of dotted-key → value for secret fields."""
    result: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{_parent_key}.{k}" if _parent_key else k
            if isinstance(v, (dict, list)):
                result.update(_secret_keys_present(v, full))
            elif isinstance(v, str) and v:
                if any(s in k.lower() for s in ("apikey", "token", "secret", "password")):
                    result[full] = v
    return result


_MASKED_PLACEHOLDER = "••••••••"


async def _api_config_get(request: Request):
    blocked = _require_admin(request)
    if blocked:
        return blocked
    raw = config.as_dict()
    masked = _mask_secrets(copy.deepcopy(raw))
    cfg_path = config.config_path()

    # Build a list of which secret fields have a value set (without revealing them)
    secrets_set = {k: True for k in _secret_keys_present(raw)}

    return {
        "config": masked,
        "configPath": str(cfg_path) if cfg_path else None,
        "providerReady": _provider is not None,
        "secretsSet": secrets_set,
    }


def _deep_set(d: dict, keys: list[str], value: Any) -> None:
    """Set a value in a nested dict using a list of keys."""
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _deep_get_raw(d: dict, keys: list[str]) -> Any:
    """Get a value from a nested dict using a list of keys."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


async def _api_config_save(request: Request):
    """Save new configuration to pythonclaw.json and hot-reload the provider.

    Secret fields that arrive as the masked placeholder or empty string
    are preserved from the existing config (not overwritten).
    """
    global _provider
    blocked = _require_admin(request)
    if blocked:
        return blocked

    try:
        body = await request.json()
        new_config = body.get("config")
        if not isinstance(new_config, dict):
            return JSONResponse({"ok": False, "error": "Invalid config object."}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # Merge: for any secret field that is still the placeholder or empty,
    # keep the original value from the current config.
    existing = config.as_dict()
    existing_secrets = _secret_keys_present(existing)
    for dotted_key, original_value in existing_secrets.items():
        keys = dotted_key.split(".")
        incoming = _deep_get_raw(new_config, keys)
        if incoming is None or incoming == "" or incoming == _MASKED_PLACEHOLDER or "****" in str(incoming):
            _deep_set(new_config, keys, original_value)

    cfg_path = config.config_path()
    if cfg_path is None:
        cfg_path = config.PYTHONCLAW_HOME / "pythonclaw.json"

    try:
        json_text = json.dumps(new_config, indent=2, ensure_ascii=False)
        cfg_path.write_text(json_text + "\n", encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Write failed: {exc}"}, status_code=500)

    config.load(str(cfg_path), force=True)
    logger.info("[Web] Config saved to %s", cfg_path)

    _reset_agent()
    if _build_provider_fn:
        try:
            _provider = _build_provider_fn()
            logger.info("[Web] Provider rebuilt successfully.")
        except Exception as exc:
            logger.warning("[Web] Provider rebuild failed: %s", exc)
            _provider = None

    channels_started = await _maybe_start_channels()

    return {
        "ok": True,
        "configPath": str(cfg_path),
        "providerReady": _provider is not None,
        "channelsStarted": channels_started,
    }


async def _api_skills():
    agent = _get_agent()
    if agent is None:
        try:
            pkg_templates = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "templates", "skills",
            )
            skills_dirs = [pkg_templates, os.path.join(str(config.PYTHONCLAW_HOME), "context", "skills")]
            skills_dirs = [d for d in skills_dirs if os.path.isdir(d)]
            registry = SkillRegistry(skills_dirs=skills_dirs)
            skills_meta = registry.discover()
        except Exception:
            return {"total": 0, "categories": {}}
    else:
        registry = agent._registry
        skills_meta = registry.discover()

    categories: dict[str, list] = {}
    for sm in skills_meta:
        cat = sm.category or "uncategorised"
        categories.setdefault(cat, []).append({
            "name": sm.name,
            "description": sm.description,
            "category": cat,
            "path": sm.path,
            "emoji": sm.emoji,
        })

    cat_meta = {}
    for cat_key, cat_obj in registry.categories.items():
        cat_meta[cat_key] = {
            "name": cat_obj.name,
            "description": cat_obj.description,
            "emoji": cat_obj.emoji,
        }

    return {"total": len(skills_meta), "categories": categories, "categoryMeta": cat_meta}


def _build_status(request: Request | None = None):
    uptime = int(time.time() - _start_time)
    provider_name = config.get_str("llm", "provider", env="LLM_PROVIDER", default="deepseek")
    is_admin = _has_admin_token(request) if request is not None else False

    agent = _get_agent()
    if agent is None:
        public_status = {
            "provider": "Not configured",
            "providerName": provider_name,
            "providerReady": False,
            "uptimeSeconds": uptime,
        }
        if is_admin:
            public_status.update({
                "skillsLoaded": 0,
                "skillsTotal": 0,
                "memoryCount": 0,
                "historyLength": 0,
                "compactionCount": 0,
                "webSearchEnabled": False,
            })
        return public_status

    status = {
        "provider": type(agent.provider).__name__,
        "providerName": provider_name,
        "providerReady": True,
        "uptimeSeconds": uptime,
    }
    if is_admin:
        status.update({
            "skillsLoaded": len(agent.loaded_skill_names),
            "skillsTotal": len(agent._registry.discover()),
            "memoryCount": len(agent.memory.list_all()),
            "historyLength": len(agent.messages),
            "compactionCount": agent.compaction_count,
            "webSearchEnabled": agent._web_search_enabled,
            "sessionPersistent": True,
        })
    return status


async def _api_status(request: Request):
    return _build_status(request)


async def _api_memories(request: Request):
    """Return safe personalization fields; never raw memory by default.

    Phase 8 privacy rule: raw MEMORY.md values, daily logs, and inferred
    legacy memories must not be exposed over the Web API without explicit
    local-debug opt-in and an admin token.
    """
    if not _web_memory_api_enabled():
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Memory API is disabled by default. Enable "
                    "web.enableMemoryApi only for trusted local debugging."
                ),
            },
            status_code=403,
        )

    agent = _get_agent()
    if agent is None:
        return {"ok": True, "safe": True, "total": 0, "fields": {}}

    safe_context = agent.memory.get_safe_boot_context()
    fields = _safe_context_to_fields(safe_context)

    if request.query_params.get("raw") == "1":
        if not (_web_raw_memory_api_enabled() and _has_admin_token(request)):
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Raw memory export is blocked. It requires "
                        "web.enableRawMemoryApi=true and a valid admin token."
                    ),
                },
                status_code=403,
            )
        memories = agent.memory.list_all()
        return {"ok": True, "safe": False, "total": len(memories), "memories": memories}

    return {
        "ok": True,
        "safe": True,
        "total": len(fields),
        "fields": fields,
        "context": safe_context,
    }


async def _api_identity(request: Request):
    """Return soul, persona content, and the full tool list."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    from ..core.tools import (
        CRON_TOOLS,
        KNOWLEDGE_TOOL,
        MEMORY_TOOLS,
        META_SKILL_TOOLS,
        PRIMITIVE_TOOLS,
        SKILL_TOOLS,
        WEB_SEARCH_TOOL,
    )

    def _read_md(directory: str) -> str | None:
        p = Path(directory)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix in (".md", ".txt") and f.is_file():
                    return f.read_text(encoding="utf-8").strip()
        return None

    home = config.PYTHONCLAW_HOME
    soul = _read_md(str(home / "context" / "soul"))
    persona = _read_md(str(home / "context" / "persona"))
    tools_notes = _read_md(str(home / "context" / "tools"))
    index_file = home / "context" / "memory" / "INDEX.md"
    index_content = None
    if index_file.is_file() and _has_admin_token(request):
        try:
            index_content = index_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    def _tool_info(schema: dict) -> dict:
        fn = schema.get("function", {})
        return {"name": fn.get("name", ""), "description": fn.get("description", "")}

    tools = []
    tool_groups = [
        ("Primitive", PRIMITIVE_TOOLS),
        ("Skills", SKILL_TOOLS),
        ("Meta", META_SKILL_TOOLS),
        ("Memory", MEMORY_TOOLS),
        ("Cron", CRON_TOOLS),
    ]
    for group, schemas in tool_groups:
        for s in schemas:
            info = _tool_info(s)
            info["group"] = group
            tools.append(info)

    tools.append({**_tool_info(WEB_SEARCH_TOOL), "group": "Search"})
    tools.append({**_tool_info(KNOWLEDGE_TOOL), "group": "Knowledge"})

    return {
        "soul": soul,
        "persona": persona,
        "toolsNotes": tools_notes,
        "indexContent": index_content,
        "indexContentRequiresAdmin": index_file.is_file() and index_content is None,
        "soulConfigured": soul is not None,
        "personaConfigured": persona is not None,
        "toolsNotesConfigured": tools_notes is not None,
        "indexConfigured": index_content is not None,
        "tools": tools,
    }


async def _api_save_soul(request: Request):
    """Save soul content to context/soul/SOUL.md and reload agent identity."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "Content cannot be empty."}, status_code=400)

        soul_dir = config.PYTHONCLAW_HOME / "context" / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        soul_file = soul_dir / "SOUL.md"
        soul_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] Soul saved to %s", soul_file)

        _reload_agent_identity()
        return {"ok": True, "path": str(soul_file)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_save_persona(request: Request):
    """Save persona content to context/persona/persona.md and reload agent identity."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "Content cannot be empty."}, status_code=400)

        persona_dir = config.PYTHONCLAW_HOME / "context" / "persona"
        persona_dir.mkdir(parents=True, exist_ok=True)
        persona_file = persona_dir / "persona.md"
        persona_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] Persona saved to %s", persona_file)

        _reload_agent_identity()
        return {"ok": True, "path": str(persona_file)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_get_tools_notes(request: Request):
    """Return the current TOOLS.md content."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    tools_dir = config.PYTHONCLAW_HOME / "context" / "tools"
    content = None
    if tools_dir.is_dir():
        for f in sorted(tools_dir.iterdir()):
            if f.suffix in (".md", ".txt") and f.is_file():
                content = f.read_text(encoding="utf-8").strip()
                break
    elif tools_dir.is_file():
        content = tools_dir.read_text(encoding="utf-8").strip()
    return {"ok": True, "content": content}


async def _api_save_tools_notes(request: Request):
    """Save TOOLS.md content and reload agent identity."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
        content = body.get("content", "").strip()

        tools_dir = config.PYTHONCLAW_HOME / "context" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        tools_file = tools_dir / "TOOLS.md"
        tools_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] TOOLS.md saved to %s", tools_file)

        _reload_agent_identity()
        return {"ok": True, "path": str(tools_file)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_get_index(request: Request):
    """Return INDEX.md only to authenticated local/debug admins."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    index_path = config.PYTHONCLAW_HOME / "context" / "memory" / "INDEX.md"
    content = ""
    if index_path.is_file():
        try:
            content = index_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return {"content": content, "path": str(index_path)}


async def _api_save_index(request: Request):
    """Save INDEX.md content and refresh agent memory."""
    blocked = _require_admin(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        index_dir = config.PYTHONCLAW_HOME / "context" / "memory"
        index_dir.mkdir(parents=True, exist_ok=True)
        index_file = index_dir / "INDEX.md"
        index_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] INDEX.md saved to %s", index_file)

        agent = _get_agent()
        if agent is not None:
            agent.memory.storage._load()
            agent._init_system_prompt()

        return {"ok": True, "path": str(index_file)}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500
        )


async def _api_transcribe(request: Request):
    """Proxy audio to Deepgram STT and return transcript."""
    from ..core.stt import no_key_message, transcribe_bytes_async

    content_type = request.headers.get("content-type", "audio/webm")
    body = await request.body()
    if not body:
        return JSONResponse({"ok": False, "error": "No audio data received."}, status_code=400)

    try:
        transcript = await transcribe_bytes_async(body, content_type)
    except Exception as exc:
        logger.warning("[Web] Deepgram error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    if transcript is None:
        return JSONResponse({"ok": False, "error": no_key_message()}, status_code=400)

    return {"ok": True, "transcript": transcript}


async def _api_marketplace_search(request: Request):
    """Search ClawHub marketplace."""
    from ..core import skillhub

    try:
        body = await request.json()
        query = body.get("query", "").strip()
        if not query:
            return JSONResponse({"ok": False, "error": "Query is required."}, status_code=400)
        limit = int(body.get("limit", 10))
        results = await skillhub.search_async(query, limit=limit)
        return {"ok": True, "results": results}
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_marketplace_browse(request: Request):
    """Browse ClawHub catalog."""
    from ..core import skillhub

    try:
        limit = int(request.query_params.get("limit", 20))
        sort = request.query_params.get("sort", "score")
        results = await skillhub.browse_async(limit=limit, sort=sort)
        return {"ok": True, "results": results}
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_marketplace_install(request: Request):
    """Install a skill from ClawHub and hot-reload into the running agent."""
    from ..core import skillhub

    try:
        body = await request.json()
        skill_id = body.get("skill_id", "").strip()
        if not skill_id:
            return JSONResponse({"ok": False, "error": "skill_id is required."}, status_code=400)

        path = await skillhub.install_skill_async(skill_id)

        agent = _get_agent()
        skill_count = 0
        installed_name = ""
        if agent is not None:
            agent._refresh_skill_registry()
            skill_count = len(agent._registry.discover())
            for sm in agent._registry.discover():
                if sm.path == path:
                    installed_name = sm.name
                    break

        if not installed_name:
            import re as _re
            md_path = os.path.join(path, "SKILL.md")
            try:
                md_text = open(md_path, encoding="utf-8").read()
                m = _re.search(r"^name:\s*(.+)$", md_text, _re.MULTILINE)
                installed_name = m.group(1).strip() if m else skill_id
            except OSError:
                installed_name = skill_id

        return {
            "ok": True,
            "path": path,
            "skill_name": installed_name,
            "skill_count": skill_count,
            "message": f"Skill '{installed_name}' installed and ready to use.",
        }
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_marketplace_stats(request: Request):
    """Get ClawHub marketplace statistics."""
    from ..core import skillhub

    try:
        result = await skillhub.verify_api_async()
        return result
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _maybe_start_channels() -> list[str]:
    """Start channels whose tokens are now configured but not yet running."""
    global _active_bots
    if _provider is None:
        return []

    wanted = []
    tg_token = config.get_str("channels", "telegram", "token", default="")
    if tg_token:
        wanted.append("telegram")
    dc_token = config.get_str("channels", "discord", "token", default="")
    if dc_token:
        wanted.append("discord")
    wa_phone = config.get_str("channels", "whatsapp", "phoneNumberId", default="")
    wa_token = config.get_str("channels", "whatsapp", "token", default="")
    if wa_phone and wa_token:
        wanted.append("whatsapp")

    if not wanted:
        return []

    running_types = set()
    for bot in _active_bots:
        cls_name = type(bot).__name__.lower()
        if "telegram" in cls_name:
            running_types.add("telegram")
        elif "discord" in cls_name:
            running_types.add("discord")
        elif "whatsapp" in cls_name:
            running_types.add("whatsapp")

    to_start = [ch for ch in wanted if ch not in running_types]
    if not to_start:
        return list(running_types)

    try:
        from ..server import start_channels
        new_bots = await start_channels(_provider, to_start, fastapi_app=_fastapi_app)
        _active_bots.extend(new_bots)
        return [ch for ch in wanted if ch in running_types or ch in to_start]
    except Exception as exc:
        logger.warning("[Web] Channel start failed: %s", exc)
        return list(running_types)


async def _api_channels_status():
    """Return status of messaging channels."""
    channels = []
    for bot in _active_bots:
        cls_name = type(bot).__name__
        if "Telegram" in cls_name:
            ch_type = "telegram"
        elif "Discord" in cls_name:
            ch_type = "discord"
        elif "WhatsApp" in cls_name:
            ch_type = "whatsapp"
        else:
            ch_type = cls_name
        channels.append({"type": ch_type, "running": True})

    running_types = {c["type"] for c in channels}

    tg_token = config.get_str("channels", "telegram", "token", default="")
    dc_token = config.get_str("channels", "discord", "token", default="")
    wa_phone = config.get_str("channels", "whatsapp", "phoneNumberId", default="")
    wa_token = config.get_str("channels", "whatsapp", "token", default="")

    if tg_token and "telegram" not in running_types:
        channels.append({"type": "telegram", "running": False, "tokenSet": True})
    if dc_token and "discord" not in running_types:
        channels.append({"type": "discord", "running": False, "tokenSet": True})
    if wa_phone and wa_token and "whatsapp" not in running_types:
        channels.append({"type": "whatsapp", "running": False, "tokenSet": True})

    return {"channels": channels}


async def _api_channels_restart(request: Request):
    """Stop and restart all configured channels."""
    global _active_bots

    for bot in _active_bots:
        if hasattr(bot, "stop_async"):
            try:
                await bot.stop_async()
            except Exception:
                pass
    _active_bots = []

    started = await _maybe_start_channels()
    return {"ok": True, "channels": started}


def _reload_agent_identity() -> None:
    """Reload the agent's soul/persona/tools from disk without full reset."""
    global _agent
    if _agent is None:
        return
    from ..core.agent import _load_text_dir_or_file
    home = config.PYTHONCLAW_HOME
    _agent.soul_instruction = _load_text_dir_or_file(
        str(home / "context" / "soul"), label="Soul"
    )
    _agent.persona_instruction = _load_text_dir_or_file(
        str(home / "context" / "persona"), label="Persona"
    )
    _agent.tools_notes = _load_text_dir_or_file(
        str(home / "context" / "tools"), label="Tools"
    )
    _agent._needs_onboarding = False
    _agent._init_system_prompt()


# ── Files management ──────────────────────────────────────────────────────────

async def _api_clear_files(request: Request):
    """Delete all downloaded/generated files."""
    count = config.clear_files()
    return JSONResponse({"ok": True, "cleared": count})


async def _api_list_files(request: Request):
    """List files in the shared files directory."""
    d = config.files_dir()
    files = []
    for entry in sorted(d.iterdir()):
        if entry.is_file():
            files.append({
                "name": entry.name,
                "size": entry.stat().st_size,
                "modified": entry.stat().st_mtime,
            })
    return JSONResponse({"files": files, "dir": str(d)})


# ── FX research browser debugger ──────────────────────────────────────────────

async def _debug_fx_research_page(request: Request):
    """Small local-only browser page for running the FX research workflow."""
    enabled = _fx_research_debug_enabled()
    status = (
        "enabled"
        if enabled
        else "disabled: set web.enableFxResearchDebug=true or JARVIS_ENABLE_FX_RESEARCH_DEBUG=true"
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jarvis FX Research Debug</title>
  <style>
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e5e7eb; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 28px 18px; }}
    label {{ display: block; margin: 14px 0 6px; color: #cbd5e1; font-size: 13px; }}
    input, button, textarea {{ font: inherit; }}
    input {{ width: 100%; box-sizing: border-box; border: 1px solid #334155; border-radius: 8px; padding: 10px 12px; background: #111827; color: #e5e7eb; }}
    button {{ margin-top: 16px; border: 0; border-radius: 8px; padding: 10px 14px; background: #38bdf8; color: #082f49; font-weight: 700; cursor: pointer; }}
    button:disabled {{ opacity: .6; cursor: wait; }}
    pre {{ white-space: pre-wrap; word-break: break-word; border: 1px solid #334155; border-radius: 8px; padding: 14px; background: #020617; min-height: 240px; }}
    .panel {{ margin-top: 18px; border: 1px solid #334155; border-radius: 8px; background: #111827; padding: 14px; }}
    .panel h2 {{ margin: 0 0 10px; font-size: 16px; }}
    .followup {{ border-top: 1px solid #1f2937; padding: 12px 0; }}
    .followup:first-of-type {{ border-top: 0; }}
    .badge {{ display: inline-block; border: 1px solid #334155; border-radius: 999px; padding: 2px 8px; margin-right: 6px; color: #bae6fd; font-size: 12px; }}
    .muted {{ color: #94a3b8; font-size: 13px; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    @media (max-width: 720px) {{ .row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <h1>Jarvis FX Research Debug</h1>
    <p class="muted">状态：{status}</p>
    <div class="row">
      <div>
        <label for="token">Admin token</label>
        <input id="token" type="password" autocomplete="off" placeholder="JARVIS_WEB_ADMIN_TOKEN">
      </div>
      <div>
        <label for="userId">User ID</label>
        <input id="userId" inputmode="numeric" placeholder="Telegram user id；留空则使用 0">
      </div>
    </div>
    <button id="run">Run /fx_research</button>
    <p id="meta" class="muted"></p>
    <section class="panel">
      <h2>Follow-up Router 推荐</h2>
      <p id="followupMeta" class="muted">尚未运行。默认仅推荐，不会启动额外 Agent。</p>
      <div id="followups"></div>
    </section>
    <section class="panel">
      <h2>Phase 10 Debug</h2>
      <p id="phase10Meta" class="muted">尚未运行。</p>
      <pre id="phase10Out"></pre>
    </section>
    <pre id="out"></pre>
  </main>
  <script>
    const run = document.getElementById('run');
    const out = document.getElementById('out');
    const meta = document.getElementById('meta');
    const followupMeta = document.getElementById('followupMeta');
    const followups = document.getElementById('followups');
    const phase10Meta = document.getElementById('phase10Meta');
    const phase10Out = document.getElementById('phase10Out');
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, ch => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }}[ch]));
    }}
    function renderFollowups(items) {{
      const rows = Array.isArray(items) ? items : [];
      followupMeta.textContent = rows.length
        ? `生成 ${{rows.length}} 条推荐。推荐模式：未执行额外 Agent。`
        : '未发现需要深入研究的方向。推荐模式：未执行额外 Agent。';
      followups.innerHTML = rows.map((item, index) => `
        <div class="followup">
          <div>
            <span class="badge">#${{index + 1}}</span>
            <span class="badge">${{escapeHtml(item.trigger_type)}}</span>
            <span class="badge">priority=${{escapeHtml(item.priority)}}</span>
          </div>
          <div><strong>${{escapeHtml(item.target_agent)}}</strong> · ${{escapeHtml(item.target_category)}}</div>
          <div class="muted">${{escapeHtml(item.reason)}}</div>
          <div>${{escapeHtml(item.suggested_query)}}</div>
        </div>
      `).join('');
    }}
    run.addEventListener('click', async () => {{
      run.disabled = true;
      out.textContent = '';
      phase10Out.textContent = '';
      followups.innerHTML = '';
      followupMeta.textContent = 'Running...';
      phase10Meta.textContent = 'Running...';
      meta.textContent = 'Running...';
      try {{
        const res = await fetch('/api/debug/fx_research', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-Jarvis-Admin-Token': document.getElementById('token').value
          }},
          body: JSON.stringify({{
            user_id: document.getElementById('userId').value || 0,
            preset_name: 'fx_cnyaud'
          }})
        }});
        const data = await res.json();
        if (!res.ok || !data.ok) {{
          out.textContent = JSON.stringify(data, null, 2);
        }} else {{
          meta.textContent = `brief=${{data.brief_id}} latency=${{data.latency_s}}s coverage=${{data.trace_summary.covered_sections}}/${{data.trace_summary.total_sections}} conflicts=${{data.trace_summary.conflict_count}}`;
          renderFollowups(data.followup_requests);
          const p10 = data.phase10 || {{}};
          phase10Meta.textContent = `ranking=${{p10.ranking_basis || 'unknown'}} selected=${{p10.score_summary?.selected_count ?? 0}} used=${{p10.score_summary?.used_in_brief_count ?? 0}} scored=${{p10.score_summary?.scored_selected_count ?? 0}}`;
          phase10Out.textContent = JSON.stringify(p10, null, 2);
          out.textContent = data.text;
        }}
      }} catch (err) {{
        out.textContent = String(err);
        followupMeta.textContent = 'Follow-up 推荐生成失败或请求失败。';
        phase10Meta.textContent = 'Phase 10 Debug 生成失败或请求失败。';
      }} finally {{
        run.disabled = false;
      }}
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(html)


async def _api_debug_fx_research(request: Request):
    """Run the same FX research workflow as Telegram, without Telegram I/O."""
    if not _fx_research_debug_enabled():
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "FX research debug API disabled. Set "
                    "web.enableFxResearchDebug=true or "
                    "JARVIS_ENABLE_FX_RESEARCH_DEBUG=true."
                ),
            },
            status_code=404,
        )
    blocked = _require_admin(request)
    if blocked:
        return blocked

    try:
        body = await request.json()
    except Exception:
        body = {}
    preset_name = str(body.get("preset_name") or "fx_cnyaud")
    if preset_name != "fx_cnyaud":
        return JSONResponse(
            {"ok": False, "error": "Only preset_name='fx_cnyaud' is supported."},
            status_code=400,
        )

    try:
        user_id = int(body.get("user_id") or 0)
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "user_id must be an integer."},
            status_code=400,
        )

    t0 = time.monotonic()
    try:
        from ..channels._telegram_helpers import (
            _ensure_research_path,
            _format_research_brief,
        )
        _ensure_research_path()
        import importlib

        _coord = importlib.import_module("coordinator")
        _super = importlib.import_module("supervisor")
        _schema = importlib.import_module("schema")
        _followup = importlib.import_module("followup_router")
        _evidence_store = importlib.import_module("evidence_store")

        task, outputs, cost_estimate = await _coord.run_research(
            preset_name=preset_name,
            user_id=user_id,
        )
        preset = _schema.PRESET_REGISTRY.get(task.preset_name)
        if preset is None:
            raise ValueError(f"Preset {task.preset_name!r} not found")

        brief = await _super.SupervisorReportWriter().run(
            task, preset, outputs, cost_estimate,
        )
        latency_s = time.monotonic() - t0
        text = _format_research_brief(brief, latency_s)
        traces = list(getattr(brief, "retrieval_traces", []) or [])
        selected_ids: set[str] = set()
        for trace in traces:
            selected_ids.update(getattr(trace, "selected_chunk_ids", []) or [])
        trace_summary = {
            "total_chunks": max(
                (int(getattr(t, "total_chunks", 0) or 0) for t in traces),
                default=0,
            ),
            "selected_chunks": len(selected_ids),
            "covered_sections": sum(
                1 for t in traces
                if getattr(t, "section_covered", False)
                or int(getattr(t, "retrieved_count", 0) or 0) > 0
            ),
            "total_sections": len(traces),
            "conflict_count": sum(
                int(getattr(t, "conflict_count", 0) or 0) for t in traces
            ),
        }
        phase10 = build_phase10_debug_payload(
            brief.task_id,
            traces,
            _evidence_store.EvidenceStore,
        )
        followup_requests = _followup.generate_followup_requests(
            task,
            outputs,
            context_pack=None,
            conflict_summary={"conflict_count": trace_summary["conflict_count"]},
        )
        return JSONResponse({
            "ok": True,
            "brief_id": brief.task_id[:8],
            "task_id": brief.task_id,
            "latency_s": round(latency_s, 1),
            "agent_statuses": brief.agent_statuses,
            "data_gaps": brief.data_gaps,
            "trace_summary": trace_summary,
            "phase10": phase10,
            "followup_execution_enabled": False,
            "followup_requests": [req.to_dict() for req in followup_requests],
            "text": text,
        })
    except Exception as exc:
        logger.exception("[Web] FX research debug failed")
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500,
        )


# ── Web file sender ───────────────────────────────────────────────────────────

def _register_web_file_sender(loop: asyncio.AbstractEventLoop, ws: WebSocket) -> None:
    """Register a sync callback so the Agent can push file-download links to the web UI."""
    from ..core.tools import set_file_sender

    def _sender(path: str, caption: str = "") -> None:
        import base64 as _b64

        name = os.path.basename(path)
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            data = _b64.b64encode(fh.read()).decode()

        async def _push():
            try:
                await ws.send_json({
                    "type": "file",
                    "filename": name,
                    "size": size,
                    "caption": caption,
                    "data": data,
                })
            except Exception as exc:
                logger.warning("[Web] send_file via WS failed: %s", exc)

        future = asyncio.run_coroutine_threadsafe(_push(), loop)
        future.result(timeout=60)

    set_file_sender(_sender)


# ── WebSocket Chat ────────────────────────────────────────────────────────────

async def _ws_chat(websocket: WebSocket):
    await websocket.accept()
    logger.info("[Web] WebSocket client connected")

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                message = payload.get("message", "").strip()
                image_data = payload.get("image")  # data:image/...;base64,...
            except (json.JSONDecodeError, AttributeError):
                message = data.strip()
                image_data = None

            if not message and not image_data:
                continue

            agent = _get_agent()
            if agent is None:
                await websocket.send_json({
                    "type": "error",
                    "content": "LLM provider is not configured yet. Go to the Config tab and set your API key, then save.",
                })
                continue

            if message.startswith("/compact"):
                hint = message[len("/compact"):].strip() or None
                result = agent.compact(instruction=hint)
                await websocket.send_json({"type": "response", "content": result})
                continue

            if message == "/status":
                status = _build_status()
                await websocket.send_json({"type": "response", "content": json.dumps(status, indent=2)})
                continue

            if message == "/clear":
                if _store:
                    _store.delete(WEB_SESSION_ID)
                if agent is not None:
                    agent.clear_history()
                await websocket.send_json({"type": "response", "content": "Chat history cleared. Agent is still active with all skills and memory intact."})
                continue

            lock = _get_chat_lock()
            if lock.locked():
                await websocket.send_json({"type": "thinking", "content": "Processing previous message\u2026"})
            else:
                await websocket.send_json({"type": "thinking", "content": ""})

            loop = asyncio.get_event_loop()

            _register_web_file_sender(loop, websocket)

            try:
                token_queue: asyncio.Queue[str | None] = asyncio.Queue()

                def _on_token(text: str) -> None:
                    loop.call_soon_threadsafe(token_queue.put_nowait, text)

                async def _stream_tokens() -> None:
                    while True:
                        tok = await token_queue.get()
                        if tok is None:
                            break
                        try:
                            await websocket.send_json(
                                {"type": "stream", "content": tok}
                            )
                        except Exception:
                            break

                # Build multimodal input if image is attached
                chat_input: str | list = message or ""
                if image_data:
                    chat_input = [
                        {"type": "text", "text": message or "What is in this image?"},
                        {"type": "image_url", "image_url": {"url": image_data}},
                    ]

                async with lock:
                    stream_task = asyncio.create_task(_stream_tokens())
                    try:
                        response = await loop.run_in_executor(
                            None, agent.chat_stream, chat_input, _on_token
                        )
                    finally:
                        loop.call_soon_threadsafe(
                            token_queue.put_nowait, None
                        )
                        await stream_task
                await websocket.send_json(
                    {"type": "response", "content": response}
                )
            except Exception as exc:
                logger.exception("[Web] Chat error")
                await websocket.send_json({"type": "error", "content": str(exc)})

    except WebSocketDisconnect:
        logger.info("[Web] WebSocket client disconnected")
    except Exception:
        logger.exception("[Web] WebSocket error")
