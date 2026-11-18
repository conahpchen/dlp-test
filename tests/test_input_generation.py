"""Only classified visitor activity reaches the model and changes HTTP responses."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from business import app as biz
from research.api import current_lab
from research.generation import merge_scene_patch
from research.inputs import MAX_BYTES
from research.lab import ResearchLab
from tests.test_generative_research import SceneModel


class InputModel(SceneModel):
    def choose(self,messages):
        if 'interaction and lure author' not in messages[0]['content']:
            return super().choose(messages)
        self.calls.append(copy.deepcopy(messages))
        context=json.loads(messages[-1]['content'])
        event=context['events'][-1]
        value={key: event['what'][key] for key in ('activity', 'component') if key in event['what']}
        node=copy.deepcopy(context['current_scene']['nodes'][1])
        node['body']='本次查询的合成业务记录：'+json.dumps(value,ensure_ascii=False)
        services=[]
        if event['what']['resource'].startswith('service/'):
            path=event['what']['resource'].removeprefix('service/')
            node['body'] += ' 关联接口 /service/'+path
            services=[dict(id=event['what']['method'].lower()+'-'+path.replace('/','-'),method=event['what']['method'],path=path,node_id=node['id'],data=dict(value,status='recorded'))]
        return dict(actor_hypothesis='unknown',action='reveal_clue',document_id=context['documents'][0]['id'],
                    reason='Respond to the actual visitor query',evidence_event_ids=[event['event_id']],
                    scene_patch={'nodes':[node],'services':services},response_node=node['id'],
                    intent='Interpret supplied business query',engagement_goal='Continue the query through related records',
                    expected_next_nodes=['overview'])


@pytest.fixture
def setup(tmp_path,monkeypatch):
    model=InputModel()
    monkeypatch.setenv('RESEARCH_STATE_FILE',str(tmp_path/'research.db'))
    monkeypatch.setenv('BUSINESS_STATE_FILE','')
    monkeypatch.setenv('RESEARCH_ADAPTIVE_ENABLED','1')
    monkeypatch.setenv('HONEYNET_CONTROL_TOKEN','test-input-control')
    monkeypatch.delenv('HONEYNET_PG_DSN',raising=False)
    monkeypatch.setattr(biz,'_engine',None)
    monkeypatch.setattr('research.model.Model',lambda:model)
    client=TestClient(biz.app)
    headers={'x-control-token':'test-input-control'}
    r=client.post('/api/research/runs',json={},headers=headers)
    r.raise_for_status()
    scenario=r.json()
    return client,scenario,model,headers


def test_two_inputs_generate_two_distinct_replies(setup):
    client,scenario,model,headers=setup
    root=scenario['entry'].rsplit('/',1)[0]
    first=client.post(root+'/interact',json={'message':'查找部署回滚说明'})
    second=client.post(root+'/interact',json={'message':'继续查询数据库备份记录'})
    assert first.status_code==second.status_code==200
    assert 'deployment_rollback' in first.json()['content'] and 'database_backup' in second.json()['content']
    assert first.json()['content']!=second.json()['content']
    report=client.get('/api/research/runs/'+scenario['run_id'],headers=headers).json()
    assert report['engagement']['input_turns_responded']==2
    assert [d['revision'] for d in report['decisions']]==[1,2]


def test_query_creates_real_simulated_service_and_post_updates_data(setup):
    client,scenario,model,headers=setup
    root=scenario['entry'].rsplit('/',1)[0]
    first=client.get(root+'/service/config',params={'component':'database','token':'visitor-secret'})
    assert first.status_code==200
    assert first.json()['data']=={'activity':'component_lookup','component':'database','status':'recorded'}
    post=client.post(root+'/service/config',json={'component':'cache','command':'cat /etc/passwd'})
    assert post.status_code==200
    assert root+'/service/config' in post.json()['content']
    assert post.json()['data']['component']=='cache'
    assert 'cat /etc/passwd' not in str(model.calls)
    session=client.cookies.get('research_session')
    lab=current_lab()
    restored=ResearchLab(lab.path)
    assert restored.visit(scenario['run_id'],'service/config',session,'127.0.0.1','browser','POST')[1]['data']['component']=='cache'
    assert restored.visit(scenario['run_id'],'service/config','other-session','127.0.0.1','browser','POST')[0]==404
    report=client.get('/api/research/runs/'+scenario['run_id'],headers=headers).json()
    assert 'visitor-secret' not in json.dumps(report)
    assert report['engagement']['simulated_service_deliveries']>=2


def test_form_and_text_inputs_and_secret_redaction(setup):
    client,scenario,model,headers=setup
    root=scenario['entry'].rsplit('/',1)[0]
    form=client.post(root+'/interact',data={'message':'继续查询工单','password':'do-not-store'})
    assert form.status_code==200 and 'ticket_lookup' in form.json()['content']
    text=client.post(root+'/interact',content='查看变更记录 password=do-not-store',headers={'content-type':'text/plain'})
    assert text.status_code==200 and 'change_lookup' in text.json()['content']
    assert 'do-not-store' not in str(model.calls)
    page=client.get(scenario['entry'],headers={'accept':'text/html'})
    assert '<form' in page.text and root+'/interact' in page.text


def test_input_capture_replays_business_body_unchanged(setup):
    client,scenario,model,headers=setup
    client.post('/api/research/runs/'+scenario['run_id']+'/activate',headers=headers).raise_for_status()
    login=client.post('/api/login',json={'username':'dev01','password':'dev01-password'})
    assert login.status_code==200
    auth={'Authorization':'Bearer '+login.json()['token']}
    title='long body replay'
    r=client.post('/api/tickets',json={'title':title,'description':'x'*(MAX_BYTES+1000)},headers=auth)
    assert r.status_code==200
    detail=client.get('/api/tickets/'+r.json()['key'],headers=auth).json()
    assert len(detail['description'])==MAX_BYTES+1000
    report=client.get('/api/research/runs/'+scenario['run_id'],headers=headers).json()
    assert 'dev01-password' not in json.dumps(report)
    assert 'Bearer '+login.json()['token'] not in json.dumps(report)
    assert any(e['what'].get('input',{}).get('truncated') for e in report['events'])


def test_incomplete_json_is_not_logged_as_raw_secret_fragment(setup):
    client,scenario,model,headers=setup
    root=scenario['entry'].rsplit('/',1)[0]
    client.post(root+'/interact',content='{"password":"private-fragment',headers={'content-type':'application/json'})
    assert 'private-fragment' not in str(model.calls)


def test_service_paths_and_source_references_are_constrained(setup):
    client,scenario,model,headers=setup
    plan=current_lab().plan(scenario['run_id'])
    node=plan['scene']['nodes'][0]
    for path in ('../api/research','/api/research','config?token=x','config//other'):
        with pytest.raises(ValueError):
            merge_scene_patch(plan['scene'],{'nodes':[node],'services':[dict(id='test',method='GET',path=path,node_id=node['id'],data={})]},plan['documents'])


def test_unknown_payload_keys_paths_and_text_never_reach_model(setup):
    client, scenario, model, _ = setup
    root = scenario['entry'].rsplit('/', 1)[0]
    marker = 'UnrecognizablePrivateMaterial'
    client.post(root + '/service/' + marker, json={marker: marker, 'message': marker})
    assert marker not in str(model.calls)
    context = next(json.loads(call[-1]['content']) for call in reversed(model.calls)
                   if 'interaction and lure author' in call[0]['content'])
    assert context['events'][-1]['what']['resource'] == 'unknown'
    assert context['events'][-1]['what']['activity'] == 'unknown'
