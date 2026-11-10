// Create the npm repositories the test uses (proxy, hosted, group), through the REST API as the run's bootstrap admin.
// Node (no curl in the node image): node /harness/npm/setup.mjs <name> <upstream-url>
const [name, upstream] = process.argv.slice(2);
const base = `${process.env.WAYSTATION_URL}/api/v1`;

async function call(method, path, body, headers = {}) {
  const response = await fetch(base + path, {
    method,
    headers: { 'content-type': 'application/json', accept: 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return response;
}

const login = await call('POST', '/auth/login', {
  username: 'admin',
  password: process.env.HARNESS_ADMIN_PASSWORD,
});
if (login.status !== 200) {
  console.error(`setup: sign-in failed with ${login.status}`);
  process.exit(1);
}
const session = await login.json();
const cookie = login.headers
  .getSetCookie()
  .map((c) => c.split(';', 1)[0])
  .join('; ');
const auth = { cookie, 'x-csrf-token': session.csrf_token };

// The proxy, a hosted repository and a group of both (writes to the group land in the hosted member).
const repositories = [
  { name, format: 'npm', type: 'proxy', online: true, proxy: { upstream_url: upstream } },
  { name: `${name}-hosted`, format: 'npm', type: 'hosted', online: true, hosted: { write_policy: 'disable_redeploy' } },
  {
    name: `${name}-all`,
    format: 'npm',
    type: 'group',
    online: true,
    group: { members: [`${name}-hosted`, name], writable_member: `${name}-hosted` },
  },
];
for (const repo of repositories) {
  const existing = await call('GET', `/repositories/${repo.name}`, undefined, auth);
  if (existing.status === 200) {
    console.log(`setup: repository ${repo.name} exists`);
    continue;
  }
  const created = await call('POST', '/repositories', repo, auth);
  if (created.status !== 201) {
    console.error(`setup: creating ${repo.name} failed with ${created.status}: ${await created.text()}`);
    process.exit(1);
  }
  console.log(`setup: created npm ${repo.type} ${repo.name}`);
}
