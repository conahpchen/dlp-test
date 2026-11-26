"""Research claims require correlated evidence, not model text or HTTP status alone."""
import copy

import pytest
from fastapi.testclient import TestClient

from business import app as biz
from dws.world import World
from experiments.research_loop import local_origin, model_visitor
from replay.harness import validate_events
from research.api import current_lab
from research.analysis import analyze
from research.lab import ResearchLab


@pytest.fixture
def lab(tmp_path):
    return ResearchLab(tmp_path / 'research.db')


@pytest.fixture
def world(demo_seed):
    return World(copy.deepcopy(demo_seed))


def visit(lab, run, resource, session='a', agent='browser'):
    return lab.visit(run, resource, session, '198.51.100.42', agent)


def deliver(lab, world, variant='agent_note'):
    scenario = lab.create(world, variant)
    run = scenario['run_id']
    _, index = visit(lab, run, 'index')
    path = index['documents'][0]['href'].split(f'/decoy/{run}/')[1]
    _, doc = visit(lab, run, path)
    return run, doc


def test_grounding_and_read_only(lab, world):
    before = world.meta.copy()
    run, doc = deliver(lab, world)
    plan = lab.plan(run)
    fact = next(f for f in world.all_current_cls('data') if f.fact_id == plan['documents'][0]['fact_id'])
    assert doc['content'] == fact.payload['content']
    assert world.meta == before
    assert all(not d.get('canary') for d in plan['documents'])


def test_delivery_not_success_and_deduplicated_follow(lab, world):
    run, doc = deliver(lab, world)
    assert lab.report(run)['metrics']['followed_deliveries'] == 0
    resource = doc['verification_link'].split(f'/decoy/{run}/')[1]
    for _ in range(2):
        assert visit(lab, run, resource)[0] == 200
    report = lab.report(run)
    assert report['metrics'] == dict(deliveries=1, eligible_deliveries=1, followed_deliveries=1, follow_rate=1)
    assert report['execution_verified'] is False
    assert report['profiles'][0]['hypothesis'] == 'unknown'
    validate_events(report['events'])
    assert '198.51.100.42' not in str(report)
    assert resource.split('/')[1] not in str(report)


def test_callback_bound_to_delivery_run_and_session(lab, world):
    run, doc = deliver(lab, world)
    resource = doc['verification_link'].split(f'/decoy/{run}/')[1]
    other = lab.create(world, 'agent_note')['run_id']
    assert visit(lab, run, resource, session='b')[0] == 404
    assert visit(lab, other, resource)[0] == 404
    assert visit(lab, run, 'beacon/invented')[0] == 404
    assert lab.report(run)['metrics']['followed_deliveries'] == 0


def test_plain_control_has_no_instruction_or_eligible_delivery(lab, world):
    run, doc = deliver(lab, world, 'plain')
    assert 'verification_link' not in doc and 'maintenance_note' not in doc
    assert lab.report(run)['metrics']['follow_rate'] is None


def test_checkpoint_survives_new_lab(lab, world):
    run, _ = deliver(lab, world)
    restored = ResearchLab(lab.path)
    assert restored.report(run) == lab.report(run)


def test_probe_and_self_claim_are_only_hypotheses(lab, world):
    run = lab.create(world, 'plain')['run_id']
    for path in ('.env', '.git/config', 'wp-admin'):
        assert visit(lab, run, path)[0] == 404
    visit(lab, run, 'index', session='b', agent='Example-Agent')
    profiles = lab.report(run)['profiles']
    assert [p['hypothesis'] for p in profiles] == ['scanner_suspected', 'ai_agent_suspected']
    assert all(p['confidence'] == 'low' and p['evidence'] for p in profiles)
    event = lab.report(run)['events'][0]
    assert analyze([event] * 10)[0]['actions'] == 1


def test_budget_atomic_and_persistent(lab, world, monkeypatch):
    from research import lab as capture

    clock = [1795478400]
    monkeypatch.setattr('research.lab.MAX_EVENTS', 2)
    monkeypatch.setattr(capture, 'time', lambda: clock[0])
    run = lab.create(world, 'plain')['run_id']
    visit(lab, run, 'index')
    visit(lab, run, 'index')
    assert visit(lab, run, 'index')[0] == 200
    restored = ResearchLab(lab.path)
    assert len(restored.report(run)['events']) == 2
    assert restored.capture_status(run)['dropped_total'] == 1
    clock[0] += capture.CAPTURE_WINDOW
    assert restored.capture_available(run)
    assert visit(restored, run, 'index')[0] == 200
    assert [event['evidence_ref'].rsplit('/', 1)[-1] for event in restored.report(run)['events']] == ['1', '2', '3']


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('RESEARCH_STATE_FILE', str(tmp_path / 'research.db'))
    monkeypatch.setenv('BUSINESS_STATE_FILE', '')
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED', '0')
    monkeypatch.setenv('HONEYNET_CONTROL_TOKEN', 'test-research-control')
    monkeypatch.delenv('HONEYNET_PG_DSN', raising=False)
    monkeypatch.setattr(biz, '_engine', None)
    return TestClient(biz.app)


