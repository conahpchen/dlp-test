"""Employee identities for the independently operated honeypot services."""

import hashlib
import hmac
import json
import os
import time
from functools import lru_cache

from contracts.agent_personas import load_personas


@lru_cache(maxsize=None)
def service_members(service):
    return {p["username"] for p in load_personas() if service in p.get("service_tasks", {})}


def service_secret(service, username):
    if username not in service_members(service):
        return ""
    return os.environ.get(f"HONEYNET_EMPLOYEE_{service.upper()}_{username.upper()}_SECRET", "")


def _signature(secret, timestamp, label, body):
    message = json.dumps([timestamp, label, body], sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def llm_proof(username, secret, label, body):
    timestamp = str(int(time.time()))
    return f"{username}:{timestamp}:{_signature(secret, timestamp, label, body)}"


def verify_llm_employee(proof, label, body):
    return _verify_employee("llm", proof, label, body)


def web_proof(username, secret, label, method, path):
    return llm_proof(username, secret, label, ["employee-web", method, path])


def verify_web_employee(proof, label, method, path):
    return _verify_employee("web", proof, label, ["employee-web", method, path])


def _verify_employee(service, proof, label, body):
    username, _, proof = proof.partition(":")
    timestamp, _, signature = proof.partition(":")
    secret = service_secret(service, username)
    if (len(secret) < 16 or len(timestamp) != 10 or not timestamp.isascii()
            or not timestamp.isdecimal() or abs(time.time() - int(timestamp)) > 60
            or len(signature) != 64 or not signature.isascii() or not label or len(label) > 128):
        return None
    expected = _signature(secret, timestamp, label, body)
    return username if hmac.compare_digest(expected, signature) else None
