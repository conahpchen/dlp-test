"""Bounded, best-effort redaction of untrusted visitor input. Never execute it."""
import hashlib
import json
import re
from urllib.parse import parse_qsl

MAX_BYTES = 8192
SENSITIVE = re.compile(r'password|passwd|pwd|secret|token|authorization|cookie|api[_-]?key|credential', re.I)


def clean_text(value):
    value = value[:2000]
    value = re.sub(r'\bBearer\s+\S+', 'Bearer [redacted]', value, flags=re.I)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[redacted-key]', value)
    value = re.sub(r'(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+', r'\1=[redacted]', value)
    return value


def clean(value, depth=0):
    if depth > 4:
        return '[depth-limit]'
    if isinstance(value, dict):
        return {clean_text(str(k))[:80]: '[redacted]' if SENSITIVE.search(str(k)) else clean(v, depth+1)
                for k,v in list(value.items())[:20]}
    if isinstance(value, list):
        return [clean(v, depth+1) for v in value[:20]]
    if isinstance(value, str):
        return clean_text(value)
    if value is None or isinstance(value, (bool,int,float)):
        return value
    return '[unsupported]'


async def capture(request):
    """Read only a bounded prefix and replay consumed ASGI messages unchanged downstream."""
    content_type = request.headers.get('content-type','').split(';')[0].lower()
    allowed = content_type in ('application/json','application/x-www-form-urlencoded','text/plain')
    result = dict(query=clean(dict(request.query_params.multi_items())), body=None,
                  content_type=content_type[:80], truncated=False)
    if not allowed:
        result['body_status'] = 'unsupported' if content_type else 'absent'
        return result
    original = request.receive
    buffered, prefix, more = [], bytearray(), True
    while more and len(prefix) < MAX_BYTES:
        message = await original()
        buffered.append(message)
        if message['type'] != 'http.request':
            break
        chunk = message.get('body',b'')
        remaining = MAX_BYTES-len(prefix)
        prefix.extend(chunk[:remaining])
        more = message.get('more_body',False)
        result['truncated'] |= len(chunk) > remaining
    result['truncated'] |= bool(more)
    # No raw body is retained in evidence; replay is solely for the application handler.
    pending = iter(buffered)
    async def replay():
        return next(pending, None) or await original()
    request._receive = replay
    raw = bytes(prefix)
    result['body_prefix_sha256'] = hashlib.sha256(raw).hexdigest()
    result['captured_bytes'] = len(raw)
    text = raw.decode('utf-8',errors='replace')
    try:
        if content_type == 'application/json':
            value = json.loads(text)
        elif content_type == 'application/x-www-form-urlencoded':
            value = dict(parse_qsl(text,max_num_fields=20))
        else:
            value = text
        result['body'] = clean(value)
    except (ValueError, RecursionError):
        # An incomplete JSON prefix can split credential fields; do not log raw fragments.
        result['body_status'] = 'unparseable_or_truncated'
    return result
