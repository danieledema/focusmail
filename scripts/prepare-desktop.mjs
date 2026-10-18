// Run on the target OS: PyInstaller cannot cross-compile a Python runtime.
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const frontend = path.join(root, 'frontend');
function run(command, args, cwd=root) {
  const result = spawnSync(command, args, {cwd, stdio:'inherit', env:process.env});
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.FOCUSMAIL_PYTHON || (fs.existsSync(localPython) ? localPython : process.platform === 'win32' ? 'python' : 'python3');
const rust = spawnSync('rustc', ['-vV'], {encoding:'utf8'});
if (rust.error || rust.status !== 0) throw new Error('Install Rust and put rustc on PATH before building desktop.');
const target = rust.stdout.match(/^host: (.+)$/m)?.[1];
if (!target) throw new Error('Could not identify the Rust host target.');
if (process.env.CARGO_BUILD_TARGET && process.env.CARGO_BUILD_TARGET !== target) throw new Error('Build the Python sidecar on the target OS/architecture; cross-compilation is not supported.');
run(process.execPath, [path.join(frontend,'node_modules/vite/bin/vite.js'),'build'], frontend);
const output = path.join(root, 'build', 'desktop-sidecar');
run(python, ['-m','PyInstaller','--noconfirm','--clean','--onefile','--name','focusmail-backend',
  '--distpath',output,'--workpath',path.join(root,'build','pyinstaller'),'--specpath',path.join(root,'build'),
  '--paths',root,'--exclude-module','backend.tests','--exclude-module','pytest','--exclude-module','_pytest','--collect-submodules','backend','--collect-submodules','sqlalchemy.dialects.sqlite',
  '--collect-data','tzdata','--add-data',path.join(frontend,'dist')+path.delimiter+'frontend/dist',
  path.join(root,'backend','desktop_entry.py')]);
const ext = process.platform === 'win32' ? '.exe' : '';
const binary = path.join(output,'focusmail-backend'+ext);
const destination = path.join(frontend,'src-tauri','binaries',`focusmail-backend-${target}${ext}`);
fs.mkdirSync(path.dirname(destination),{recursive:true});
fs.copyFileSync(binary,destination);
if(process.platform !== 'win32') fs.chmodSync(destination,0o755);
run(destination,['--version']);
console.log(`Desktop backend prepared for ${target}.`);
