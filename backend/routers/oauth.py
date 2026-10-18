import re
import secrets
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from ..auth import DEMO
from ..database import Session
from ..schemas import OAuthConfiguration, OAuthStart
from ..services import oauth_service as oauth

router = APIRouter(prefix='/api/oauth', tags=['oauth'])
Provider = Literal['google','microsoft']
OWNER_COOKIE = 'focusmail_oauth_owner'

@router.get('/providers')
def providers():
    with Session() as db: return oauth.public_providers(db)

@router.put('/providers/{provider}')
def configure(provider: Provider, data: OAuthConfiguration):
    if DEMO: raise HTTPException(400, 'OAuth setup is disabled in demo mode')
    try:
        with Session() as db:
            oauth.save_configuration(db,provider,data)
            return next(p for p in oauth.public_providers(db) if p['id']==provider)
    except oauth.OAuthError as error: raise HTTPException(400,str(error))

@router.post('/{provider}/start')
def start(provider: Provider, data: OAuthStart, request: Request):
    if DEMO: raise HTTPException(400, 'Real account sign-in is disabled in demo mode')
    owner = request.cookies.get(OWNER_COOKIE,'')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,100}',owner): owner=secrets.token_urlsafe(32)
    try: result = oauth.start(provider,owner,data.account_id)
    except oauth.OAuthError as error: raise HTTPException(400,str(error))
    response = JSONResponse(result)
    response.set_cookie(OWNER_COOKIE,owner,httponly=True,samesite='strict',secure=request.url.scheme=='https',path='/api/oauth',max_age=660)
    response.headers['Cache-Control']='no-store'
    return response

@router.get('/attempts/{attempt_id}')
def status(attempt_id: str,request: Request):
    try: return oauth.status(attempt_id,request.cookies.get(OWNER_COOKIE,''))
    except oauth.OAuthError as error: raise HTTPException(404,str(error))

@router.delete('/attempts/{attempt_id}')
def cancel(attempt_id: str,request: Request):
    try: oauth.cancel(attempt_id,request.cookies.get(OWNER_COOKIE,'')); return {'ok':True}
    except oauth.OAuthError as error: raise HTTPException(409,str(error))
