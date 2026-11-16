import json

from research.reporting import sanitize, write_report


def test_request_and_response_report(tmp_path):
    result = dict(mode='model',model='test-model',results=[dict(
        scenario={'entry':'/decoy/run/index'},visitors=[dict(stopped='model_done',actions=[dict(
            method='GET',path='/decoy/run/beacon/canaryvalue',status=200,
            response={'received':True,'token':'private','nested':{'password':'private'}})])],
        report={'metrics':{'followed_deliveries':1},'execution_verified':False})])
    output, markdown = write_report(result,tmp_path/'report.json')
    body = markdown.read_text()
    assert 'GET' in body and '200' in body and 'received' in body
    assert 'private' not in body and 'canaryvalue' not in body
    assert '[canary:' in body
    assert json.loads(output.read_text())['results'][0]['visitors'][0]['actions'][0]['response']['received'] is True


def test_blocked_report_does_not_invent_attack(tmp_path):
    _, markdown = write_report(dict(status='blocked',mode='model',preflight={'key_present':False}),tmp_path/'blocked.json')
    assert '未执行攻击请求' in markdown.read_text()


def test_untrusted_markdown_and_sensitive_headers():
    result = sanitize({'Authorization':'sensitive','nested':{'x-control-token':'sensitive'},'body':'/beacon/abc'})
    assert 'sensitive' not in str(result)


def test_model_visitor_preserves_observed_responses_on_model_error():
    import httpx
    from experiments.research_loop import model_visitor
    class Broken:
        def __init__(self):
            self.calls=0
        def choose(self,messages):
            self.calls+=1
            if self.calls==1:
                return {'path':'/decoy/test/index'}
            raise ValueError('invalid provider format')
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'content':'actual response'})),base_url='http://127.0.0.1') as client:
        report=model_visitor(client,'/decoy/test/index',Broken())
    assert report['stopped']=='model_error'
    assert report['actions'][0]['response']['content']=='actual response'
