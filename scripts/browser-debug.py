"""Use an existing desktop profile in a local browser, with private launch auth.
Quit the desktop app first. The launch URL file is owner-readable and contains
only a one-use local session token, not email credentials.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    launch = ROOT / 'data' / 'browser-launch.url'
    launch.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen([sys.executable, '-m', 'backend.desktop_entry', '--data-dir', args.data_dir, '--port', str(args.port)],
                             cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        line = child.stdout.readline()
        if not line: raise RuntimeError('Backend did not start. Check the port and the startup error above.')
        ready = json.loads(line)
        fd = os.open(launch, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(f'http://127.0.0.1:{ready["port"]}/desktop/start/{ready["token"]}')
        print(f'Browser debug server ready on http://127.0.0.1:{ready["port"]}. Private launch URL saved locally.', flush=True)
        child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        launch.unlink(missing_ok=True)
        if child.stdin: child.stdin.close()
        if child.poll() is None:
            child.terminate(); child.wait(timeout=10)

if __name__ == '__main__': main()
