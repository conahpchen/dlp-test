"""Observe real business traffic and route it to an activated generative honeynet."""
import hashlib
import os
import re
import secrets
import time

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from research.bindings import HEADER, resolve
from research.lab import PROBES


def install_observer(app, lab_provider, *, allow_model):
    @app.middleware('http')
    async def observe_business(request, call_next):
        path = request.url.path
        # Control tools never become attacker evidence; decoy routes collect themselves.
        if path.startswith(('/api/research', '/decoy/', '/v1/')) or path in ('/api/events', '/api/events/ingest'):
            return await call_next(request)
        lab = lab_provider()
        session = request.cookies.get('research_session', '')
        if not re.fullmatch('[a-f0-9]{32}', session):
            session = secrets.token_hex(16)
        if path == '/' and HEADER in request.headers:
            try:
                binding = await run_in_threadpool(resolve, lab, request.headers[HEADER])
            except KeyError:
                return JSONResponse({'error': '版本绑定无效'}, status_code=404)
            try:
                run = await run_in_threadpool(lab.active_for, session, preferred=binding['run'])
            except ValueError:
                return JSONResponse({'error': '会话已绑定'}, status_code=409)
        else:
            run = await run_in_threadpool(lab.active_for, session)
        if not run:
            return await call_next(request)
        from research.inputs import capture
        visitor_input = await capture(request)
        policy = await run_in_threadpool(lab.policy, run, session)
        start = time.monotonic()
        if path == '/':
            response = RedirectResponse(f'/decoy/{run}/index', status_code=307)
        else:
            response = await call_next(request)
        route = request.scope.get('route')
        what = dict(kind='web.request', method=request.method, status=response.status_code,
                    resource=getattr(route, 'path', 'unmatched'),
                    query_keys=[k for k in request.query_params.keys() if re.fullmatch('[A-Za-z0-9_-]{1,32}', k)][:20],
                    auth_present=bool(request.headers.get('authorization')), path_hash=hashlib.sha256(path.encode()).hexdigest(),
                    probe=next((p for p in sorted(PROBES) if p in path.split('/')), None),
                    input=visitor_input, duration_ms=round((time.monotonic()-start)*1000),
                    policy_revision=policy.get('revision', 0))
        try:
            await run_in_threadpool(lab.observe, run, session,
                                   request.client.host if request.client else 'unknown',
                                   request.headers.get('user-agent','')[:256], what)
            if allow_model and os.environ.get('RESEARCH_ADAPTIVE_ENABLED', '1') == '1':
                from research.controller import adapt
                from research.model import Model

                try:
                    model = Model()
                except ValueError:
                    model = None
                if model:
                    await run_in_threadpool(adapt, lab, run, session, model)
        except OverflowError:
            # Telemetry cap must not make ordinary business endpoints unavailable.
            pass
        # Keep natural business status codes, but offer a model-authored next lead.
        latest = await run_in_threadpool(lab.policy, run, session)
        scene = latest.get('scene')
        if scene:
            known = {node['id'] for node in scene['nodes']}
            next_node = next((node for node in latest.get('expected_next_nodes', []) if node in known), scene['entry_node'])
            response.headers['Link'] = f'</decoy/{run}/scene/{next_node}>; rel="related"'
        response.set_cookie('research_session', session, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', path='/')
        return response
