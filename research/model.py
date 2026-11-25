"""OpenAI-compatible structured inference with bounded calls and optional SSE."""
import asyncio
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx

from research.model_budget import InferenceUnavailable, ModelBudget, budget_path


MAX_PROMPT_BYTES = 96_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_MODEL_READ_TIMEOUT_SECONDS = 600


class ModelProviderError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'Model provider returned HTTP {status_code}')


class ModelResponseError(RuntimeError):
    def __init__(self, message, code='model_response_invalid', usage=None):
        self.code = code
        self.usage = usage
        super().__init__(message)


class ModelTimeout(httpx.TimeoutException):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


MODEL_FAILURES = (InferenceUnavailable, httpx.RequestError, ModelProviderError, ModelResponseError)


def failure_code(error):
    if isinstance(error, (InferenceUnavailable, ModelTimeout, ModelResponseError)):
        return error.code
    if isinstance(error, httpx.TimeoutException):
        return 'model_timeout'
    if isinstance(error, httpx.RequestError):
        return 'model_connection'
    if isinstance(error, ModelProviderError):
        if error.status_code in (401, 403):
            return 'model_auth'
        return 'model_rate_limit' if error.status_code == 429 else 'model_provider_error'
    return 'model_response_invalid'


def _integer(name, default, minimum, maximum):
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f'Set {name} between {minimum} and {maximum}')
    return value


@lru_cache(maxsize=8)
def _budget(state_path, provider, name, account):
    return ModelBudget.configured(Path(state_path))


def _provider_failure(error):
    return (isinstance(error, httpx.RequestError)
            or isinstance(error, ModelProviderError) and (error.status_code == 429 or error.status_code >= 500))


async def _chunks(response):
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ModelResponseError('Model response exceeds byte limit')
        yield chunk


async def _events(response):
    pending, event = b'', []
    async for chunk in _chunks(response):
        lines = (pending + chunk).split(b'\n')
        pending = lines.pop()
        for line in lines:
            line = line.rstrip(b'\r')
            if line.startswith(b'data:'):
                event.append(line[5:].lstrip(b' '))
            elif not line and event:
                yield b'\n'.join(event)
                event.clear()
    if pending.startswith(b'data:'):
        event.append(pending[5:].lstrip(b' '))
    if event:
        yield b'\n'.join(event)


