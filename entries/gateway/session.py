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
