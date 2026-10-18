import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs,urlencode
from unittest.mock import Mock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select,create_engine,inspect
from backend.auth import fernet
from backend.database import Session
from backend.models import Account,Setting
from backend.services import oauth_service as oauth
from backend.main import app

GOOGLE_ID='123-test.apps.googleusercontent.com'
MICROSOFT_ID='11111111-2222-3333-4444-555555555555'

@pytest.fixture(autouse=True)
def real_connections_mode(client,monkeypatch):
    import backend.main as main
    import backend.routers.oauth as router
    monkeypatch.setattr(main,'DEMO',False)
    monkeypatch.setattr(router,'DEMO',False)


def configure(client,provider='google'):
    data={'client_id':GOOGLE_ID if provider=='google' else MICROSOFT_ID,'tenant':'common'}
    assert client.put('/api/oauth/providers/'+provider,json=data).status_code==200


def begin(client,provider='google',account_id=None):
    configure(client,provider)
    response=client.post('/api/oauth/'+provider+'/start',json={'account_id':account_id})
    assert response.status_code==200
    result=response.json();query=parse_qs(urlsplit(result['authorization_url']).query)
    return result,query


def callback(query,**kwargs):
    return httpx.get(query['redirect_uri'][0]+'?'+urlencode({'state':query['state'][0],**kwargs}),timeout=10)


def fake_identity(provider,config,token,nonce):
    return {'subject':'verified-person','email':'person@example.com','name':'Person','consumer':False}


def grant(provider='google',**changes):
    return {'access_token':'secret-access-token','refresh_token':'secret-refresh-token','expires_in':3600,
            'scope':oauth.PROVIDERS[provider]['scope'],'id_token':'verified-by-test-double',**changes}


def test_configuration_validation_and_secret_redaction(client):
    assert client.put('/api/oauth/providers/google',json={'client_id':'not-a-client'}).status_code==400
    assert client.put('/api/oauth/providers/microsoft',json={'client_id':'not-a-uuid'}).status_code==400
    response=client.put('/api/oauth/providers/google',json={'client_id':GOOGLE_ID,'client_secret':'registration-secret'})
    assert response.status_code==200 and response.json()['has_client_secret']
    assert 'registration-secret' not in response.text
    assert 'registration-secret' not in client.get('/api/oauth/providers').text
    with Session() as db:
        row=db.get(Setting,'secret.oauth.google.client_secret')
        assert row.value!='registration-secret'
        assert fernet.decrypt(row.value.encode())==b'registration-secret'
    client.put('/api/oauth/providers/google',json={'client_id':GOOGLE_ID})
    assert client.get('/api/oauth/providers').json()[0]['has_client_secret']


def test_start_needs_setup_and_uses_pkce_nonce_and_offline_access(client):
    assert client.post('/api/oauth/google/start',json={}).status_code==400
    result,query=begin(client)
    attempt=oauth._attempts[result['attempt_id']]
    assert query['code_challenge_method']==['S256']
    assert query['code_challenge']==[oauth.pkce_challenge(attempt['verifier'])]
    assert query['nonce']==[attempt['nonce']]
    assert query['access_type']==['offline']
    assert 'verifier' not in result and 'client_secret' not in query
    other=TestClient(app)
    assert other.get('/api/oauth/attempts/'+result['attempt_id']).status_code==404


def test_microsoft_authorization_uses_mail_scopes_and_localhost(client):
    result,query=begin(client,'microsoft')
    assert urlsplit(result['authorization_url']).netloc=='login.microsoftonline.com'
    assert urlsplit(query['redirect_uri'][0]).hostname=='localhost'
    assert 'offline_access' in query['scope'][0]
    assert 'IMAP.AccessAsUser.All' in query['scope'][0] and 'SMTP.Send' in query['scope'][0]
    assert 'client_secret' not in query


