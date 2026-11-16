"""Control-plane tools for builder/analyst agents and a separate visitor surface."""
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from research.lab import ResearchLab


@lru_cache(maxsize=4)
def _lab(path):
    return ResearchLab(path)


def current_lab():
    path = os.environ.get('RESEARCH_STATE_FILE', str(Path(__file__).resolve().parents[1] / 'var/research.db'))
    return _lab(path)


def control(request: Request):
    expected = os.environ.get('HONEYNET_CONTROL_TOKEN', '')
    if not expected or not secrets.compare_digest(expected, request.headers.get('x-control-token', '')):
        raise HTTPException(404, '接口不存在')


class AdaptSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session: str


class RunSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['ai', 'fixture'] = 'ai'
    goal: str = Field(default='生成连贯的研发运维业务场景，观察访客意图并持续生成可信线索，引导深入探索', min_length=1, max_length=2000)
    variant: Literal['plain', 'agent_note'] = 'plain'


def install_routes(app, engine_provider, event_provider=lambda: []):
    control_api = APIRouter(prefix='/api/research', dependencies=[Depends(control)])

    @control_api.get('/status')
    def status():
        from research.model import Model

        try:
            Model()
            configured = True
        except ValueError:
            configured = False
        return dict(adaptive_enabled=os.environ.get('RESEARCH_ADAPTIVE_ENABLED', '1') == '1',
                    default_mode='ai', objective='sustained_engagement', active_run=current_lab().active(),
                    model_configured=configured,
                    required_variables=['RESEARCH_MODEL_BASE_URL', 'RESEARCH_MODEL', 'RESEARCH_MODEL_API_KEY'],
                    inference_status='not_checked_by_status_endpoint')

    @control_api.post('/runs')
    def create(spec: RunSpec):
        try:
            if spec.mode == 'fixture':
                return current_lab().create(engine_provider().world, spec.variant)
            from research.model import Model

            try:
                model = Model()
            except ValueError as exc:
                raise HTTPException(503, 'Configure the model service; AI mode never silently falls back to fixtures') from exc
            return current_lab().create(engine_provider().world, spec.variant, model=model, goal=spec.goal)
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, 'Scene generation or validation failed; no run was created') from exc

    @control_api.post('/runs/{run_id}/activate')
    def activate(run_id: str):
        try:
            return current_lab().activate(run_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown run') from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @control_api.delete('/active')
    def deactivate():
        current_lab().deactivate()
        return dict(active_run=None)

    @control_api.get('/runs/{run_id}')
    def report(run_id: str):
        try:
            return current_lab().report(run_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown run') from exc

    @control_api.post('/runs/{run_id}/adapt')
    def adapt_run(run_id: str, spec: AdaptSpec):
        from research.controller import adapt
        from research.model import Model

        try:
            return adapt(current_lab(), run_id, spec.session, Model())
        except KeyError as exc:
            raise HTTPException(404, 'Unknown run') from exc
        except ValueError as exc:
            raise HTTPException(503, 'Model service configuration required') from exc

    @control_api.get('/observations')
    def observations():
        from research.analysis import analyze

        return dict(profiles=analyze(event_provider()),
                    interpretation='Session-level hypotheses; no automatic identity merge or proof of actor type.')

    app.include_router(control_api)
    from research.observer import install_observer

    install_observer(app, current_lab)

    @app.get('/decoy/{run_id}/{resource:path}')
    def decoy(run_id: str, resource: str, request: Request):
        session = request.cookies.get('research_session', '')
        if not re.fullmatch('[a-f0-9]{32}', session):
            session = secrets.token_hex(16)
        try:
            status, body = current_lab().visit(
                run_id, resource, session,
                request.client.host if request.client else 'unknown',
                request.headers.get('user-agent', '')[:256])
        except KeyError:
            return JSONResponse({'error': '接口不存在'}, status_code=404)
        except OverflowError:
            return JSONResponse({'error': '请求预算已用尽'}, status_code=429,
                                headers={'Retry-After': '3600', 'Cache-Control': 'no-store'})
        if os.environ.get('RESEARCH_ADAPTIVE_ENABLED', '1' if current_lab().plan(run_id).get('origin') == 'ai' else '0') == '1':
            from research.controller import adapt
            from research.model import Model

            try:
                adapt(current_lab(), run_id, session, Model())
            except ValueError:
                # Explicit configuration failure is visible on the control plane.
                # Existing grounded responses remain available.
                pass
        policy = current_lab().policy(run_id, session)
        scene = policy.get('scene')
        lead = None
        if scene:
            ids = {node['id'] for node in scene['nodes']}
            lead = next((node for node in policy.get('expected_next_nodes', []) if node in ids), None)
        response = JSONResponse(body, status_code=status,
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        if lead:
            response.headers['Link'] = f'</decoy/{run_id}/scene/{lead}>; rel="related"'
            try:
                current_lab().observe(run_id, session, request.client.host if request.client else 'unknown',
                                      request.headers.get('user-agent', '')[:256],
                                      dict(kind='research.lead', resource='related', offered_nodes=[lead], policy_revision=policy['revision']))
            except OverflowError:
                pass
        response.set_cookie('research_session', session, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', path=f'/decoy/{run_id}/')
        if 'text/html' in request.headers.get('accept', '') and status == 200:
            from research.render import html_response

            rendered = html_response(body)
            for key in ('set-cookie', 'cache-control', 'referrer-policy', 'link'):
                if key in response.headers:
                    rendered.headers[key] = response.headers[key]
            return rendered
        return response
