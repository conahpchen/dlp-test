"""Credential-scoped fake API session identifiers shared by both adapters."""

import hashlib
import re
import secrets

OBSERVATION_COOKIE = "api_session"


def install_observation_cookie(app):
    @app.middleware("http")
    async def scope_llm_observations(request, call_next):
        if request.url.path != "/v1/chat/completions" or request.method != "POST":
            return await call_next(request)
        cookie = request.cookies.get(OBSERVATION_COOKIE, "")
        fresh = not re.fullmatch(r"[a-f0-9]{32}", cookie)
        request.state.observation_cookie = secrets.token_hex(16) if fresh else cookie
        response = await call_next(request)
        if fresh:
            response.set_cookie(OBSERVATION_COOKIE, request.state.observation_cookie, path="/v1",
                                httponly=True, secure=request.url.scheme == "https",
                                samesite="strict", max_age=86400)
        return response


def observed_session(request, node, session):
    """Pseudonymous evidence bucket, independent from explicit DWS session binding."""
    material = f'{node}:{request.state.observation_cookie}:{session}'
    return "obs-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def credentials(request):
    auth = request.headers.get("authorization", "")
    key = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
    digest = hashlib.sha256(key.encode()).hexdigest() if key else None
    label = request.headers.get("x-session", "")[:128]
    identifier = hashlib.sha256(f"{digest}:{label}".encode()).hexdigest()
    return ("sha256:" + digest[:40] if digest else None), f"llm-{identifier}"


def bind_model_list(engine, session_id, key_hash):
    if engine.check_llm_key(key_hash, session_id):
        engine.world.open_session(session_id, session_id, None, None)
    return engine.llm_models(session_id)


def bind_explicit(engine, request, session, key_hash):
    from research.api import current_lab
    from research.bindings import HEADER, resolve

    if HEADER not in request.headers:
        return session
    binding = resolve(current_lab(), request.headers[HEADER], entry="llm")
    version = binding["world_version"]
    if binding["world_id"] != engine.world.meta["world_id"] or version > engine.world.meta["world_version"]:
        raise KeyError("Unknown binding")
    if (not engine.check_llm_key(key_hash)
            or engine._match_llm_credential(key_hash, view=engine.world.view(version)) is None):
        raise PermissionError("Invalid fake API key")
    overlay = binding.get("branch_version")
    if overlay is not None and overlay not in engine.world.private_versions:
        raise KeyError("Unknown branch")
    identifier = hashlib.sha256((session + binding["id"]).encode()).hexdigest()
    session = f"llm-{identifier}"
    state = engine.world.open_session(session, session, None, None, pinned_version=version)
    if overlay is not None:
        state.own_versions.add(overlay)
    return session
