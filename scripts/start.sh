#!/bin/zsh
set -eu
ROOT_DIR="${0:A:h:h}"
export MEDIALINK_PROJECT="$ROOT_DIR"
/bin/zsh "$ROOT_DIR/scripts/backup.sh"
/usr/bin/python3 - <<'PY'
import os, pathlib, plistlib, subprocess, time, urllib.request
root = pathlib.Path(os.environ['MEDIALINK_PROJECT'])
data = pathlib.Path.home() / 'Library/Application Support/MediaLink'
data.mkdir(parents=True, exist_ok=True, mode=0o700)
label = 'asia.viol1n.medialink'
plist = pathlib.Path.home() / 'Library/LaunchAgents' / (label + '.plist')
plist.parent.mkdir(parents=True, exist_ok=True)
previous = plistlib.loads(plist.read_bytes()) if plist.exists() else {}
environment = dict(previous.get('EnvironmentVariables', {}))
environment.update({'USER': os.environ.get('USER', pathlib.Path.home().name), 'PYTHONUNBUFFERED': '1'})
if 'MEDIALINK_ALLOWED_HOSTS' in os.environ:
    environment['MEDIALINK_ALLOWED_HOSTS'] = os.environ['MEDIALINK_ALLOWED_HOSTS']
configuration = {
    'Label': label, 'ProgramArguments': ['/bin/zsh', str(root/'scripts/run.sh')],
    'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True,
    'ThrottleInterval': 10, 'ProcessType': 'Standard', 'SoftResourceLimits': {'NumberOfFiles': 8192},
    'EnvironmentVariables': environment,
    'StandardOutPath': str(data/'service.log'), 'StandardErrorPath': str(data/'service-error.log')
}
plist.write_bytes(plistlib.dumps(configuration))
domain = 'gui/' + str(os.getuid())
loaded = subprocess.run(['launchctl', 'print', domain+'/'+label], capture_output=True).returncode == 0
if loaded and previous == configuration:
    subprocess.run(['launchctl', 'kickstart', '-k', domain+'/'+label], check=True)
else:
    if loaded:
        subprocess.run(['launchctl', 'bootout', domain+'/'+label], check=True)
    for attempt in range(6):
        result = subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit('LaunchAgent could not start; inspect ~/Library/Application Support/MediaLink/service-error.log')
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8766/api/v1/health', timeout=1) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(0.5)
else:
    raise SystemExit('Service did not become ready; inspect ~/Library/Application Support/MediaLink/service-error.log')
print('MediaLink is running at http://127.0.0.1:8766')
PY
