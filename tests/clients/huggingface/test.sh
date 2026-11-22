#!/bin/sh
# Hugging Face through Waystation (client-huggingface container: huggingface_hub 2.4 with hf_xet). With HF_ENDPOINT
# pointed at a proxy of the Hub: the file metadata huggingface_hub reads from HEAD (commit, LFS sha256 as the etag,
# size, no Xet hash: the download URL is Waystation's), hf_hub_download (a regular and an LFS file, checked against
# the etag), hf download (the CLI) and snapshot_download (info document, tree listing, files at the commit); the same
# downloads from Waystation's cache with the repository offline; a Waystation API token as HF_TOKEN, a bad one refused.
# The client never talks to the Hub: the fake upstream fails on anything unexpected.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
MODEL=julien-c/dummy-unknown
work=/tmp/hf-harness
rm -rf "$work"
mkdir -p "$work"
export HF_ENDPOINT="$W/repository/huggingface"

repo() {  # repo <offline: true|false>
  ws_api PUT /repositories/huggingface '{"name": "huggingface", "format": "huggingface", "type": "proxy",
    "online": true, "description": "Hugging Face Hub proxy (client harness)",
    "proxy": {"upstream_url": "https://huggingface.co", "offline": '"$1"'}}' >/dev/null || fail "could not set offline=$1"
}

py() {  # py <label> < script: run Python, show its output on failure
  python3 - > "$work/py.log" 2>&1 || { tail -n 40 "$work/py.log"; fail "python: $1"; }
}

ws_login
repo false

# ---------------------------------------------------------------------------- proxy
step 'HEAD metadata and hf_hub_download (regular and LFS files)'
HF_HOME="$work/a" py hf_hub_download <<'EOF'
import hashlib, json, os
from huggingface_hub import get_hf_file_metadata, hf_hub_download, hf_hub_url

model = 'julien-c/dummy-unknown'
meta = get_hf_file_metadata(hf_hub_url(model, 'pytorch_model.bin'))
assert meta.commit_hash and len(meta.commit_hash) == 40, meta
assert meta.etag and len(meta.etag) == 64 and meta.size == 65074, meta
assert meta.location.startswith(os.environ['HF_ENDPOINT'] + '/'), meta.location
assert meta.xet_file_data is None, meta
weights = hf_hub_download(model, 'pytorch_model.bin')
assert hashlib.sha256(open(weights, 'rb').read()).hexdigest() == meta.etag
config = hf_hub_download(model, 'config.json', revision='main')
assert json.load(open(config))
small = get_hf_file_metadata(hf_hub_url(model, 'config.json', revision=meta.commit_hash))
assert small.commit_hash == meta.commit_hash and len(small.etag) == 40 and small.size == os.path.getsize(config)
EOF

step 'hf download (CLI)'
HF_HOME="$work/b" hf download "$MODEL" config.json vocab.json --revision main > "$work/cli.log" 2>&1 \
  || { tail -n 20 "$work/cli.log"; fail 'hf download'; }
for f in config.json vocab.json; do
  [ -n "$(find "$work/b/hub" -path "*/snapshots/*/$f" -size +0)" ] || fail "hf download: no $f in the client cache"
done

snapshot() {  # snapshot <HF_HOME> <label>: snapshot_download without the TF and Flax weights, every file checked
  HF_HOME="$1" py "$2" <<'EOF'
import hashlib, os
from huggingface_hub import snapshot_download

folder = snapshot_download('julien-c/dummy-unknown', ignore_patterns=['*.h5', '*.msgpack'])
files = sorted(os.listdir(folder))
assert files == ['.gitattributes', 'README.md', 'config.json', 'merges.txt', 'pytorch_model.bin', 'vocab.json'], files
blob = os.path.realpath(os.path.join(folder, 'pytorch_model.bin'))
assert hashlib.sha256(open(blob, 'rb').read()).hexdigest() == os.path.basename(blob)  # LFS blobs: named by sha256
EOF
}

step 'snapshot_download'
snapshot "$work/c" snapshot_download

step 'the files are in the cache (HIT by commit)'
commit=$(curl -fsSI "$HF_ENDPOINT/$MODEL/resolve/main/config.json" | tr -d '\r' | sed -n 's/^[Xx]-[Rr]epo-[Cc]ommit: //p')
[ "${#commit}" = 40 ] || fail "no X-Repo-Commit: $commit"
cache=$(curl -fsS -o /dev/null -D - "$HF_ENDPOINT/$MODEL/resolve/$commit/pytorch_model.bin" | tr -d '\r' \
  | sed -n 's/^[Xx]-[Ww]s-[Cc]ache: //p')
[ "$cache" = HIT ] || fail "pytorch_model.bin answered X-Ws-Cache: $cache"
models=$(curl -fsS "$W/api/v1/repositories/huggingface/huggingface/models")
printf '%s' "$models" | grep -q "\"name\":\"$MODEL\"" || fail "$MODEL is not browsable: $models"

# ---------------------------------------------------------------------------- from the cache only
step 'snapshot_download and hf_hub_download again with the repository offline (a fresh client cache)'
repo true
snapshot "$work/d" 'snapshot_download (offline repository)'
HF_HOME="$work/e" py 'hf_hub_download (offline repository)' <<'EOF'
from huggingface_hub import hf_hub_download
hf_hub_download('julien-c/dummy-unknown', 'config.json')
EOF
repo false

step 'HF_HUB_OFFLINE: the client cache alone'
HF_HUB_OFFLINE=1 snapshot "$work/c" 'snapshot_download (HF_HUB_OFFLINE)'

# ---------------------------------------------------------------------------- credentials
step 'a Waystation API token as HF_TOKEN works, a bad one is refused'
ws_token '["read"]'
HF_TOKEN=$WS_TOKEN HF_HOME="$work/f" py 'hf_hub_download (HF_TOKEN)' <<'EOF'
from huggingface_hub import hf_hub_download
hf_hub_download('julien-c/dummy-unknown', 'merges.txt')
EOF
HF_TOKEN=not-a-token HF_HOME="$work/g" py 'hf_hub_download (bad HF_TOKEN)' <<'EOF'
from huggingface_hub import hf_hub_download
try:
    hf_hub_download('julien-c/dummy-unknown', 'merges.txt')
except Exception as exc:
    print('refused:', type(exc).__name__)
else:
    raise SystemExit('a bad token was accepted')
EOF

pass 'huggingface_hub (hf_hub_download, hf download, snapshot_download) through the Hugging Face proxy'