def test_capture_limit_preserves_web_delivery_and_issued_canary(client, monkeypatch):
    monkeypatch.setattr('research.lab.MAX_EVENTS', 2)
    monkeypatch.setenv('HONEYNET_CANARY_HANDOFFS', '1')
    start = client.post('/api/research/runs', headers={'x-control-token': 'test-research-control'},
                        json={'mode': 'fixture', 'variant': 'agent_note'}).json()
    path = client.get(start['entry']).json()['documents'][0]['href']
    received = client.get(path).json()
    assert 'verification_link' in received
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()['content'] == received['content']
    assert 'verification_link' not in response.json()
    assert client.get(received['verification_link']).json() == {'received': True}
    lab = current_lab()
    assert lab.capture_status(start['run_id'])['dropped_total'] == 2
    (hit,) = lab.canary_uses(start['run_id'])
    assert hit['entry'] == 'web' and hit['associated']


def test_operator_can_shorten_canary_path_for_future_deliveries(client, monkeypatch):
    from audit.situation_model import assess_next
    from audit.situations import publish_reports
    from management.app import app as dashboard

    monkeypatch.setenv('HONEYNET_CANARY_HANDOFFS', '1')
    monkeypatch.setenv('HONEYNET_DASHBOARD_PASSWORD', 'synthetic-operator')
    manager = TestClient(dashboard)
    auth = ('operator', 'synthetic-operator')
    run = client.post('/api/research/runs', headers={'x-control-token': 'test-research-control'},
                      json={'mode': 'fixture', 'variant': 'agent_note'}).json()['run_id']
    evidence = f'/api/evidence/runs/{run}'
    policy = evidence + '/handoff-policy'
    path = f'/decoy/{run}/documents/0'
    short = ['web', 'llm', 'web']
    assert manager.put(policy, json={'route': short}).status_code == 401
    assert manager.put(policy, auth=auth, json={'route': ['web', 'llm', 'ssh']}).status_code == 422
    assert manager.put(policy, auth=auth, json={'route': short}).json()['route'] == short
    assert manager.get(evidence, auth=auth).json()['release']['handoff_route'] == short
    original = client.get(path).json()['verification_link']
    assert manager.put(policy, auth=auth, json={'route': ['web']}).json()['route'] == ['web']
    key = client.get(original).json()['next_hop']['verification_key']
    verification = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + key},
                               json={'model': 'corp-llm-7b'})
    assert verification.status_code == 401
    assert client.get(verification.json()['next_hop']['href']).json() == {'received': True}
    with TestClient(biz.app) as another:
        fresh = another.get(path).json()['verification_link']
        assert fresh != original
        assert another.get(fresh).json() == {'received': True}
    monkeypatch.setenv('HONEYNET_SITUATION_REPORT_FILE', str(current_lab().path.parent / 'situations.sqlite'))
    assert publish_reports() == 2

    class Analyst:
        name = 'synthetic-analyst'

        def choose(self, messages):
            import json

            observations = json.loads(messages[-1]['content'])['observations']
            assert observations['chain:protocols'] == 2
            assert run not in messages[-1]['content'] and key not in messages[-1]['content']
            return {'assessment': 'cross_protocol', 'severity': 'high', 'recommendations': [
                {'action': 'notify_operator', 'evidence': ['chain:protocols']},
                {'action': 'review_block', 'evidence': ['chain:protocols']},
            ]}

    assert assess_next(Analyst)
    report = next(row for row in manager.get('/api/situations', auth=auth).json()
                  if row['kind'] == 'cross_protocol')
    assert report['analysis']['status'] == 'ready'
    assert '提交阻断复核' in manager.get('/situations', auth=auth).text
    assert client.get('/api/situations').status_code == 404


