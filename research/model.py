"""OpenAI-compatible JSON decision provider; never executes model-generated code."""
import json
import os
from threading import BoundedSemaphore
from urllib.parse import urlsplit

import httpx


MAX_PROMPT_BYTES = 96_000
IN_FLIGHT = BoundedSemaphore(2)


class Model:
    def __init__(self, transport=None):
        self.transport = transport
        self.last_response_metadata = {}
        self.base = os.environ.get('RESEARCH_MODEL_BASE_URL', 'https://api.deepseek.com').rstrip('/')
        self.name = os.environ.get('RESEARCH_MODEL', 'deepseek-flash')
        self.key = os.environ.get('RESEARCH_MODEL_API_KEY', '')
        url = urlsplit(self.base)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('RESEARCH_MODEL_BASE_URL must be an HTTPS API base URL without credentials/query')
        if not self.name or not self.key:
            raise ValueError('Set RESEARCH_MODEL and RESEARCH_MODEL_API_KEY securely')

    def choose(self, messages):
        payload = {'model': self.name, 'messages': messages, 'temperature': 0,
                   'max_tokens': 6000, 'response_format': {'type': 'json_object'}}
        if urlsplit(self.base).hostname == 'api.deepseek.com':
            payload['thinking'] = {'type': 'disabled'}
        if len(json.dumps(messages, ensure_ascii=False).encode()) > MAX_PROMPT_BYTES:
            raise ValueError('Model prompt byte budget exceeded')
        if not IN_FLIGHT.acquire(blocking=False):
            raise RuntimeError('Model concurrency budget exhausted')
        try:
            with httpx.Client(timeout=15, transport=self.transport) as client:
                response = client.post(self.base + '/chat/completions',
                                   headers={'Authorization': 'Bearer ' + self.key},
                                       json=payload)
        finally:
            IN_FLIGHT.release()
        # Do not echo provider errors which may contain credentials or reflected input.
        if response.status_code != 200:
            raise RuntimeError(f'Model provider returned HTTP {response.status_code}')
        data = response.json()
        choice = data['choices'][0]
        content = choice['message'].get('content') or ''
        self.last_response_metadata = dict(model=data.get('model'), usage=data.get('usage'),
                                           finish_reason=choice.get('finish_reason'), content_characters=len(content))
        if choice.get('finish_reason') == 'length' or not content.strip():
            raise RuntimeError('Model returned truncated or empty structured output')
        return json.loads(content)


