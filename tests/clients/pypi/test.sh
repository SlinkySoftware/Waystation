#!/bin/sh
# PyPI through Waystation (client-python container: pip, uv, twine). pip and uv install through the pypi.org proxy;
# twine and uv publish upload tiny generated distributions to the hosted repository (directly and through the
# group's writable member); pip and uv install through the group; yanking, a narrowed token, the browse API.
# The clients never talk to the upstream: only Waystation's simple pages name the files they download.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
host=${W#http://}
export PIP_TRUSTED_HOST="${host%%:*}"  # the harness stack is plain HTTP
export PIP_NO_CACHE_DIR=1 UV_NO_CACHE=1
work=/tmp/pypi-harness
rm -rf "$work"
mkdir -p "$work"

check_import() {  # check_import <site dir> <python statement>
  PYTHONPATH="$1" python3 -c "$2" || fail "import check failed in $1: $2"
}

step 'pip install through the proxy (simple JSON pages, PEP 658 metadata, wheels verified by sha256)'
pip install --quiet --target "$work/pip" --index-url "$W/repository/pypi-proxy/simple/" six==1.17.0 iniconfig==2.1.0 \
  || fail 'pip install through the proxy'
check_import "$work/pip" 'import six, iniconfig; assert six.__version__ == "1.17.0"'

step 'uv pip install through the proxy (served from the Waystation cache)'
uv pip install --quiet --target "$work/uv" --index-url "$W/repository/pypi-proxy/simple/" six==1.17.0 iniconfig==2.1.0 \
  || fail 'uv pip install through the proxy'
check_import "$work/uv" 'import six, iniconfig'

step 'a non-normalised project URL redirects to the normalised one'
python3 - "$W/repository/pypi-proxy/simple/Six" <<'EOF' || fail 'PEP 503 redirect'
import sys, urllib.request
response = urllib.request.urlopen(sys.argv[1])
assert response.url.endswith('/repository/pypi-proxy/simple/six/'), response.url
assert b'six-1.17.0-py2.py3-none-any.whl' in response.read()
EOF

step 'twine upload to the hosted repository (an API token as the password)'
ws_login
ws_token '["read", "write"]' '["pypi-*"]'
token=$WS_TOKEN
python3 /harness/pypi/make_dist.py "$work/dist1" 0.1.0
twine upload --non-interactive --disable-progress-bar --repository-url "$W/repository/pypi-hosted/" \
  -u __token__ -p "$token" "$work"/dist1/* || fail 'twine upload'

step 'uv publish through the group (lands in its writable member)'
python3 /harness/pypi/make_dist.py "$work/dist2" 0.2.0
uv publish --publish-url "$W/repository/pypi-group/" --username __token__ --password "$token" "$work"/dist2/* \
  || fail 'uv publish'

group="http://__token__:$token@$host/repository/pypi-group/simple/"
step 'pip install through the group: an uploaded project and a proxied one'
pip install --quiet --target "$work/group" --index-url "$group" ws-harness-demo six==1.17.0 \
  || fail 'pip install through the group'
check_import "$work/group" 'import six, ws_harness_demo; assert ws_harness_demo.VERSION == "0.2.0"'

step 'uv pip install through the group (pinned older version)'
uv pip install --quiet --target "$work/uv-group" --index-url "$group" ws-harness-demo==0.1.0 \
  || fail 'uv pip install through the group'
check_import "$work/uv-group" 'import ws_harness_demo; assert ws_harness_demo.VERSION == "0.1.0"'

step 'pip install from the hosted repository with plain credentials: sent after the 401 challenge (ADR 0005)'
# No credentials in the index URL: pip asks its keyring only when the server challenges (handle_401).
: > "$work/keyring-asked"
(cd "$work" && PYTHONPATH=/harness/pypi PYTHON_KEYRING_BACKEND=challenge_keyring.ChallengeKeyring \
  WS_KEYRING_TOKEN="$token" WS_KEYRING_MARK="$work/keyring-asked" \
  pip install --quiet --no-input --keyring-provider import --target "$work/challenge" \
  --index-url "$W/repository/pypi-hosted/simple/" ws-harness-demo==0.1.0) || fail 'pip install after the challenge'
[ -s "$work/keyring-asked" ] || fail 'pip was never challenged for credentials'
check_import "$work/challenge" 'import ws_harness_demo; assert ws_harness_demo.VERSION == "0.1.0"'

project() {  # project <dir> <tool section>: a dependency-only pyproject.toml using the group
  mkdir -p "$1"
  cat > "$1/pyproject.toml" <<EOF
[project]
name = "harness-$(basename "$1")"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = ["six==1.17.0", "iniconfig==2.1.0", "ws-harness-demo==0.2.0"]

$2
EOF
}

step 'Poetry: lock and install through the group (primary source, http-basic credentials)'
project "$work/poetry" "[tool.poetry]
package-mode = false

[[tool.poetry.source]]
name = \"waystation\"
url = \"$W/repository/pypi-group/simple/\"
priority = \"primary\""
(cd "$work/poetry" && POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=true POETRY_CACHE_DIR="$work/poetry-cache" \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  POETRY_HTTP_BASIC_WAYSTATION_USERNAME=__token__ POETRY_HTTP_BASIC_WAYSTATION_PASSWORD="$token" \
  poetry install --no-root) || fail 'poetry install'
"$work/poetry/.venv/bin/python" -c 'import six, iniconfig, ws_harness_demo; assert ws_harness_demo.VERSION == "0.2.0"' \
  || fail 'poetry environment'
grep -q 'name = "ws-harness-demo"' "$work/poetry/poetry.lock" || fail 'poetry.lock does not pin ws-harness-demo'

step 'PDM: lock and install through the group'
project "$work/pdm" "[tool.pdm]
distribution = false

[[tool.pdm.source]]
name = \"pypi\"
url = \"http://__token__:\${WS_PYPI_TOKEN}@$host/repository/pypi-group/simple/\"
verify_ssl = false  # plain HTTP harness stack: PDM trusts the host only then"
(cd "$work/pdm" && WS_PYPI_TOKEN="$token" PDM_CHECK_UPDATE=false PDM_CACHE_DIR="$work/pdm-cache" \
  PDM_IGNORE_SAVED_PYTHON=1 PDM_PYTHON=/usr/local/bin/python3 pdm install --quiet) \
  || { tail -n 40 /root/.local/state/pdm/log/*.log 2>/dev/null | grep -v -i token; fail 'pdm install'; }
"$work/pdm/.venv/bin/python" -c 'import six, iniconfig, ws_harness_demo; assert ws_harness_demo.VERSION == "0.2.0"' \
  || fail 'pdm environment'

step 'yank 0.2.0: an unpinned install now picks 0.1.0'
ws_api POST /repositories/pypi-hosted/pypi/yank \
  '{"project": "ws-harness-demo", "version": "0.2.0", "yanked": true, "reason": "harness"}' >/dev/null \
  || fail 'yank'
pip install --quiet --target "$work/yanked" --index-url "http://__token__:$token@$host/repository/pypi-hosted/simple/" \
  ws-harness-demo || fail 'pip install after the yank'
check_import "$work/yanked" 'import ws_harness_demo; assert ws_harness_demo.VERSION == "0.1.0"'

step 'a token limited to other repositories cannot read the hosted repository'
ws_token '["read"]' '["docker-*"]'
if pip download --quiet --no-deps -d "$work/narrow" \
  --index-url "http://__token__:$WS_TOKEN@$host/repository/pypi-hosted/simple/" ws-harness-demo 2>"$work/narrow.log"; then
  fail 'a token limited to docker-* could read the hosted PyPI repository'
fi

step 'browse API: cached and uploaded projects, versions and files'
python3 - "$W/api/v1/repositories" "$WS_COOKIES" "$WS_CSRF" <<'EOF' || fail 'browse API'
import json, sys, time, urllib.parse, urllib.request
api, cookies = sys.argv[1], open(sys.argv[2]).read().strip()

def get(path, auth=False):
    request = urllib.request.Request(api + path, headers={'Cookie': cookies} if auth else {})
    return json.load(urllib.request.urlopen(request))

for attempt in range(40):  # proxy project summaries are recorded in the background
    names = [p['name'] for p in get('/pypi-proxy/pypi/projects')['items']]
    if {'six', 'iniconfig'} <= set(names):
        break
    time.sleep(0.5)
else:
    raise SystemExit(f'proxy projects not listed: {names}')
six = get('/pypi-proxy/pypi/versions?project=Six')
release = next(v for v in six['items'] if v['version'] == '1.17.0')
wheel = next(f for f in release['files'] if f['kind'] == 'wheel')
assert wheel['cached'] and wheel['core_metadata'] and wheel['sha256'], wheel
hosted = get('/pypi-hosted/pypi/versions?project=ws-harness-demo', auth=True)
versions = {v['version']: v for v in hosted['items']}
assert set(versions) == {'0.1.0', '0.2.0'} and versions['0.2.0']['yanked'], versions
assert hosted['project']['latest'] == '0.2.0' and hosted['project']['files'] == 4, hosted['project']
assert all(f['uploaded_by'] == 'admin' for v in versions.values() for f in v['files'])
EOF

pass
