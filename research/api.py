"""Control-plane tools for builder/analyst agents and a separate visitor surface."""
import hashlib
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from research.model_input import model_event
from research.worker import enabled as async_adaptation, enqueue, result_status
from research.registration import install_registration
from research.lab import MYSQL_CANARY_ID_LENGTH, MYSQL_CANARY_PREFIX, ResearchLab


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


class AssociationRevision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    revision: int = Field(ge=0)
    associated: bool
    reason: str = Field(min_length=1, max_length=240)


class RunSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['ai', 'fixture'] = 'ai'
    goal: str = Field(default='生成连贯的研发运维业务场景，观察访客意图并持续生成可信线索，引导深入探索', min_length=1, max_length=2000)
    variant: Literal['plain', 'agent_note'] = 'plain'
    template_id: str | None = Field(default=None, max_length=32)
    template_revision: int | None = Field(default=None, ge=1)


def install_routes(app, engine_provider, event_provider=lambda: [], *, surface='integrated'):
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
            lab = current_lab()
            template = None
            if spec.template_id is not None or spec.template_revision is not None:
                if not spec.template_id or spec.template_revision is None:
                    raise HTTPException(422, 'Specify the template ID and revision together')
                try:
                    template = lab.template(spec.template_id, spec.template_revision)
                except KeyError as exc:
                    raise HTTPException(404, 'Unknown template revision') from exc
            variant = template['variant'] if template else spec.variant
            goal = template['goal'] if template else spec.goal
            if spec.mode == 'fixture':
                return lab.create(engine_provider().world, variant, template=template)
            from research.model import Model

            try:
                model = Model()
            except ValueError as exc:
                raise HTTPException(503, 'Configure the model service; AI mode never silently falls back to fixtures') from exc
            return lab.create(engine_provider().world, variant, model=model, goal=goal, template=template)
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

    @control_api.get('/runs/{run_id}/release')
    def release_status(run_id: str):
        try:
            return current_lab().release_status(run_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown run') from exc

    @control_api.post('/runs/{run_id}/rollback')
    def rollback(run_id: str):
        try:
            return current_lab().rollback(run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, 'Release cannot be rolled back') from exc

    @control_api.post('/active/reconcile')
    def reconcile():
        return current_lab().compensate_unready()

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

    @control_api.get('/runs/{run_id}/canaries')
    def list_canaries(run_id: str):
        try:
            return current_lab().canaries(run_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown run') from exc

    @control_api.post('/canaries/{credential_id}/revoke')
    def revoke_canary(credential_id: str = ApiPath(pattern=r'^[0-9a-f]{64}$')):
        try:
            return current_lab().revoke_canary(credential_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown canary') from exc

    @control_api.get('/runs/{run_id}/canary-uses')
    def canary_uses(run_id: str):
        try:
            return current_lab().canary_uses(run_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown run') from exc

    @control_api.post('/canary-uses/{use_id}/revisions')
    def revise_canary_use(use_id: int, edit: AssociationRevision):
        try:
            return current_lab().revise_canary_use(use_id, **edit.model_dump())
        except KeyError as exc:
            raise HTTPException(404, 'Unknown canary evidence') from exc
        except ValueError as exc:
            raise HTTPException(409, 'Evidence association changed') from exc

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

    if surface != 'visitor':
        app.include_router(control_api)
    if surface == 'control':
        return
    from research.observer import install_observer

    install_observer(app, current_lab, allow_model=surface == 'integrated')

    install_registration(app, current_lab)

    @app.api_route('/decoy/{run_id}/{resource:path}', methods=['GET', 'POST'])
    async def decoy(run_id: str, resource: str, request: Request):
        from research.inputs import capture, clean_text
        from starlette.concurrency import run_in_threadpool

        session = request.cookies.get('research_session', '')
        if not re.fullmatch('[a-f0-9]{32}', session):
            session = secrets.token_hex(16)
        peer = request.client.host if request.client else 'unknown'
        agent = request.headers.get('user-agent', '')[:256]
        try:
            lab = current_lab()
            plan = await run_in_threadpool(lab.plan, run_id)
            visitor_input = await capture(request)
            is_input = (request.method == 'POST' or bool(visitor_input['query'])
                        or resource.startswith('service/'))
            enabled = (surface == 'integrated' and os.environ.get(
                'RESEARCH_ADAPTIVE_ENABLED', '1' if plan.get('origin') == 'ai' else '0') == '1')
            outcome = None
            queued = False
            background_mode = surface == 'visitor' and async_adaptation()
            if is_input:
                event = await run_in_threadpool(lab.observe, run_id, session, peer, agent,
                                       dict(kind='research.input', method=request.method,
                                            resource=clean_text(resource)[:160], input=visitor_input))
                if (background_mode and plan.get('origin') == 'ai'
                        and model_event(event, plan)['what']['activity'] not in ('unknown', 'truncated')):
                    queued = await run_in_threadpool(enqueue, lab, run_id, session)
                if enabled and plan.get('origin') == 'ai':
                    from research.controller import adapt
                    from research.model import Model
                    try:
                        model = Model()
                    except ValueError:
                        model = None
                    if model:
                        outcome = await run_in_threadpool(adapt, lab, run_id, session, model, 1)
            policy_now = await run_in_threadpool(lab.policy, run_id, session)
            if resource == 'interact':
                if request.method == 'POST' and queued:
                    status, body = 202, dict(status='queued', result=f'/decoy/{run_id}/interact')
                elif request.method == 'GET' and background_mode:
                    job_status = await run_in_threadpool(result_status, lab, run_id, session)
                    if job_status == 'applied':
                        current = await run_in_threadpool(lab.policy, run_id, session)
                        if current.get('response_node'):
                            status, body = await run_in_threadpool(
                                lab.visit, run_id, 'scene/' + current['response_node'], session, peer, agent
                            )
                        else:
                            status, body = 503, dict(error='当前查询暂无法完成')
                    elif job_status == 'pending':
                        status, body = 202, dict(status='pending')
                    elif job_status == 'missing':
                        status, body = 404, dict(error='未找到查询任务')
                    else:
                        status, body = 503, dict(error='当前查询暂无法完成')
                elif request.method != 'POST':
                    status, body = 405, dict(error='请提交查询内容')
                elif not outcome or outcome['status'] != 'applied' or not policy_now.get('response_node'):
                    status, body = 503, dict(error='当前查询暂无法完成')
                else:
                    status, body = await run_in_threadpool(lab.visit, run_id, 'scene/' + policy_now['response_node'], session, peer, agent)
            else:
                status, body = await run_in_threadpool(lab.visit, run_id, resource, session, peer, agent, request.method, require_receipt=True)
            if enabled and not is_input:
                from research.controller import adapt
                from research.model import Model
                try:
                    model = Model()
                except ValueError:
                    model = None
                if model:
                    await run_in_threadpool(adapt, lab, run_id, session, model)
        except KeyError:
            return JSONResponse({'error': '接口不存在'}, status_code=404)
        except OverflowError:
            return JSONResponse({'error': '请求预算已用尽'}, status_code=429,
                                headers={'Retry-After': '3600', 'Cache-Control': 'no-store'})
        policy = current_lab().policy(run_id, session)
        scene = policy.get('scene')
        lead = None
        if scene:
            ids = {node['id'] for node in scene['nodes']}
            lead = next((node for node in policy.get('expected_next_nodes', []) if node in ids), None)
        lure_link = body.get('verification_link')
        receipt = None
        if status == 200 and lure_link:
            body['registration'] = {'optional': True, 'href': f'/decoy/{run_id}/register'}
            token = lure_link.rsplit('/', 1)[-1]
            receipt = BackgroundTask(lab.record_lure_response, run_id, session, token)
            if os.environ.get('HONEYNET_ENABLE_MYSQL_CANARY') == '1':
                short_id = hashlib.sha256(token.encode()).hexdigest()[:MYSQL_CANARY_ID_LENGTH]
                body['mysql_verification'] = {'username': MYSQL_CANARY_PREFIX + short_id, 'password': token}
        response = JSONResponse(body, status_code=status, background=receipt,
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
            rendered.background = receipt
            for key in ('set-cookie', 'cache-control', 'referrer-policy', 'link'):
                if key in response.headers:
                    rendered.headers[key] = response.headers[key]
            return rendered
        return response
