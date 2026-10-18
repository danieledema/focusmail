import httpx
import pytest
from backend.services.ai_service import provider_error

@pytest.mark.parametrize('status,error,expected',[
    (429, {'code':'credit_balance_exhausted','type':'insufficient_quota'}, 'credits are exhausted'),
    (429, {'code':'project_spend_limit_exceeded','type':'insufficient_quota'}, 'project has reached its spending limit'),
    (429, {'code':'organization_spend_limit_exceeded'}, 'organization has reached its spending limit'),
    (429, {'code':'organization_usage_limit_exceeded'}, 'API usage limit'),
    (429, {'type':'insufficient_quota'}, 'API quota is unavailable'),
    (429, {'code':'rate_limit_exceeded'}, 'temporary rate limit'),
    (401, {'message':'Incorrect API key: sk-never-echo-this'}, 'did not accept your API key'),
    (401, {'message':'Insufficient permissions. Missing scopes: model.request sk-never-echo-this'}, 'Chat completions Request'),
    (403, {}, 'does not allow this request'),
    (404, {'code':'model_not_found'}, 'model is unavailable'),
    (400, {'code':'unsupported_parameter'}, 'could not accept this request'),
    (503, {}, 'temporarily unavailable'),
])
def test_provider_errors_are_actionable_and_do_not_echo_secrets(status,error,expected):
    error.setdefault('message','provider text sk-never-echo-this private mail content')
    result=provider_error(httpx.Response(status,json={'error':error}))
    assert expected in result
    assert 'sk-never-echo-this' not in result and 'private mail content' not in result

@pytest.mark.parametrize('body',[b'not JSON',b'[]',b'{"error":"untrusted string"}'])
def test_malformed_error_is_safe(body):
    assert 'temporarily unavailable' in provider_error(httpx.Response(500,content=body))
