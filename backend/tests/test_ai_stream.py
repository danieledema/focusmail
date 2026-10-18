import asyncio
import json
import httpx
import pytest
from backend.database import Session
from backend.services import ai_service
from backend.tests.test_ai_server import prepare,mock_server


def events(client,tid):
    response=client.post('/api/ai/chat/stream',json={'thread_id':tid,'message':'Help'})
    assert response.status_code==200
    return [json.loads(line) for line in response.text.splitlines()]


def sse(*chunks):
    return ''.join('data: '+(chunk if isinstance(chunk,str) else json.dumps(chunk))+'\n\n' for chunk in chunks)


def chunk(text,finish=None):
    return {'choices':[{'delta':{'content':text},'finish_reason':finish}]}


def test_stream_delivers_deltas_and_validated_draft(client,monkeypatch):
    tid=prepare(client,monkeypatch,'fictional-custom-key')
    def handler(request):
        assert json.loads(request.content)['stream'] is True
        assert request.headers['authorization']=='Bearer fictional-custom-key'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse(chunk('{"content":"Hel'),chunk('lo","draft":"Reply"}'),chunk('', 'stop'),'[DONE]'))
    mock_server(monkeypatch,handler)
    result=events(client,tid)
    assert result[0]=={'type':'status','stage':'connecting'}
    assert {'type':'status','stage':'waiting'} in result
    assert {'type':'status','stage':'responding'} in result
    assert result[-1]=={'type':'result','content':'Hello','draft':'Reply'}
    assert ''.join(e['text'] for e in result if e['type']=='delta')=='{"content":"Hello","draft":"Reply"}'

@pytest.mark.parametrize('exception,code',[(httpx.ConnectTimeout,'connect_timeout'),(httpx.ConnectError,'connection_error'),(httpx.ReadError,'network_error')])
def test_stream_identifies_network_failures_without_echoing_secrets(client,monkeypatch,exception,code):
    tid=prepare(client,monkeypatch)
    def handler(request): raise exception('fictional-secret')
    mock_server(monkeypatch,handler)
    result=events(client,tid)
    assert result[-1]['type']=='error' and result[-1]['code']==code
    assert 'fictional-secret' not in str(result)


def test_stream_accepts_buffered_compatible_server(client,monkeypatch):
    tid=prepare(client,monkeypatch)
    mock_server(monkeypatch,lambda request:httpx.Response(200,json={'choices':[{'message':{'content':'Buffered answer'}}]}))
    assert events(client,tid)[-1]=={'type':'result','content':'Buffered answer','draft':None}


def test_truncated_stream_cannot_be_used_as_a_finished_draft(client,monkeypatch):
    tid=prepare(client,monkeypatch)
    mock_server(monkeypatch,lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse(chunk('{"content":"Partial","draft":"Unsafe"}'))))
    result=events(client,tid)
    assert result[-1]['type']=='error' and 'before the reply finished' in result[-1]['message']
    assert not any(e['type']=='result' for e in result)


def test_stream_provider_error_remains_specific_and_redacted(client,monkeypatch):
    tid=prepare(client,monkeypatch)
    mock_server(monkeypatch,lambda request:httpx.Response(401,json={'error':{'message':'fictional-secret'}}))
    result=events(client,tid)
    assert result[-1]['code']=='provider_error' and 'API key' in result[-1]['message']
    assert 'fictional-secret' not in str(result)


def test_stream_total_deadline_survives_keepalive_bytes(client,monkeypatch):
    prepare(client,monkeypatch)
    original=ai_service.get_setting
    monkeypatch.setattr(ai_service,'get_setting',lambda db,key,default: .04 if key=='ai_timeout_seconds' else original(db,key,default))
    closed=[]
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b': keepalive\n\n'
                await asyncio.sleep(.005)
        async def aclose(self): closed.append(True)
    mock_server(monkeypatch,lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=SlowBody()))
    async def run():
        with Session() as db: return [event async for event in ai_service.stream_chat(db,[],'Help',[])]
    result=asyncio.run(run())
    assert result[-1]['code']=='reply_timeout'
    assert 'may still be working' in result[-1]['message']
    assert closed


def test_closing_stream_cancels_upstream_socket(client,monkeypatch):
    prepare(client,monkeypatch)
    closed=[]
    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(60)
            yield b''
        async def aclose(self): closed.append(True)
    mock_server(monkeypatch,lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=WaitingBody()))
    async def run():
        with Session() as db:
            stream=ai_service.stream_chat(db,[],'Help',[])
            assert (await anext(stream))['stage']=='connecting'
            assert (await anext(stream))['stage']=='waiting'
            await stream.aclose()
    asyncio.run(run())
    assert closed


def test_timeout_preference_defaults_and_bounds(client):
    assert client.get('/api/state').json()['preferences']['ai_timeout_seconds']==300
    for value in (29,1801):
        assert client.put('/api/preferences',json={'ai_timeout_seconds':value}).status_code==422
    assert client.put('/api/preferences',json={'ai_timeout_seconds':600}).json()['ai_timeout_seconds']==600
