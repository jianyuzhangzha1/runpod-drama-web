import importlib, json, sys, time
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def studio(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path)); monkeypatch.setenv('DEMO_MODE','true')
    sys.path.insert(0,str(Path(__file__).parents[1])); sys.modules.pop('app',None)
    app=importlib.import_module('app')
    return app

def seed(client):
    p=client.post('/api/projects',json={'title':'测试短片'}).json()
    s=client.post(f'/api/projects/{p["id"]}/shots',json={'title':'测试镜头','image_prompt':'山谷','video_prompt':'雾移动','narration':'天亮了。'}).json()
    return p,s

def wait(client,p,j):
    for _ in range(200):
        item=next(x for x in client.get('/api/projects/'+p['id']).json()['jobs'] if x['id']==j['id'])
        if item['state'] in ('completed','failed','paused','uncertain'): return item
        time.sleep(.1)
    raise AssertionError('worker timeout')

def test_demo_end_to_end(studio):
    with TestClient(studio.app) as c:
        assert c.get('/').status_code==200
        p,s=seed(c)
        assert c.post(f'/api/shots/{s["id"]}/generate/video').status_code==422
        j=c.post(f'/api/shots/{s["id"]}/generate/image').json()
        assert wait(c,p,j)['state']=='completed'
        j=c.post(f'/api/shots/{s["id"]}/generate/video').json()
        assert wait(c,p,j)['state']=='completed'
        j=c.post(f'/api/projects/{p["id"]}/assemble').json()
        result=wait(c,p,j); assert result['state']=='completed',result
        output=json.loads(result['output'])
        assert c.get('/media/'+output['file']).status_code==200
        assert '天亮了' in c.get('/media/'+output['subtitles']).text
        assert c.get('/api/projects/'+p['id']).json()['shots'][0]['video']

def test_validation_and_guard(studio):
    c=TestClient(studio.app);p,s=seed(c)
    assert c.post(f'/api/projects/{p["id"]}/shots',json={'duration':7}).status_code==422
    assert c.post(f'/api/shots/{s["id"]}/image',files={'file':('bad.png',b'bad')}).status_code==422
    assert c.post('/api/projects',json={'title':'x'},headers={'Origin':'https://evil.test'}).status_code==403
    assert c.get('/media/studio.db').status_code==404

def test_ambiguous_submit_and_recovery(studio,monkeypatch):
    monkeypatch.setattr(studio,'DEMO',False);monkeypatch.setattr(studio,'KEY','test')
    c=TestClient(studio.app);p,s=seed(c)
    j=c.post(f'/api/shots/{s["id"]}/generate/image').json()
    with patch.object(studio.requests,'post',side_effect=TimeoutError()): studio.execute(j)
    assert studio.row('jobs',j['id'])['state']=='uncertain'
    assert c.post(f'/api/shots/{s["id"]}/generate/image').status_code==409
    assert c.post(f'/api/jobs/{j["id"]}/resume').status_code==409
    assert c.post(f'/api/jobs/{j["id"]}/recover',json={'remote_id':'known-job'}).status_code==200
    with patch.object(studio.requests,'get') as get,patch.object(studio.requests,'post') as post:
        get.return_value.json.return_value={'status':'IN_QUEUE'}
        studio.execute(studio.row('jobs',j['id'])); post.assert_not_called()
    assert studio.row('jobs',j['id'])['state']=='polling'

def test_restart_states(studio):
    c=TestClient(studio.app);p,s=seed(c);j=c.post(f'/api/shots/{s["id"]}/generate/image').json()
    studio.update_job(j['id'],state='submitting');studio.init()
    assert studio.row('jobs',j['id'])['state']=='uncertain'
    studio.update_job(j['id'],state='polling',remote_id='known');studio.init()
    assert studio.row('jobs',j['id'])['state']=='polling'

def test_real_completion_uses_saved_id(studio,monkeypatch):
    monkeypatch.setattr(studio,'DEMO',False);monkeypatch.setattr(studio,'KEY','test')
    c=TestClient(studio.app);p,s=seed(c);j=c.post(f'/api/shots/{s["id"]}/generate/image').json()
    studio.update_job(j['id'],state='polling',remote_id='saved-job')
    with patch.object(studio.requests,'get') as get,patch.object(studio.requests,'post') as post,patch.object(studio,'download',return_value='a'*32+'.png') as download:
        get.return_value.json.return_value={'status':'COMPLETED','output':{'image_url':'https://example.com/frame.png','cost':0.01}}
        studio.execute(studio.row('jobs',j['id']))
        post.assert_not_called();download.assert_called_once_with('https://example.com/frame.png','.png')
    assert studio.row('shots',s['id'])['image']=='a'*32+'.png'
    assert studio.row('jobs',j['id'])['cost']==0.01

def test_uploaded_image_requires_public_url(studio,monkeypatch):
    import io
    c=TestClient(studio.app);p,s=seed(c)
    stream=io.BytesIO();studio.Image.new('RGB',(32,32)).save(stream,format='PNG')
    assert c.post(f'/api/shots/{s["id"]}/image',files={'file':('frame.png',stream.getvalue())}).status_code==200
    monkeypatch.setattr(studio,'DEMO',False);monkeypatch.setattr(studio,'KEY','test');monkeypatch.setattr(studio,'PUBLIC','')
    assert c.post(f'/api/shots/{s["id"]}/generate/video').status_code==422
    monkeypatch.setattr(studio,'PUBLIC','https://media.example.com')
    j=c.post(f'/api/shots/{s["id"]}/generate/video').json()
    assert json.loads(j['payload'])['input']['image'].startswith('https://media.example.com/media/')
