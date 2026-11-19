"""Credential-scoped fake API session identifiers shared by both adapters."""

import hashlib


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
    binding = resolve(current_lab(), request.headers[HEADER])
    version = binding["world_version"]
    if binding["world_id"] != engine.world.meta["world_id"] or version > engine.world.meta["world_version"]:
        raise KeyError("Unknown binding")
    if (not engine.check_llm_key(key_hash)
            or engine._match_llm_credential(key_hash, view=engine.world.view(version)) is None):
        raise PermissionError("Invalid fake API key")
    identifier = hashlib.sha256((session + binding["id"]).encode()).hexdigest()
    session = f"llm-{identifier}"
    engine.world.open_session(session, session, None, None, pinned_version=version)
    return session