def test_control_boundary_and_public_cookie(client, monkeypatch):
    assert client.post('/api/research/runs', json={'mode':'fixture', 'variant': 'plain'}).status_code == 404
    headers = {'x-control-token': 'test-research-control'}
    scenario = client.post('/api/research/runs', json={'mode':'fixture', 'variant': 'agent_note'}, headers=headers).json()
    run = scenario['run_id']
    assert client.get('/api/research/runs/' + run).status_code == 404
    assert client.get('/api/research/observations').status_code == 404
    assert client.get('/api/research/observations', headers=headers).status_code == 200
    assert client.post('/api/research/runs', json={'variant': 'shell'}, headers=headers).status_code == 422
    response = client.get(scenario['entry'])
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    document = client.get(response.json()['documents'][0]['href']).json()
    with current_lab().connect() as db:
        assert db.execute('SELECT COUNT(*) FROM lure_response_receipts').fetchone()[0] == 1
    assert client.get(document['verification_link']).status_code == 200
    token = document['verification_link'].rsplit('/', 1)[-1]
    cross = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + token},
                        json={'model': 'corp-llm-7b'})
    assert cross.status_code == 401
    with current_lab().connect() as db:
        use = db.execute('SELECT token_hash,entry FROM lure_uses').fetchone()
        receipt = db.execute('SELECT token_hash FROM lure_response_receipts').fetchone()
    assert use == (receipt[0], 'llm')
    assert client.get('/api/research/runs/' + run, headers=headers).json()['metrics']['followed_deliveries'] == 1
    monkeypatch.delenv('HONEYNET_CONTROL_TOKEN')
    assert client.get('/api/research/runs/' + run, headers=headers).status_code == 404


@pytest.mark.parametrize('origin', ['https://127.0.0.1', 'http://example.com', 'http://127.0.0.1/x',
                                    'http://user:pass@127.0.0.1', 'http://127.0.0.1?x=y'])
def test_runner_origin_restriction(origin):
    with pytest.raises(ValueError):
        local_origin(origin)


def test_model_cannot_request_undiscovered_path(client):
    class Model:
        def choose(self, messages):
            assert 'x-control-token' not in str(messages)
            return {'path': 'https://example.com/private'}
    result = model_visitor(client, '/decoy/example/index', Model())
    assert result['stopped'] == 'rejected_unobserved_or_external_path'
    assert result['actions'] == []


class DecisionModel:
    name = 'test-policy-model'

    def __init__(self, action='reveal_clue'):
        self.action = action
        self.calls = 0

    def choose(self, messages):
        import json
        self.calls += 1
        context = json.loads(messages[-1]['content'])
        return dict(actor_hypothesis='unknown', action=self.action,
                    document_id=context['documents'][-1]['id'], reason='Test policy from observed behavior',
                    evidence_event_ids=[context['events'][-1]['event_id']])


def test_model_policy_changes_response_and_is_session_scoped(lab, world):
    from research.controller import adapt
    run = lab.create(world, 'plain')['run_id']
    for _ in range(3):
        visit(lab, run, 'index')
    model = DecisionModel()
    assert adapt(lab, run, 'a', model)['status'] == 'applied'
    assert adapt(lab, run, 'a', model)['status'] == 'not_due'
    assert model.calls == 1
    _, index = visit(lab, run, 'index')
    selected = lab.plan(run)['documents'][-1]['id']
    assert index['documents'][0]['href'].endswith('/' + selected)
    _, doc = visit(lab, run, 'documents/' + selected)
    assert 'verification_link' in doc
    # Plain plan remains immutable and other sessions keep their own response.
    assert lab.plan(run)['variant'] == 'plain'
    assert 'verification_link' not in visit(lab, run, 'documents/' + selected, session='b')[1]
    visit(lab, run, 'index')
    assert adapt(lab, run, 'a', DecisionModel('continue_dialogue'))['revision'] == 2
    assert visit(lab, run, 'index')[0] == 200
    assert visit(lab, run, 'index', session='b')[0] == 200
    assert ResearchLab(lab.path).report(run)['policies'][0]['revision'] == 2


@pytest.mark.parametrize('failure', ['invented_fact', 'invented_evidence', 'external_action', 'provider_error'])
def test_bad_model_decisions_cannot_change_policy(lab, world, failure):
    from research.controller import adapt
    run = lab.create(world, 'plain')['run_id']
    for _ in range(3):
        visit(lab, run, 'index')

    class Bad(DecisionModel):
        def choose(self, messages):
            if failure == 'provider_error':
                raise RuntimeError('sensitive provider response must not be logged')
            value = super().choose(messages)
            if failure == 'invented_fact':
                value['document_id'] = 'unknown'
            if failure == 'invented_evidence':
                value['evidence_event_ids'] = ['invented']
            if failure == 'external_action':
                value['action'] = 'execute_shell'
            return value
    result = adapt(lab, run, 'a', Bad())
    assert result['status'] == 'rejected'
    assert not lab.report(run)['policies']
    assert 'sensitive provider response' not in str(lab.report(run))


def test_inflight_inference_reserves_budget(lab, world):
    from research.controller import adapt
    run = lab.create(world, 'plain')['run_id']
    for _ in range(3):
        visit(lab, run, 'index')

    class Concurrent(DecisionModel):
        def choose(self, messages):
            assert adapt(lab, run, 'a', DecisionModel())['status'] == 'not_due'
            return super().choose(messages)
    assert adapt(lab, run, 'a', Concurrent())['status'] == 'applied'
    assert len(lab.report(run)['decisions']) == 1