def test_callback_rejects_wrong_state_and_saves_verified_encrypted_tokens(client,monkeypatch):
    result,query=begin(client)
    monkeypatch.setattr(oauth,'_token_request',lambda *args:grant())
    monkeypatch.setattr(oauth,'_verify_identity',fake_identity)
    wrong=httpx.get(query['redirect_uri'][0]+'?state=wrong&code=secret-code')
    assert wrong.status_code==400
    assert client.get('/api/oauth/attempts/'+result['attempt_id']).json()['status']=='pending'
    response=callback(query,code='secret-code')
    assert response.status_code==200 and 'secret-code' not in response.text
    status=client.get('/api/oauth/attempts/'+result['attempt_id']).json()
    assert status['status']=='success'
    with Session() as db:
        account=db.get(Account,status['account_id'])
        assert account.auth_type=='oauth2' and account.provider=='google'
        assert account.imap_host=='imap.gmail.com'
        assert account.last_sync==0  # Signing in does not import mail.
        assert 'secret-access-token' not in account.oauth_credentials
        assert oauth._unpack(account)['refresh_token']=='secret-refresh-token'
        assert fernet.decrypt(account.password.encode())==b''
    state=client.get('/api/state').text
    assert 'secret-access-token' not in state and 'secret-refresh-token' not in state
    # A second callback can never create a second account.
    try: replay=callback(query,code='secret-code');assert replay.status_code==410
    except httpx.ConnectError: pass
    with Session() as db:
        assert len(list(db.scalars(select(Account).where(Account.auth_type=='oauth2'))))==1


def test_denial_and_cancel_never_save_account(client,monkeypatch):
    result,query=begin(client)
    exchange=Mock(side_effect=AssertionError('must not exchange a denied code'))
    monkeypatch.setattr(oauth,'_token_request',exchange)
    assert callback(query,error='access_denied',error_description='untrusted provider text').status_code==400
    status=client.get('/api/oauth/attempts/'+result['attempt_id']).json()
    assert status['status']=='error' and 'untrusted provider text' not in str(status)
    assert not exchange.called
    second,_=begin(client)
    assert client.delete('/api/oauth/attempts/'+second['attempt_id']).status_code==200
    assert client.get('/api/oauth/attempts/'+second['attempt_id']).json()['status']=='cancelled'


def test_reconnect_must_match_existing_identity(client,monkeypatch):
    result,query=begin(client)
    monkeypatch.setattr(oauth,'_token_request',lambda *args:grant())
    monkeypatch.setattr(oauth,'_verify_identity',fake_identity)
    callback(query,code='one')
    account_id=client.get('/api/oauth/attempts/'+result['attempt_id']).json()['account_id']
    result,query=begin(client,account_id=account_id)
    monkeypatch.setattr(oauth,'_verify_identity',lambda *args:{'subject':'someone-else','email':'other@example.com','name':'Other'})
    assert callback(query,code='two').status_code==400
    with Session() as db:
        assert db.get(Account,account_id).username=='person@example.com'
        assert len(list(db.scalars(select(Account).where(Account.auth_type=='oauth2'))))==1
    assert client.put(f'/api/accounts/{account_id}/password',json={'password':'unused'}).status_code==409


def test_refresh_rotation_is_persisted_and_is_only_on_demand(client,monkeypatch):
    configure(client)
    with Session() as db:
        account=Account(name='OAuth',username='person@example.com',password=fernet.encrypt(b'').decode(),
                        imap_host='imap.gmail.com',smtp_host='smtp.gmail.com',auth_type='oauth2',provider='google',oauth_subject='person',oauth_client_id=GOOGLE_ID,
                        oauth_credentials=oauth._pack({'access_token':'expired','refresh_token':'old-refresh','expires_at':0,'scope':oauth.PROVIDERS['google']['scope']}))
        db.add(account);db.commit();account_id=account.id
        exchange=Mock(return_value=grant(refresh_token='rotated-refresh',access_token='new-access'))
        monkeypatch.setattr(oauth,'_token_request',exchange)
        client.get('/api/state');client.get('/api/oauth/providers')
        assert not exchange.called
        assert oauth.access_token(account)=='new-access'
        assert oauth.access_token(account)=='new-access' and exchange.call_count==1
    with Session() as db:
        assert oauth._unpack(db.get(Account,account_id))['refresh_token']=='rotated-refresh'


