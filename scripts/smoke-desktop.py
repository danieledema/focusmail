"""Exercise the frozen backend twice; never contact mail servers or OpenAI."""
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'build' / 'desktop-sidecar' / ('focusmail-backend.exe' if os.name == 'nt' else 'focusmail-backend')


def start(profile, log):
    process = subprocess.Popen([str(BINARY), '--data-dir', str(profile)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=log, text=True)
    ready_queue = queue.Queue()
    threading.Thread(target=lambda: ready_queue.put(process.stdout.readline()), daemon=True).start()
    try:
        ready = json.loads(ready_queue.get(timeout=60))
        client = httpx.Client(base_url=f'http://127.0.0.1:{ready["port"]}', follow_redirects=True)
        assert client.get('/api/state').status_code == 401
        assert client.get('/desktop/start/wrong-token').status_code == 401
        response = client.get('/desktop/start/' + ready['token'])
        assert response.status_code == 200, 'Desktop bootstrap failed'
        assert '<div id="root">' in response.text, 'Bundled frontend missing'
        assert client.get('/desktop/start/' + ready['token']).status_code == 401
        return process, client
    except Exception:
        process.stdin.close(); process.wait(timeout=10)
        raise


def stop(process, client):
    client.close()
    process.stdin.close()  # The exact parent-death cleanup used by the desktop host.
    process.wait(timeout=10)


def main():
    with tempfile.TemporaryDirectory(prefix='focusmail-desktop-smoke-') as temp:
        profile = Path(temp) / 'profile'
        with (Path(temp) / 'backend.log').open('w') as log:
            process, client = start(profile, log)
            try:
                state = client.get('/api/state').json()
                assert state['desktop'] and not state['demo'] and not state['accounts']
                account = {'name':'Smoke account','username':'smoke@example.invalid','password':'fictional-email-password',
                           'imap_host':'127.0.0.1','imap_port':1,'smtp_host':'127.0.0.1','smtp_port':1}
                assert client.post('/api/accounts', json=account).status_code == 200
                assert client.put('/api/ai/key', json={'key':'sk-fictional-smoke-key'}).status_code == 200
                providers=client.get('/api/oauth/providers').json()
                assert {p['id'] for p in providers}=={'google','microsoft'}
                assert client.put('/api/oauth/providers/google',json={'client_id':'smoke.apps.googleusercontent.com'}).status_code==200
                started=client.post('/api/oauth/google/start',json={}).json()
                assert started['authorization_url'].startswith('https://accounts.google.com/o/oauth2/v2/auth?')
                assert client.delete('/api/oauth/attempts/'+started['attempt_id']).status_code==200
                key_bytes = (profile / 'credential.key').read_bytes()
            finally:
                stop(process, client)
            process, client = start(profile, log)
            try:
                state = client.get('/api/state').json()
                assert state['accounts'][0]['username'] == 'smoke@example.invalid'
                assert state['accounts'][0]['last_sync'] == 0 and not state['accounts'][0]['sync_error']
                assert state['ai_key_saved'] and state['ai_ready']
                assert client.get('/api/oauth/providers').json()[0]['configured']
                assert 'fictional-email-password' not in str(state) and 'sk-fictional-smoke-key' not in str(state)
                assert (profile / 'credential.key').read_bytes() == key_bytes
                with sqlite3.connect(profile / 'focusmail.db') as db:
                    assert db.execute('SELECT password FROM accounts').fetchone()[0] != 'fictional-email-password'
                    assert db.execute("SELECT value FROM settings WHERE key='secret.openai_api_key'").fetchone()[0] != 'sk-fictional-smoke-key'
                # Explicit async sync stays readable and reports a safe local refusal.
                assert client.post('/api/sync').status_code == 200
                deadline = time.monotonic() + 5
                while client.get('/api/sync').json()['running'] and time.monotonic() < deadline:
                    assert client.get('/api/state').status_code == 200
                    time.sleep(.05)
                assert not client.get('/api/sync').json()['running']
                assert client.get('/api/state').json()['accounts'][0]['sync_error']
            finally:
                stop(process, client)
    print('PASS: bundled UI, private launch, credential persistence, manual sync, and parent-exit cleanup.')


if __name__ == '__main__':
    main()
