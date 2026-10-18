import json
import httpx
import pytest
from backend.database import Session
from backend.models import Setting
from backend.secrets import ai_api_key, saved_api_key, API_KEY_SETTING
from backend.services import ai_service
from backend.services.ai_config import normalize_base_url,DEFAULT_AI_BASE_URL

@pytest.mark.parametrize('value', ['file:///tmp/key','ftp://example.com/v1','https://user:secret@example.com/v1','https://example.com/v1?key=secret','https://example.com/v1#secret','http://example.com:wrong/v1','https://example.com/\nheader'])
def test_api_base_rejects_non_api_urls(value):
    with pytest.raises(ValueError): normalize_base_url(value)


def test_api_base_normalizes_endpoint_and_default_port():
    assert normalize_base_url(' https://API.OPENAI.COM:443/v1/ ')==DEFAULT_AI_BASE_URL
    assert normalize_base_url('http://127.0.0.1:8123/proxy/v1/chat/completions/')=='http://127.0.0.1:8123/proxy/v1'


def test_keys_are_atomic_encrypted_redacted_and_scoped_to_each_server(client,monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    assert client.put('/api/ai/key',json={'key':'fictional-openai-key'}).status_code==200
    response=client.put('/api/preferences',json={'ai_base_url':'https://custom.example/v1/','ai_model':'server/model','ai_api_key':'fictional-custom-key'})
    assert response.status_code==200 and response.json()['ai_base_url']=='https://custom.example/v1'
    assert 'fictional-custom-key' not in response.text and 'ai_api_key' not in response.json()
    with Session() as db:
        assert ai_api_key(db)=='fictional-custom-key'
        assert db.get(Setting,'ai_api_key') is None
        assert db.get(Setting,API_KEY_SETTING).value!='fictional-openai-key'
        for row in db.query(Setting).filter(Setting.key.like('secret.%')):
            assert row.value not in ('fictional-openai-key','fictional-custom-key')
    assert client.put('/api/preferences',json={'ai_base_url':DEFAULT_AI_BASE_URL}).status_code==200
    with Session() as db: assert ai_api_key(db)=='fictional-openai-key'
    assert client.put('/api/preferences',json={'ai_base_url':'https://custom.example/v1'}).status_code==200
    with Session() as db: assert ai_api_key(db)=='fictional-custom-key'


def prepare(client,monkeypatch,key=None):
    import backend.main as main
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setenv('OPENAI_API_KEY','fictional-openai-environment-key')
    payload={'ai_base_url':'http://127.0.0.1:8123/proxy/v1','ai_model':'server/model:latest'}
    if key is not None: payload['ai_api_key']=key
    assert client.put('/api/preferences',json=payload).status_code==200
    return client.get('/api/state').json()['thread']['id']


def mock_server(monkeypatch,handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(ai_service.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))

@pytest.mark.parametrize('key',[None,'fictional-custom-key'])
def test_custom_url_prefix_model_and_optional_authorization(client,monkeypatch,key):
    thread_id=prepare(client,monkeypatch,key)
    requests=[]
    def handler(request):
        requests.append(request)
        assert str(request.url)=='http://127.0.0.1:8123/proxy/v1/chat/completions'
        assert request.headers.get('authorization')==('Bearer '+key if key else None)
        payload=json.loads(request.content)
        assert payload['model']=='server/model:latest'
        assert payload['response_format']=={'type':'json_object'}
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"content":"Server answer","draft":"A draft"}'}}]})
    mock_server(monkeypatch,handler)
    response=client.post('/api/ai/chat',json={'thread_id':thread_id,'message':'Help me reply'})
    assert response.status_code==200 and response.json()=={'content':'Server answer','draft':'A draft'}
    assert len(requests)==1
    with Session() as db: assert ai_api_key(db)==key
    assert client.get('/api/state').json()['ai_ready']


def test_redirect_is_not_followed_and_error_never_echoes_server_secrets(client,monkeypatch):
    tid=prepare(client,monkeypatch,'fictional-custom-key')
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(307,headers={'Location':'https://api.openai.com/v1/chat/completions'})
    mock_server(monkeypatch,handler)
    response=client.post('/api/ai/chat',json={'thread_id':tid,'message':'Hello'})
    assert response.status_code==503 and 'redirect' in response.json()['detail']
    assert len(requests)==1
    assert 'fictional-custom-key' not in response.text


def test_custom_plain_text_response_still_works(client,monkeypatch):
    tid=prepare(client,monkeypatch)
    mock_server(monkeypatch,lambda request:httpx.Response(200,json={'choices':[{'message':{'content':'A helpful plain text answer'}}]}))
    response=client.post('/api/ai/chat',json={'thread_id':tid,'message':'Hello'})
    assert response.status_code==200 and response.json()=={'content':'A helpful plain text answer','draft':None}


def test_custom_error_names_the_custom_server_not_openai(client,monkeypatch):
    tid=prepare(client,monkeypatch)
    mock_server(monkeypatch,lambda request:httpx.Response(401,json={'error':{'message':'Bad key fictional-secret-never-echo'}}))
    response=client.post('/api/ai/chat',json={'thread_id':tid,'message':'Hello'})
    assert response.status_code==503
    assert 'Your AI server' in response.text and 'OpenAI' not in response.text
    assert 'fictional-secret-never-echo' not in response.text
