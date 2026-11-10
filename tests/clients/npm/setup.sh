#!/bin/sh
# Create the npm repositories: proxy, hosted, group (client-node container; the bootstrap admin comes from run.sh).
set -eu
. /harness/lib.sh
step 'wait for the stack and create the npm repositories'
i=0
until node -e "fetch(process.env.WAYSTATION_URL + '/readyz').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"; do
  i=$((i + 1)); [ "$i" -lt 120 ] || fail 'stack not ready'; sleep 1
done
node /harness/npm/setup.mjs npm https://registry.npmjs.org/