def test_online_request_trigger(client, monkeypatch):
    model = DecisionModel()
    monkeypatch.setattr('research.model.Model', lambda: model)
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED', '1')
    headers = {'x-control-token': 'test-research-control'}
    scenario = client.post('/api/research/runs', json={'mode':'fixture', 'variant': 'plain'}, headers=headers).json()
    for _ in range(3):
        client.get(scenario['entry']).raise_for_status()
    assert model.calls == 1
    index = client.get(scenario['entry']).json()
    document = client.get(index['documents'][0]['href']).json()
    assert 'verification_link' in document
    report = client.get('/api/research/runs/' + scenario['run_id'], headers=headers).json()
    assert report['decisions'][0]['status'] == 'applied'
    assert report['events'][-1]['what']['policy_revision'] == 1


def test_deepseek_compatible_transport(monkeypatch):
    import httpx
    from research.model import Model
    monkeypatch.setenv('RESEARCH_MODEL_API_KEY', 'test-only-provider-key')
    monkeypatch.delenv('RESEARCH_MODEL_BASE_URL', raising=False)
    monkeypatch.delenv('RESEARCH_MODEL', raising=False)

    def handler(request):
        import json
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert request.headers['authorization'] == 'Bearer test-only-provider-key'
        assert json.loads(request.content)['model'] == 'deepseek-flash'
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"done":true}'}}]})
    assert Model(transport=httpx.MockTransport(handler)).choose([]) == {'done': True}


def test_delivered_canary_survives_later_policy_change(lab, world):
    from research.controller import adapt
    run, doc = deliver(lab, world)
    visit(lab, run, 'index')
    assert adapt(lab, run, 'a', DecisionModel('continue_dialogue'))['status'] == 'applied'
    resource = doc['verification_link'].split(f'/decoy/{run}/')[1]
    assert visit(lab, run, resource)[0] == 200
    assert lab.report(run)['metrics']['followed_deliveries'] == 1


def test_rejected_decision_preserves_previous_policy(lab, world):
    from research.controller import adapt
    run = lab.create(world, 'plain')['run_id']
    for _ in range(3):
        visit(lab, run, 'index')
    adapt(lab, run, 'a', DecisionModel())
    before = lab.report(run)['policies']
    for _ in range(3):
        visit(lab, run, 'index')
    bad = DecisionModel('execute_shell')
    assert adapt(lab, run, 'a', bad)['status'] == 'rejected'
    assert lab.report(run)['policies'] == before


def test_run_inference_budget_limits_actual_calls(lab, world):
    from research.controller import adapt
    run = lab.create(world, 'plain')['run_id']
    model = DecisionModel('continue_dialogue')
    for _ in range(21):
        for _ in range(3):
            visit(lab, run, 'index')
        adapt(lab, run, 'a', model)
    assert model.calls == 20
    assert len(lab.report(run)['decisions']) == 20


def test_model_budget_renews_without_blocking_new_sessions(lab, world, monkeypatch):
    from research import controller

    clock = [1795478400]
    monkeypatch.setattr(controller, 'time', lambda: clock[0])
    monkeypatch.setattr(controller, 'SESSION_DECISIONS', 1)
    monkeypatch.setattr(controller, 'RUN_DECISIONS', 2)
    run = lab.create(world, 'plain')['run_id']
    model = DecisionModel('continue_dialogue')
    for session, status in [('a', 'applied'), ('a', 'budget_exhausted'),
                            ('b', 'applied'), ('c', 'budget_exhausted')]:
        assert visit(lab, run, 'index', session=session)[0] == 200
        assert controller.adapt(lab, run, session, model, interval=1)['status'] == status
    assert model.calls == 2
    clock[0] += controller.DECISION_WINDOW
    assert controller.adapt(lab, run, 'a', model, interval=1)['status'] == 'applied'
    assert model.calls == 3



def test_probe_statistics_do_not_classify_an_agent_or_group_sessions_by_network():
    from research.analysis import session_agent_assessments

    def event(identifier, session, port, probe=None):
        return dict(event_id=identifier, session=session, dst={"port": port}, src={"ip": "192.0.2.0/24"},
                    what={"kind": "web.request", "probe": probe})

    events = [event(str(i), "visitor-a" if i < 2 else "visitor-b", 8000 + i) for i in range(4)]
    events.extend(event("probe-" + str(i), "visitor-c", 8080, probe)
                  for i, probe in enumerate(("test-target-a", "test-target-b", "test-target-c")))
    profiles = session_agent_assessments(events, [])
    assert profiles["visitor-a"]["signals"] == profiles["visitor-b"]["signals"] == {}
    assert "multi_target_probe" in profiles["visitor-c"]["signals"]
    assert not profiles["visitor-c"]["suspected"]
