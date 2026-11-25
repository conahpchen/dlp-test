"""OpenAI-compatible JSON decision provider; never executes model-generated code."""
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from research.model_budget import ModelBudget, budget_path


MAX_PROMPT_BYTES = 96_000


class ModelProviderError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'Model provider returned HTTP {status_code}')


class ModelResponseError(RuntimeError):
    pass


@lru_cache(maxsize=8)
def _budget(state_path, provider, name, account):
    return ModelBudget.configured(Path(state_path))


class Model:
    def __init__(self, transport=None, budget=None):
        self.transport = transport
        self.last_response_metadata = {}
        self.base = os.environ.get('RESEARCH_MODEL_BASE_URL', 'https://api.deepseek.com').rstrip('/')
        self.name = os.environ.get('RESEARCH_MODEL', 'deepseek-flash')
        self.key = os.environ.get('RESEARCH_MODEL_API_KEY', '')
        self.read_timeout = int(os.environ.get('RESEARCH_MODEL_TIMEOUT_SECONDS', '15'))
        url = urlsplit(self.base)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('RESEARCH_MODEL_BASE_URL must be an HTTPS API base URL without credentials/query')
        if not self.name or not self.key:
            raise ValueError('Set RESEARCH_MODEL and RESEARCH_MODEL_API_KEY securely')
        if not 1 <= self.read_timeout <= 180:
            raise ValueError('Set RESEARCH_MODEL_TIMEOUT_SECONDS between 1 and 180')

        account = hashlib.sha256(self.key.encode()).hexdigest()
        self.budget = budget if budget is not None else _budget(str(budget_path()), self.base, self.name, account)

    def choose(self, messages):
        payload = {'model': self.name, 'messages': messages, 'temperature': 0,
                   'max_tokens': 6000, 'response_format': {'type': 'json_object'}}
        if urlsplit(self.base).hostname == 'api.deepseek.com':
            payload['thinking'] = {'type': 'disabled'}
        prompt_bytes = len(json.dumps(messages, ensure_ascii=False).encode())
        if prompt_bytes > MAX_PROMPT_BYTES:
            raise ValueError('Model prompt byte budget exceeded')
        reserved_input = prompt_bytes + 32 * len(messages) + 128
        result, metadata = self.budget.choose(payload, reserved_input, lambda: self._send(payload))
        self.last_response_metadata = metadata
        return result

    def _send(self, payload):
        timeout = httpx.Timeout(15, read=self.read_timeout)
        with httpx.Client(timeout=timeout, transport=self.transport) as client:
            response = client.post(self.base + '/chat/completions',
                                   headers={'Authorization': 'Bearer ' + self.key},
                                   json=payload)
        # Do not echo provider errors which may contain credentials or reflected input.
        if response.status_code != 200:
            raise ModelProviderError(response.status_code)
        try:
            data = response.json()
            choice = data['choices'][0]
            content = choice['message']['content']
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelResponseError('Model returned an invalid completion') from None
        if not isinstance(content, str) or not content.strip() or choice.get('finish_reason') == 'length':
            raise ModelResponseError('Model returned truncated or empty structured output')
        metadata = dict(model=data.get('model'), usage=data.get('usage'),
                                           finish_reason=choice.get('finish_reason'), content_characters=len(content))
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            raise ModelResponseError('Model returned invalid JSON content') from None
        if not isinstance(result, dict):
            raise ModelResponseError('Model output must be a JSON object')
        return result, metadata


