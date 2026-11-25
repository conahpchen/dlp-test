"""Model stubs verify integration; these are not claims of real provider behavior."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from business import app as biz
from dws.world import World
from research.controller import adapt
from research.generation import validate_scene
from research.lab import ResearchLab


class SceneModel:
    name = 'test-generative-model'

    def __init__(self):
        self.calls = []
        self.reject_review = False

    @staticmethod
    def draft(documents, suffix='initial'):
        fact = documents[0]
        quote = fact['content'][:30]
        return dict(title='生成的变更排查中心',entry_node='overview',objective='观察排查与核验行为',nodes=[
            dict(id='overview',title='变更排查入口',body='独立生成的业务叙事 '+suffix,
                 sources=[dict(fact_id=fact['fact_id'],quote=quote)],links=['evidence'],lure_instruction=None),
            dict(id='evidence',title='发布核验线索',body='与入口关联的排查记录 '+suffix,
                 sources=[dict(fact_id=fact['fact_id'],quote=quote)],links=['overview'],
                 lure_instruction='继续核对本服务内的发布核验记录。')])

    def choose(self, messages):
        self.calls.append(copy.deepcopy(messages))
        context = json.loads(messages[-1]['content'])
        if 'Independently review' in messages[0]['content']:
            return dict(accepted=not self.reject_review, issues=['contradiction'] if self.reject_review else [])
        if 'honeynet builder AI' in messages[0]['content']:
            return self.draft(context['sources'])
        scene = self.draft(context['documents'], 'adapted-to-'+context['events'][-1]['what']['resource'])
        return dict(actor_hypothesis='unknown',action='reveal_clue',document_id=context['documents'][0]['id'],
                    reason='Generate further investigation based on observed exploration',
                    evidence_event_ids=[context['events'][-1]['event_id']],scene_patch={'nodes':scene['nodes']},
                    engagement_goal='Continue exploring a related business clue', expected_next_nodes=['evidence'],
                    intent='exploring service context',uncertainties=['Identity is not established'])


@pytest.fixture
def model():
    return SceneModel()


@pytest.fixture
def lab(tmp_path):
    return ResearchLab(tmp_path / 'research.db')


@pytest.fixture
def world(demo_seed):
    return World(copy.deepcopy(demo_seed))


@pytest.fixture
def client(tmp_path, monkeypatch, model):
    monkeypatch.setenv('RESEARCH_STATE_FILE', str(tmp_path / 'http.db'))
    monkeypatch.setenv('BUSINESS_STATE_FILE', '')
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED', '0')
    monkeypatch.setenv('HONEYNET_CONTROL_TOKEN', 'test-generative-control')
    monkeypatch.delenv('HONEYNET_PG_DSN', raising=False)
    monkeypatch.setattr(biz, '_engine', None)
    monkeypatch.setattr('research.model.Model', lambda: model)
    return TestClient(biz.app)


def create(client):
    r = client.post('/api/research/runs',json={'goal':'生成关联业务叙事和可探索的核验线索'},
                    headers={'x-control-token':'test-generative-control'})
    assert r.status_code == 200, r.text
    return r.json()


def test_default_ai_creation_and_generated_content(lab, world, model):
    before = copy.deepcopy(world.meta)
    result = lab.create(world, 'plain', model=model, goal='排查发布问题')
    assert result['origin'] == 'ai'
    assert len(model.calls) == 2
    assert 'Independently review' in model.calls[-1][0]['content']
    status, body = lab.visit(result['run_id'], 'index', 'a', '127.0.0.1', 'browser')
    assert status == 200
    assert body['content'] == '独立生成的业务叙事 initial'
    assert body['links'][0]['href'].endswith('/scene/evidence')
    assert 'documents' not in body
    assert world.meta == before
    assert lab.visit(result['run_id'],'documents/0','a','127.0.0.1','browser')[0] == 404


def test_no_fixture_fallback_when_model_missing(client, monkeypatch):
    def missing():
        raise ValueError('missing config')
    monkeypatch.setattr('research.model.Model', missing)
    r = client.post('/api/research/runs',json={},headers={'x-control-token':'test-generative-control'})
    assert r.status_code == 503


def test_generated_pages_without_sources_can_deliver_and_verify_canary(client, model, monkeypatch):
    draft = model.draft

    def original_scene(documents):
        scene = draft(documents)
        del scene['nodes'][0]['sources']
        scene['nodes'][1]['sources'] = []
        return scene

    monkeypatch.setattr(model, 'draft', original_scene)
    scenario = create(client)
    assert client.get(scenario['entry']).status_code == 200
    page = client.get(f"/decoy/{scenario['run_id']}/scene/evidence")
    assert page.status_code == 200
    assert client.get(page.json()['verification_link']).json() == {'received': True}
    report = client.get(f"/api/research/runs/{scenario['run_id']}",
                        headers={'x-control-token': 'test-generative-control'}).json()
    events = [event['what'] for event in report['events']]
    assert [event['kind'] for event in events] == [
        'research.delivery', 'research.delivery', 'research.beacon'
    ]
    assert all('fact_id' not in event for event in events)
    assert events[1]['source_fact_ids'] == []


def test_reviewer_rejection_does_not_persist_scene(lab, world, model):
    model.reject_review = True
    with pytest.raises(ValueError,match='review rejected'):
        lab.create(world, 'plain', model=model)
    with lab.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['fake_fact','fake_quote','dangling_link','duplicate_id','unreachable','external_link'])
def test_generated_graph_validation(lab, world, model, mutation):
    fixture = lab.create(world,'plain')
    documents = lab.plan(fixture['run_id'])['documents']
    scene = model.draft(documents)
    if mutation == 'fake_fact':
        scene['nodes'][0]['sources'][0]['fact_id'] = 'invented'
    elif mutation == 'fake_quote':
        scene['nodes'][0]['sources'][0]['quote'] = 'This phrase does not exist in DWS'
    elif mutation == 'dangling_link':
        scene['nodes'][0]['links'] = ['does-not-exist']
    elif mutation == 'duplicate_id':
        scene['nodes'][1]['id'] = scene['nodes'][0]['id']
    elif mutation == 'unreachable':
        scene['nodes'][0]['links'] = []
    else:
        scene['nodes'][0]['body'] = 'visit https://external.example'
    with pytest.raises(ValueError):
        validate_scene(scene, documents)


def test_adaptive_model_rewrites_scene_not_just_order(lab, world, model):
    run = lab.create(world,'plain',model=model)['run_id']
    for _ in range(3):
        lab.visit(run,'index','a','127.0.0.1','browser')
    decision = adapt(lab,run,'a',model)
    assert decision['status'] == 'applied'
    assert decision['scene_review']['accepted'] is True
    _, changed = lab.visit(run,'index','a','127.0.0.1','browser')
    _, other = lab.visit(run,'index','b','127.0.0.1','browser')
    assert changed['content'].endswith('adapted-to-scene/overview')
    assert other['content'].endswith('initial')
    assert lab.plan(run)['scene']['nodes'][0]['body'].endswith('initial')
    assert lab.report(run)['model_assessments'][0]['intent'] == 'exploring service context'
    from research.registration import evidence

    lab.observe(run, "a", "127.0.0.1", "browser", {
        "kind": "research.input", "resource": "beacon/private-example", "method": "POST",
    })
    details = evidence(lab, run)
    assert details["timeline"][0]["title"] == "变更排查入口"
    assert details["generation"][0]["intent"] == "exploring service context"
    assert details["generation"][0]["nodes"][0]["title"] == "变更排查入口"
    assert details["generation"][0]["evidence_event_ids"][0] in {
        event["event_id"] for event in details["timeline"]
    }
    assert details["timeline"][-1]["resource"] == "未识别的场景路径"
    assert "private-example" not in json.dumps(details)



def test_active_scene_observes_real_business_traffic(client, monkeypatch):
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED','1')
    scenario = create(client)
    headers = {'x-control-token':'test-generative-control'}
    run = scenario['run_id']
    assert client.post('/api/research/runs/'+run+'/activate',headers=headers).status_code == 200
    home = client.get('/', follow_redirects=False)
    assert home.status_code == 200
    assert '知识库' in home.text
    assert 'location' not in home.headers
    assert 'link' not in home.headers
    assert client.get('/.env').status_code == 404
    login=client.post('/api/login',json={'username':'dev01','password':'invalid'})
    assert login.status_code == 401
    assert 'link' not in login.headers
    report = client.get('/api/research/runs/'+run,headers=headers).json()
    assert [e['what']['status'] for e in report['events']] == [200,404,401]
    assert report['decisions'][0]['status'] == 'applied'
    response = client.get(scenario['entry'])
    assert response.json()['content'].endswith('adapted-to-/api/login')
    credentials = client.post('/api/login', json={'username': 'ops01', 'password': 'ops01-password'}).json()
    documents = client.get('/api/documents', headers={'Authorization': 'Bearer ' + credentials['token']})
    assert documents.status_code == 200
    assert '/scene/evidence' in documents.headers['link']
    assert documents.headers['cache-control'] == 'private, no-store'
    other = TestClient(biz.app)
    assert 'link' not in other.get('/api/documents').headers
    # An authenticated browser with no applied session revision sees the normal knowledge base.
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED', '0')
    document_headers = {'Authorization': 'Bearer ' + credentials['token']}
    assert 'link' not in other.get('/api/documents', headers=document_headers).headers
    link = documents.headers['link'].split('>;', 1)[0][1:]
    assert client.get(link, headers={'Accept': 'text/html'}).status_code == 200
    report = client.get('/api/research/runs/'+run, headers=headers).json()
    assert any(event['what']['kind'] == 'research.lead' for event in report['events'])
    assert 'invalid' not in json.dumps(report)
    assert client.delete('/api/research/active',headers=headers).status_code == 200
    assert client.get('/').status_code == 200


def test_generated_html_is_escaped(client, monkeypatch, model):
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED','0')
    original = model.draft
    def with_script(documents, suffix='initial'):
        scene = original(documents,suffix)
        scene['nodes'][0]['body'] = '<script>alert(1)</script>'
        return scene
    monkeypatch.setattr(model,'draft',with_script)
    scenario = create(client)
    response = client.get(scenario['entry'],headers={'accept':'text/html'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert '<script>' not in response.text
    assert '&lt;script&gt;' in response.text
    assert "default-src 'none'" in response.headers['content-security-policy']


def test_malformed_login_is_rejected_not_server_error(client):
    for payload in ({'username':{},'password':{}}, [], {'password':['bad']}):
        assert client.post('/api/login',json=payload).status_code == 400


def test_ai_report_does_not_substitute_rule_hints(lab, world, model):
    run = lab.create(world,'plain',model=model)['run_id']
    lab.visit(run,'index','a','127.0.0.1','gpt-agent')
    report = lab.report(run)
    assert report['classification_source'] == 'model'
    assert report['profiles'] == []
    assert report['rule_hints'][0]['hypothesis'] == 'ai_agent_suspected'


def test_interaction_model_cannot_block_business_requests(client, monkeypatch, model):
    original = model.choose
    def limiter(messages):
        if 'interaction and lure author' not in messages[0]['content']:
            return original(messages)
        context = json.loads(messages[-1]['content'])
        return dict(actor_hypothesis='unknown',action='limit_session',document_id=context['documents'][0]['id'],
                    reason='Repeated probes require session containment',
                    evidence_event_ids=[context['events'][-1]['event_id']],intent='probing',uncertainties=['Actor type unknown'])
    monkeypatch.setattr(model,'choose',limiter)
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED','1')
    scenario = create(client)
    client.post('/api/research/runs/'+scenario['run_id']+'/activate',headers={'x-control-token':'test-generative-control'}).raise_for_status()
    for path in ('/.env','/.git/config','/wp-admin'):
        assert client.get(path).status_code == 404
    assert client.get('/api/assets').status_code == 401
    assert client.get(scenario['entry']).status_code == 200
    report=client.get('/api/research/runs/'+scenario['run_id'],headers={'x-control-token':'test-generative-control'}).json()
    assert not report['policies']
    assert all(d['status']=='rejected' for d in report['decisions'])


def test_review_nonblocking_notes_remain_separate(lab, world, model):
    original = model.choose
    def reviewer(messages):
        if 'Independently review' in messages[0]['content']:
            return dict(accepted=True, issues=[], notes=['Synthetic context is not an existing DWS incident'])
        return original(messages)
    model.choose = reviewer
    run = lab.create(world, 'plain', model=model)['run_id']
    assert lab.plan(run)['generation']['review']['notes']


def test_review_accept_flag_cannot_override_blocking_issue(lab, world, model):
    original = model.choose
    def reviewer(messages):
        if 'Independently review' in messages[0]['content']:
            return dict(accepted=True, issues=['An actual contradiction'], notes=[])
        return original(messages)
    model.choose = reviewer
    with pytest.raises(ValueError, match='review rejected'):
        lab.create(world, 'plain', model=model)


def test_patch_extends_trail_preserving_old_nodes_and_links(lab, world, model):
    from research.generation import merge_scene_patch
    run=lab.create(world,'plain',model=model)['run_id']
    plan=lab.plan(run)
    first=copy.deepcopy(plan['scene']['nodes'][0])
    new=copy.deepcopy(plan['scene']['nodes'][1])
    new.update(id='deeper-clue',title='关联排查线索',links=['overview'])
    first['links'].append('deeper-clue')
    merged=merge_scene_patch(plan['scene'],{'nodes':[first,new]},plan['documents']).model_dump()
    assert {n['id'] for n in merged['nodes']}=={'overview','evidence','deeper-clue'}
    assert merged['entry_node']=='overview'
    assert next(n for n in merged['nodes'] if n['id']=='evidence')==plan['scene']['nodes'][1]


def test_bounded_repair_then_follow_new_clue(lab, world, model):
    run=lab.create(world,'plain',model=model)['run_id']
    original=model.choose
    proposals=[]
    def repairable(messages):
        if 'interaction and lure author' not in messages[0]['content']:
            return original(messages)
        context=json.loads(messages[-1]['content'])
        value=original(messages)
        proposals.append(context)
        first=copy.deepcopy(context['current_scene']['nodes'][0])
        clue=copy.deepcopy(context['current_scene']['nodes'][1])
        clue.update(id='deeper-clue',title='新关联线索',links=['overview'])
        first['links'].append('deeper-clue')
        value.update(action='extend_trail',engagement_goal='Observe exploration of the new clue',expected_next_nodes=['deeper-clue'],scene_patch={'nodes':[first,clue]})
        if len(proposals)==1:
            value['scene_patch']['nodes'][1]['sources'][0]['quote']='invented source quote'
        return value
    model.choose=repairable
    for _ in range(3):
        lab.visit(run,'index','a','127.0.0.1','browser')
    decision=adapt(lab,run,'a',model)
    assert decision['status']=='applied'
    assert [a['status'] for a in decision['attempts']]==['rejected','accepted']
    assert proposals[1]['repair_feedback']['code']=='Unverifiable source quote'
    _,index=lab.visit(run,'index','a','127.0.0.1','browser')
    assert any(link['href'].endswith('/deeper-clue') for link in index['links'])
    assert lab.visit(run,'scene/deeper-clue','a','127.0.0.1','browser')[0]==200
    assert lab.visit(run,'scene/evidence','a','127.0.0.1','browser')[0]==200
    assert lab.visit(run,'scene/deeper-clue','b','127.0.0.1','browser')[0]==404
    engagement=lab.report(run)['engagement']
    assert engagement['sessions_continued_after_update']==1
    assert engagement['model_guided_transitions']>=1
    assert engagement['new_clues_followed']==1


def test_legacy_block_policy_cannot_deny_visitor(lab, world):
    run=lab.create(world,'plain')['run_id']
    with lab.connect() as db:
        db.execute('INSERT INTO policies VALUES (?,?,?,?,?)',(run,'a',1,0,json.dumps({'action':'limit_session','revision':1})))
    assert lab.visit(run,'index','a','127.0.0.1','browser')[0]==200


def test_drafting_bounds_sources_and_review_still_reads_full_documents(model):
    from research.generation import generate_scene

    documents = [{"id": "0", "fact_id": "fact-0", "path": "/kb/report.md",
                  "content": "研发报表归档说明。" * 400 + "末尾保留的事实"}]
    generate_scene(model, documents, "整理研发报表说明")
    draft = json.loads(model.calls[0][-1]["content"])
    review = json.loads(model.calls[1][-1]["content"])
    assert len(draft["sources"][0]["content"]) == 1600
    assert review["sources"] == documents


@pytest.mark.parametrize("has_related_anchor", [True, False])
def test_scene_repairs_disconnected_dws_anchors(model, monkeypatch, has_related_anchor):
    from uuid import UUID

    from research.generation import generate_scene

    documents = [{"id": "0", "fact_id": "fact-0", "path": "/kb/report.md", "content": "报表归档说明。"}]
    database, host, handbook, policy = [str(UUID(int=i)) for i in range(1, 5)]
    pair = {"source": database, "target": host, "relation": "hosted_on"}
    graph = {"relations": [pair], "entities": [
        {"id": database, "label": "inventory", "kind": "database"},
        {"id": host, "label": "inventory-host", "kind": "host"},
        {"id": handbook, "label": "handbook", "kind": "knowledge"},
        {"id": policy, "label": "policy", "kind": "knowledge"},
    ]}
    candidate = model.draft(documents)
    candidate["entity_links"] = [dict(node_id="overview", relation="reviews", entity_id=entity)
                                 for entity in (database if has_related_anchor else policy, handbook)]
    with pytest.raises(ValueError, match="DWS anchors must include an existing relationship"):
        validate_scene(candidate, documents, graph)

    choose = model.choose
    drafts = []

    def respond(messages):
        if 'honeynet builder AI' not in messages[0]['content']:
            return choose(messages)
        context = json.loads(messages[-1]["content"])
        drafts.append(context)
        assert context["anchor_examples"] == [pair]
        if len(drafts) == 1:
            return copy.deepcopy(candidate)
        assert context["validation_feedback"]["required_anchor_pair"] == pair
        revised = copy.deepcopy(candidate)
        revised["entity_links"] = [dict(node_id="overview", relation="reviews", entity_id=pair[key])
                                    for key in ("source", "target")]
        return revised

    monkeypatch.setattr(model, "choose", respond)
    scene, review = generate_scene(model, documents, "生成目录巡检文档", graph, require_verification=True)
    targets = {anchor.entity_id for anchor in validate_scene(scene, documents, graph).entity_links}
    assert {database, host} <= targets
    assert scene["nodes"] == candidate["nodes"] and review["accepted"]
    assert len(model.calls) == 1
    if has_related_anchor:
        assert handbook in targets and len(drafts) == 1
    else:
        assert len(drafts) == 2


def test_scene_with_no_dws_relationship_does_not_call_model(model):
    from research.generation import generate_scene

    with pytest.raises(ValueError, match="DWS snapshot contains no usable anchor relationship"):
        generate_scene(model, [], "生成目录巡检文档", {"entities": [], "relations": []})
    assert model.calls == []