def test_missing_mail_permissions_are_rejected():
    with pytest.raises(oauth.OAuthError): oauth._credentials('google',grant(scope='openid email'))
    with pytest.raises(oauth.OAuthError): oauth._credentials('microsoft',grant('microsoft',scope='openid offline_access'))
    with pytest.raises(oauth.OAuthError): oauth._credentials('google',grant(refresh_token=''))


def test_id_token_signature_audience_nonce_and_issuer_are_verified(monkeypatch):
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    class Keys:
        def __init__(self,*args,**kwargs): pass
        def get_signing_key_from_jwt(self,token): return SimpleNamespace(key=private.public_key())
    monkeypatch.setattr(oauth.jwt,'PyJWKClient',Keys)
    claims={'sub':'person','aud':GOOGLE_ID,'iss':'https://accounts.google.com','exp':int(time.time())+300,'iat':int(time.time()),'nonce':'expected','email':'person@example.com','email_verified':True}
    config={'client_id':GOOGLE_ID,'tenant':'common'}
    encode=lambda changes:jwt.encode({**claims,**changes},private,algorithm='RS256')
    assert oauth._verify_identity('google',config,encode({}),'expected')['email']=='person@example.com'
    for changes in [{'aud':'another-app'},{'nonce':'wrong'},{'iss':'https://attacker.example'},{'exp':0},{'email_verified':False}]:
        with pytest.raises(oauth.OAuthError): oauth._verify_identity('google',config,encode(changes),'expected')
    other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    with pytest.raises(oauth.OAuthError): oauth._verify_identity('google',config,jwt.encode(claims,other,algorithm='RS256'),'expected')
    microsoft={**claims,'aud':MICROSOFT_ID,'tid':oauth.CONSUMER_TENANT,'iss':f'https://login.microsoftonline.com/{oauth.CONSUMER_TENANT}/v2.0','preferred_username':'person@outlook.com'}
    token=jwt.encode(microsoft,private,algorithm='RS256')
    assert oauth._verify_identity('microsoft',{'client_id':MICROSOFT_ID,'tenant':'common'},token,'expected')['consumer']
    with pytest.raises(oauth.OAuthError): oauth._verify_identity('microsoft',{'client_id':MICROSOFT_ID,'tenant':'organizations'},token,'expected')


def test_imap_and_smtp_use_xoauth2_without_password_fallback(client,monkeypatch):
    from backend.services import imap_service,smtp_service
    imap=Mock()
    imap.capability.return_value=('OK',[b'IMAP4rev1 MOVE UIDPLUS'])
    monkeypatch.setattr(imap_service.imaplib,'IMAP4_SSL',lambda *args,**kwargs:imap)
    monkeypatch.setattr(imap_service,'access_token',lambda account:'oauth-access')
    monkeypatch.setattr(smtp_service,'access_token',lambda account:'oauth-access')
    with Session() as db:
        account=db.get(Account,1);account.auth_type='oauth2'
        imap_service.connect(account)
        assert imap.authenticate.call_args[0][0]=='XOAUTH2'
        assert b'auth=Bearer oauth-access' in imap.authenticate.call_args[0][1](b'')
        assert not imap.login.called
        smtp=Mock();smtp_service.authenticate(smtp,account)
        assert smtp.auth.call_args[0][0]=='XOAUTH2'
        assert 'auth=Bearer oauth-access' in smtp.auth.call_args[0][1]()
        assert smtp.auth.call_args[0][1](b'error-challenge')=='' and not smtp.login.called


def test_old_database_migration_keeps_password_accounts(tmp_path,monkeypatch):
    import backend.database as database
    legacy=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with legacy.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE accounts (id INTEGER PRIMARY KEY, password TEXT)')
        conn.exec_driver_sql("INSERT INTO accounts VALUES (1, 'original-encrypted-value')")
        conn.exec_driver_sql('CREATE TABLE messages (id INTEGER PRIMARY KEY)')
    monkeypatch.setattr(database,'engine',legacy)
    database.initialize_database();database.initialize_database()
    with legacy.connect() as conn:
        assert conn.exec_driver_sql('SELECT auth_type, provider, password FROM accounts').one()==('password','manual','original-encrypted-value')
    assert 'oauth_credentials' in {c['name'] for c in inspect(legacy).get_columns('accounts')}
