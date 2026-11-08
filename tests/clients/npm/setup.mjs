// Create the npm proxy repository the test uses, through the REST API as the run's bootstrap admin.
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

const existing = await call('GET', `/repositories/${name}`, undefined, auth);
if (existing.status === 200) {
  console.log(`setup: repository ${name} exists`);
  process.exit(0);
}
const created = await call(
  'POST',
  '/repositories',
  { name, format: 'npm', type: 'proxy', online: true, proxy: { upstream_url: upstream } },
  auth,
);
if (created.status !== 201) {
  console.error(`setup: creating ${name} failed with ${created.status}: ${await created.text()}`);
  process.exit(1);
}
console.log(`setup: created npm proxy ${name} -> ${upstream}`);
