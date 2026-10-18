"""Self-contained desktop backend. Its stdout is a private readiness channel to Tauri."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading


def main():
    parser = argparse.ArgumentParser(description='FocusMail desktop backend')
    parser.add_argument('--version', action='version', version='FocusMail backend 1.0.0')
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        directory.chmod(0o700)
        os.umask(0o077)
    token = secrets.token_urlsafe(48)
    os.environ['DATA_DIR'] = str(directory)
    os.environ['FOCUSMAIL_DESKTOP'] = 'true'
    os.environ['FOCUSMAIL_DESKTOP_TOKEN'] = token
    os.environ['APP_PASSWORD'] = token
    os.environ['DEMO_MODE'] = str(args.demo).lower()
    # Desktop credentials belong to this profile; do not inherit a shell's key.
    os.environ.pop('CREDENTIAL_KEY', None)
    os.environ.pop('COOKIE_SECURE', None)

    # Parent death / crash closes stdin, preventing orphan mail backends.
    def watch_parent():
        try:
            while os.read(sys.stdin.fileno(), 1):
                pass
        finally:
            os._exit(0)
    threading.Thread(target=watch_parent, daemon=True).start()

    import uvicorn
    from backend.main import app
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != 'nt': sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', args.port))
    port = sock.getsockname()[1]

    class DesktopServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                print(json.dumps({'port': port, 'token': token}), flush=True)

    server = DesktopServer(uvicorn.Config(app, host='127.0.0.1', port=port,
        loop='asyncio', http='h11', ws='none', access_log=False, log_level='warning', proxy_headers=False))
    try: server.run(sockets=[sock])
    except KeyboardInterrupt: pass


if __name__ == '__main__':
    main()