class Model:
    def __init__(self, transport=None, budget=None):
        self.transport = transport
        self.last_response_metadata = {}
        self.progress = None
        self.base = os.environ.get('RESEARCH_MODEL_BASE_URL', 'https://api.deepseek.com').rstrip('/')
        self.name = os.environ.get('RESEARCH_MODEL', 'deepseek-flash')
        self.key = os.environ.get('RESEARCH_MODEL_API_KEY', '')
        self.read_timeout = _integer('RESEARCH_MODEL_TIMEOUT_SECONDS', 15, 1, MAX_MODEL_READ_TIMEOUT_SECONDS)
        self.total_timeout = _integer('RESEARCH_MODEL_TOTAL_TIMEOUT_SECONDS', self.read_timeout + 30, 1, 900)
        self.draft_tokens = _integer('RESEARCH_MODEL_DRAFT_MAX_TOKENS', 3500, 512, 16000)
        self.review_tokens = _integer('RESEARCH_MODEL_REVIEW_MAX_TOKENS', 800, 128, 8000)
        url = urlsplit(self.base)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('RESEARCH_MODEL_BASE_URL must be an HTTPS API base URL without credentials/query')
        if not self.name or not self.key:
            raise ValueError('Set RESEARCH_MODEL and RESEARCH_MODEL_API_KEY securely')
        thinking = os.environ.get('RESEARCH_MODEL_THINKING', 'auto')
        field = os.environ.get('RESEARCH_MODEL_THINKING_FIELD', 'thinking')
        if thinking not in ('auto', 'enabled', 'disabled', 'omit') or field not in (
            'thinking', 'enable_thinking', 'chat_template_kwargs'
        ):
            raise ValueError('Invalid model thinking configuration')
        if thinking == 'auto':
            thinking = 'disabled' if url.hostname == 'api.deepseek.com' else 'omit'
        enabled = thinking == 'enabled'
        self.thinking = {} if thinking == 'omit' else {field: {
            'thinking': {'type': thinking},
            'enable_thinking': enabled,
            'chat_template_kwargs': {'enable_thinking': enabled},
        }[field]}
        stream = os.environ.get('RESEARCH_MODEL_STREAM', '0')
        if stream not in ('0', '1'):
            raise ValueError('Set RESEARCH_MODEL_STREAM to 0 or 1')
        self.streaming = stream == '1'
        stream_usage = os.environ.get('RESEARCH_MODEL_STREAM_USAGE', '1')
        if stream_usage not in ('0', '1'):
            raise ValueError('Set RESEARCH_MODEL_STREAM_USAGE to 0 or 1')
        self.stream_usage = stream_usage == '1'

        account = hashlib.sha256(self.key.encode()).hexdigest()
        self.budget = budget if budget is not None else _budget(str(budget_path()), self.base, self.name, account)

    def choose(self, messages):
        return self._choose(messages, 6000)

    def choose_for_phase(self, messages, phase):
        limit = self.draft_tokens if phase == 'draft' else self.review_tokens
        return self._choose(messages, limit, phase)

    def _choose(self, messages, limit, phase=None):
        payload = {'model': self.name, 'messages': messages, 'temperature': 0,
                   'max_tokens': limit, 'response_format': {'type': 'json_object'}, **self.thinking}
        if self.streaming:
            payload['stream'] = True
            if self.stream_usage:
                payload['stream_options'] = {'include_usage': True}
        prompt_bytes = len(json.dumps(messages, ensure_ascii=False).encode())
        if prompt_bytes > MAX_PROMPT_BYTES:
            raise ValueError('Model prompt byte budget exceeded')
        reserved_input = prompt_bytes + 32 * len(messages) + 128
        self.last_response_metadata = {}
        start = monotonic()
        result, metadata = self.budget.choose(payload, reserved_input, lambda: self._send(payload, phase),
                                              counts_as_failure=_provider_failure)
        self.last_response_metadata = dict(metadata, prompt_bytes=prompt_bytes, elapsed_seconds=monotonic() - start)
        return result

    def _send(self, payload, phase):
        async def bounded():
            try:
                return await asyncio.wait_for(self._request(payload, phase), timeout=self.total_timeout)
            except TimeoutError:
                raise ModelTimeout('model_deadline') from None

        return asyncio.run(bounded())

    async def _request(self, payload, phase):
        def report(stage):
            if phase and self.progress:
                self.progress(f'{phase}_{stage}')

        timeout = httpx.Timeout(15, read=self.read_timeout)
        waiting = True
        report('waiting')
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                async with client.stream('POST', self.base + '/chat/completions',
                                         headers={'Authorization': 'Bearer ' + self.key}, json=payload) as response:
                    if response.status_code != 200:
                        raise ModelProviderError(response.status_code)
                    if self.streaming and 'text/event-stream' in response.headers.get('content-type', '').lower():
                        parts, finish, usage, model = [], None, None, None
                        thinking = False
                        async for raw in _events(response):
                            if raw == b'[DONE]':
                                break
                            frame = json.loads(raw)
                            if not isinstance(frame, dict):
                                raise ModelResponseError('Invalid model stream event')
                            waiting = False
                            usage = frame.get('usage') or usage
                            model = frame.get('model') or model
                            if frame.get('choices'):
                                choice = frame['choices'][0]
                                finish = choice.get('finish_reason') or finish
                                delta = choice.get('delta') or {}
                                content = delta.get('content')
                                if isinstance(content, str) and content:
                                    if not parts:
                                        report('receiving')
                                    parts.append(content)
                                elif not parts and not thinking and delta.get('reasoning_content'):
                                    thinking = True
                                    report('reasoning')
                        if finish is None:
                            raise ModelResponseError('Model stream ended before completion')
                        content = ''.join(parts)
                    else:
                        body = bytearray()
                        async for chunk in _chunks(response):
                            if waiting:
                                waiting = False
                                report('receiving')
                            body.extend(chunk)
                        data = json.loads(body)
                        choice = data['choices'][0]
                        content, finish = choice['message']['content'], choice.get('finish_reason')
                        usage, model = data.get('usage'), data.get('model')
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ModelTimeout('model_connect_timeout') from None
        except httpx.ReadTimeout:
            raise ModelTimeout('model_first_response_timeout' if waiting else 'model_response_idle') from None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise ModelResponseError('Model returned an invalid completion') from None
        if finish == 'length':
            raise ModelResponseError('Model reached its output token limit', 'model_output_limit', usage)
        if not isinstance(content, str) or not content.strip():
            raise ModelResponseError('Model returned empty structured output', usage=usage)
        try:
            result = json.loads(content)
        except ValueError:
            raise ModelResponseError('Model returned invalid JSON content', usage=usage) from None
        if not isinstance(result, dict):
            raise ModelResponseError('Model output must be a JSON object', usage=usage)
        return result, dict(model=model, usage=usage, finish_reason=finish, content_characters=len(content))
