"""LDAP sign-in provider (SPECIFICATION §4.9; tested against an OpenLDAP directory with a FreeIPA-style tree and a
Samba Active Directory domain controller).

Password check for ``username``:

1. Connect to the first reachable server (``ldaps://``, or ``ldap://`` + StartTLS, or clear text when the provider
   says ``none``). Certificates are verified against the provider's CA bundle (else the system store), host names
   included (Python's ``ssl``, not ldap3's matcher). A server that cannot be reached or fails TLS is skipped and
   remembered as down for ``DOWN_FOR_S`` (failover); when all are down, all are tried again.
2. Service bind (``bind_dn`` + encrypted password) or anonymous, then search ``users.base_dn`` for
   ``(&<users.filter>(<username_attribute>=<escaped username>))``. Zero entries = unknown user, more than one = refused.
3. Bind as the entry's DN with the password on a separate connection. Empty passwords are refused before anything is
   sent (an empty simple bind is an anonymous bind on many servers). Locked or disabled accounts fail here (the
   directory refuses the bind) or are excluded by ``users.filter``.
4. Groups with the service connection: ``member_of`` reads the entry's ``member_of_attribute``; ``search`` looks for
   ``(&<groups.filter>(<member_attribute>=<user DN or username>))`` under ``groups.base_dn``. ``nested`` follows
   groups of groups (breadth first, at most ``MAX_GROUPS`` groups and ``MAX_DEPTH`` levels).
5. ``require_group`` (DN or name, case-insensitive) gates the sign-in.

ldap3 is synchronous: every directory conversation runs in a dedicated thread pool (never on the event loop) with
the provider's connect and response timeouts, plus an overall ceiling on the awaiting side.

Successful checks are cached for ``cache_ttl_s`` (format clients and ``docker login`` authenticate often) under an
HMAC of provider, configuration version, username and password with a per-process random key: the password is never
stored, and a changed configuration or password never matches an old entry. Interactive sign-ins skip the cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from ldap3 import ANONYMOUS, BASE, NONE, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPInvalidFilterError, LDAPSSLConfigurationError
from ldap3.operation.search import parse_filter
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

from waystation_server.security.external import ExternalGroup, ExternalIdentity
from waystation_server.security.providers import GroupMode, LdapConfigBase, LdapSecurity, MemberValue, group_key

log = logging.getLogger('waystation.auth.ldap')

DOWN_FOR_S = 30.0
MAX_GROUPS = 500
MAX_DEPTH = 10
MAX_USERNAME = 256
CACHE_MAX_ENTRIES = 10_000
RESULT_INVALID_CREDENTIALS = 49
RESULT_NO_SUCH_OBJECT = 32
RESULT_SIZE_LIMIT = 4

_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix='ldap')


# ---------------------------------------------------------------------------- configuration helpers
@dataclass(frozen=True)
class ServerAddress:
    host: str
    port: int
    ssl: bool

    @property
    def url(self) -> str:
        host = f'[{self.host}]' if ':' in self.host else self.host
        return f'{"ldaps" if self.ssl else "ldap"}://{host}:{self.port}'


def parse_server(value: str, security: LdapSecurity) -> ServerAddress:
    """``host``, ``host:port``, ``ldap://host[:port]`` or ``ldaps://host[:port]`` → address (``ValueError``)."""
    text = value.strip()
    ssl_wanted = security is LdapSecurity.LDAPS
    if '://' not in text:
        text = f'{"ldaps" if ssl_wanted else "ldap"}://{text}'
    parts = urlsplit(text)
    if parts.scheme not in ('ldap', 'ldaps'):
        raise ValueError('use ldap:// or ldaps://')
    if (parts.scheme == 'ldaps') != ssl_wanted:
        raise ValueError('ldaps:// needs security "ldaps" (and "ldaps" needs ldaps:// or a bare host)')
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise ValueError('only a host and an optional port')
    try:
        port = parts.port
    except ValueError:
        raise ValueError('invalid port') from None
    if not parts.hostname or port == 0:
        raise ValueError('a host is required')
    return ServerAddress(parts.hostname, port or (636 if ssl_wanted else 389), ssl_wanted)


def check_filter(value: str) -> None:
    """Raise ``ValueError`` unless ``value`` is one valid RFC 4515 filter in parentheses."""
    text = value.strip()
    if not (text.startswith('(') and text.endswith(')')):
        raise ValueError('must be a filter in parentheses, e.g. (objectClass=person)')
    try:
        parse_filter(text, None, auto_escape=False, auto_encode=False, validator=None, check_names=False)  # type: ignore[no-untyped-call]
    except (LDAPInvalidFilterError, LDAPException, ValueError) as exc:
        raise ValueError(f'invalid LDAP filter: {exc}') from None


def check_dn(value: str) -> None:
    try:
        parse_dn(value)
    except (LDAPException, ValueError) as exc:
        raise ValueError(f'invalid DN: {exc}') from None


def check_ca(pem: str) -> None:
    try:
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError(f'not a usable PEM CA bundle: {exc}') from None


def valid_username(username: str) -> bool:
    return 0 < len(username) <= MAX_USERNAME and username.isprintable() and username.strip() == username


# ---------------------------------------------------------------------------- results
Outcome = Literal['ok', 'not_found', 'bad_password', 'not_in_group', 'unavailable', 'error']


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    message: str


@dataclass(frozen=True)
class DirectoryUser:
    dn: str
    username: str
    display_name: str
    email: str | None


@dataclass(frozen=True)
class AuthResult:
    outcome: Outcome
    identity: ExternalIdentity | None = None
    server: str | None = None
    detail: str = ''


@dataclass
class ServerTest:
    server: str
    ok: bool = False
    latency_ms: float | None = None
    tls: str | None = None
    checks: list[Check] = field(default_factory=list)


@dataclass
class UserTest:
    found: bool = False
    server: str | None = None
    user: DirectoryUser | None = None
    groups: list[ExternalGroup] = field(default_factory=list)
    require_group_ok: bool | None = None
    bind_outcome: Literal['not_tested', 'ok', 'failed'] = 'not_tested'
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.found and self.bind_outcome != 'failed' and self.require_group_ok is not False


class ServerDown(Exception):  # noqa: N818 - an outcome: skip this server
    pass


class DirectoryError(Exception):
    """The directory answered but the provider configuration does not work (service bind refused, bad base DN)."""


# ---------------------------------------------------------------------------- failover state (per process)
class _DownServers:
    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def order(self, servers: list[ServerAddress]) -> list[ServerAddress]:
        """Servers to try: the ones not marked down, in order, then (all down) every server."""
        now = time.monotonic()
        with self._lock:
            up = [s for s in servers if self._until.get(s.url, 0.0) <= now]
        return up or servers

    def mark(self, server: ServerAddress) -> None:
        with self._lock:
            if len(self._until) > 1000:
                self._until.clear()
            self._until[server.url] = time.monotonic() + DOWN_FOR_S

    def clear(self, server: ServerAddress) -> None:
        with self._lock:
            self._until.pop(server.url, None)


DOWN = _DownServers()


# ---------------------------------------------------------------------------- TLS
class _Tls(Tls):
    """ldap3 TLS with a standard ``ssl`` context: certificate chain *and* host name verified by Python."""

    def __init__(self, ca_pem: str | None) -> None:
        super().__init__(validate=ssl.CERT_REQUIRED)
        try:
            context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
        except (ssl.SSLError, ValueError) as exc:
            raise LDAPSSLConfigurationError(f'CA bundle: {exc}') from None
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context = context

    def wrap_socket(self, connection: Any, do_handshake: bool = False) -> None:
        connection.socket = self.context.wrap_socket(
            connection.socket, server_hostname=connection.server.host, do_handshake_on_connect=do_handshake
        )


def _tls_summary(conn: Connection) -> str | None:
    sock = getattr(conn, 'socket', None)
    if not isinstance(sock, ssl.SSLSocket):
        return None
    cert: Any = sock.getpeercert() or {}
    subject = ', '.join(f'{k}={v}' for rdn in cert.get('subject', ()) for k, v in rdn)
    names = {'commonName': 'CN', 'organizationName': 'O'}
    for long, short in names.items():
        subject = subject.replace(f'{long}=', f'{short}=')
    return f'{sock.version()}, {subject}' if subject else sock.version()


# ---------------------------------------------------------------------------- the directory
def _values(attributes: Any, name: str) -> list[str]:
    raw = attributes.get(name) if attributes is not None else None
    if raw is None:
        return []
    items = raw if isinstance(raw, list | tuple) else [raw]
    return [v.decode('utf-8', 'replace') if isinstance(v, bytes) else str(v) for v in items if v not in (None, '')]


def _rdn_value(dn: str) -> str:
    try:
        parts = parse_dn(dn)
    except LDAPException, ValueError:
        return dn
    return str(parts[0][1]) if parts else dn


class LdapDirectory:
    """One provider configuration plus its bind password. Every method blocks: call it in the LDAP thread pool."""

    def __init__(self, source: str, config: LdapConfigBase, bind_password: str | None) -> None:
        self.source = source
        self.config = config
        self.bind_password = bind_password
        self.servers = [parse_server(s, config.connection.security) for s in config.connection.servers]

    # ------------------------------------------------------------------ connections
    def _connection(self, server: ServerAddress, user: str | None, password: str | None) -> tuple[Connection, float]:
        """Open (and StartTLS) a connection; ``ServerDown`` when the server cannot be used. Returns it and the
        connect latency in ms."""
        cfg = self.config.connection
        tls = _Tls(cfg.ca_pem) if cfg.security is not LdapSecurity.NONE else None
        srv = Server(
            server.host,
            port=server.port,
            use_ssl=server.ssl,
            tls=tls,
            get_info=NONE,
            connect_timeout=cfg.connect_timeout_s,
        )
        conn = Connection(
            srv,
            user=user,
            password=password,
            authentication=SIMPLE if user else ANONYMOUS,
            receive_timeout=cfg.timeout_s,
            read_only=True,
            raise_exceptions=False,
            auto_referrals=False,
            return_empty_attributes=False,
        )
        started = time.monotonic()
        try:
            conn.open()
            if cfg.security is LdapSecurity.STARTTLS:
                conn.start_tls()
        except LDAPSSLConfigurationError as exc:
            _close(conn)
            raise DirectoryError(str(exc)) from None
        except (LDAPException, OSError) as exc:
            _close(conn)
            raise ServerDown(_reason(exc)) from None
        return conn, (time.monotonic() - started) * 1000

    def _service(self, server: ServerAddress) -> Connection:
        """A connection bound as the service account (or anonymous)."""
        cfg = self.config.connection
        conn, _ = self._connection(server, cfg.bind_dn or None, self.bind_password if cfg.bind_dn else None)
        if cfg.bind_dn:
            self._bind_service(conn)
        return conn

    def _bind_service(self, conn: Connection) -> None:
        try:
            ok = conn.bind()
        except (LDAPException, OSError) as exc:
            _close(conn)
            raise ServerDown(_reason(exc)) from None
        if not ok:
            description = conn.result.get('description', 'refused') if conn.result else 'refused'
            _close(conn)
            raise DirectoryError(f'service bind refused ({description})')

    def _user_bind(self, server: ServerAddress, dn: str, password: str) -> bool:
        conn, _ = self._connection(server, dn, password)
        try:
            ok = bool(conn.bind())
        except (LDAPException, OSError) as exc:
            raise ServerDown(_reason(exc)) from None
        finally:
            _close(conn)
        return ok

    # ------------------------------------------------------------------ searches
    def _search(
        self,
        conn: Connection,
        base: str,
        search_filter: str,
        attributes: list[str],
        *,
        scope: Literal['BASE', 'LEVEL', 'SUBTREE'] = SUBTREE,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            conn.search(
                base,
                search_filter,
                search_scope=scope,
                attributes=attributes,
                size_limit=limit,
                time_limit=int(self.config.connection.timeout_s),
            )
        except (LDAPException, OSError) as exc:
            raise ServerDown(_reason(exc)) from None
        code = conn.result.get('result', 0) if conn.result else 0
        if code == RESULT_NO_SUCH_OBJECT:
            raise DirectoryError(f'search base not found: {base}')
        if code not in (0, RESULT_SIZE_LIMIT):
            raise DirectoryError(f'search failed ({conn.result.get("description", code)})')
        entries = [e for e in (conn.response or []) if e.get('type') == 'searchResEntry']
        if code == RESULT_SIZE_LIMIT and len(entries) < limit:
            entries.append({'type': 'searchResEntry', 'dn': '', 'attributes': {}})  # "more than allowed"
        return entries

    def find_user(self, conn: Connection, username: str) -> tuple[DirectoryUser, list[str]] | None:
        """The user's entry (and its raw ``member_of`` values); ``None`` when unknown; ``DirectoryError`` when
        ambiguous."""
        users = self.config.users
        groups = self.config.groups
        attributes = [users.username_attribute, users.display_name_attribute, users.email_attribute]
        if groups.mode is GroupMode.MEMBER_OF:
            attributes.append(groups.member_of_attribute)
        search_filter = f'(&{users.filter.strip()}({users.username_attribute}={escape_filter_chars(username)}))'
        entries = self._search(conn, users.base_dn, search_filter, attributes, limit=2)
        if not entries:
            return None
        if len(entries) > 1:
            raise DirectoryError('more than one entry matches this username')
        entry = entries[0]
        attrs = entry.get('attributes', {})
        names = _values(attrs, users.username_attribute)
        canonical = next((n for n in names if n.lower() == username.lower()), names[0] if names else username)
        display = next(iter(_values(attrs, users.display_name_attribute)), '') or canonical
        email = next(iter(_values(attrs, users.email_attribute)), None)
        user = DirectoryUser(dn=str(entry['dn']), username=canonical.lower(), display_name=display, email=email)
        member_of = _values(attrs, groups.member_of_attribute) if groups.mode is GroupMode.MEMBER_OF else []
        return user, member_of

    def find_groups(self, conn: Connection, user: DirectoryUser, member_of: list[str]) -> list[ExternalGroup]:
        cfg = self.config.groups
        if cfg.mode is GroupMode.NONE:
            return []
        found: dict[str, ExternalGroup] = {}
        if cfg.mode is GroupMode.MEMBER_OF:
            frontier = [dn for dn in member_of if dn]
            for dn in frontier:
                found.setdefault(group_key(dn), ExternalGroup(id=dn, name=_rdn_value(dn)))
            depth = 0
            while cfg.nested and frontier and depth < MAX_DEPTH and len(found) < MAX_GROUPS:
                depth += 1
                parents: list[str] = []
                for dn in frontier:
                    entries = self._search(conn, dn, '(objectClass=*)', [cfg.member_of_attribute], scope=BASE, limit=1)
                    for parent in _values(entries[0].get('attributes', {}) if entries else {}, cfg.member_of_attribute):
                        if group_key(parent) not in found and len(found) < MAX_GROUPS:
                            found[group_key(parent)] = ExternalGroup(id=parent, name=_rdn_value(parent))
                            parents.append(parent)
                frontier = parents
            return list(found.values())
        if not cfg.base_dn:
            raise DirectoryError('group search needs a group base DN')
        value = user.dn if cfg.member_value is MemberValue.DN else user.username
        frontier_values = [value]
        depth = 0
        while frontier_values and len(found) < MAX_GROUPS:
            next_values: list[str] = []
            for member in frontier_values:
                search_filter = f'(&{cfg.filter.strip()}({cfg.member_attribute}={escape_filter_chars(member)}))'
                limit = MAX_GROUPS - len(found)
                for entry in self._search(conn, cfg.base_dn, search_filter, [cfg.name_attribute], limit=limit):
                    dn = str(entry.get('dn', ''))
                    if not dn or group_key(dn) in found:
                        continue
                    name = next(iter(_values(entry.get('attributes', {}), cfg.name_attribute)), '') or _rdn_value(dn)
                    found[group_key(dn)] = ExternalGroup(id=dn, name=name)
                    next_values.append(dn)
            depth += 1
            if not cfg.nested or depth >= MAX_DEPTH:
                break
            frontier_values = next_values
        return list(found.values())

    def require_group_ok(self, groups: Iterable[ExternalGroup]) -> bool | None:
        required = self.config.groups.require_group
        if not required:
            return None
        key = group_key(required)
        return any(key in (group_key(g.id), group_key(g.name)) for g in groups)

    # ------------------------------------------------------------------ operations
    def authenticate(self, username: str, password: str) -> AuthResult:
        if not password or not valid_username(username):
            return AuthResult('not_found' if password else 'bad_password', detail='empty password or bad username')
        unavailable: list[str] = []
        for server in DOWN.order(self.servers):
            try:
                result = self._authenticate_on(server, username, password)
            except ServerDown as exc:
                DOWN.mark(server)
                unavailable.append(f'{server.url}: {exc}')
                log.warning('LDAP server unavailable', extra={'server': server.url, 'error': str(exc)})
                continue
            except DirectoryError as exc:
                log.error('LDAP provider misconfigured', extra={'server': server.url, 'error': str(exc)})
                return AuthResult('error', server=server.url, detail=str(exc))
            DOWN.clear(server)
            return result
        return AuthResult('unavailable', detail='; '.join(unavailable))

    def _authenticate_on(self, server: ServerAddress, username: str, password: str) -> AuthResult:
        conn = self._service(server)
        try:
            found = self.find_user(conn, username)
            if found is None:
                return AuthResult('not_found', server=server.url)
            user, member_of = found
            if not self._user_bind(server, user.dn, password):
                return AuthResult('bad_password', server=server.url)
            groups = self.find_groups(conn, user, member_of)
        finally:
            _close(conn)
        if self.require_group_ok(groups) is False:
            return AuthResult('not_in_group', server=server.url)
        identity = ExternalIdentity(
            provider=self.source,
            subject=user.dn,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            groups=tuple(groups),
        )
        return AuthResult('ok', identity=identity, server=server.url)

    def test_connection(self) -> list[ServerTest]:
        return [self._test_server(server) for server in self.servers]

    def _test_server(self, server: ServerAddress) -> ServerTest:
        result = ServerTest(server=server.url)
        cfg = self.config
        try:
            conn, latency = self._connection(server, cfg.connection.bind_dn or None, self.bind_password)
        except (ServerDown, DirectoryError) as exc:
            result.checks.append(Check('connect', False, str(exc)))
            return result
        result.latency_ms = round(latency, 1)
        result.checks.append(Check('connect', True, f'connected in {latency:.0f} ms'))
        if cfg.connection.security is not LdapSecurity.NONE:
            result.tls = _tls_summary(conn)
            result.checks.append(Check('tls', True, result.tls or 'TLS established'))
        try:
            if cfg.connection.bind_dn:
                self._bind_service(conn)
                result.checks.append(Check('bind', True, f'bound as {cfg.connection.bind_dn}'))
            else:
                result.checks.append(Check('bind', True, 'anonymous (no bind DN)'))
            bases = [('user_base', cfg.users.base_dn)]
            if cfg.groups.mode is GroupMode.SEARCH and cfg.groups.base_dn:
                bases.append(('group_base', cfg.groups.base_dn))
            for name, base in bases:
                try:
                    self._search(conn, base, '(objectClass=*)', ['1.1'], scope=BASE, limit=1)
                    result.checks.append(Check(name, True, f'{base} is readable'))
                except DirectoryError as exc:
                    result.checks.append(Check(name, False, str(exc)))
        except (ServerDown, DirectoryError) as exc:
            result.checks.append(Check('bind', False, str(exc)))
        finally:
            _close(conn)
        result.ok = all(c.ok for c in result.checks)
        return result

    def test_user(self, username: str, password: str | None) -> UserTest:
        result = UserTest()
        if not valid_username(username):
            result.checks.append(Check('search', False, 'not a valid username'))
            return result
        for server in self.servers:
            try:
                conn = self._service(server)
            except ServerDown as exc:
                result.checks.append(Check('connect', False, f'{server.url}: {exc}'))
                continue
            except DirectoryError as exc:
                result.checks.append(Check('bind', False, f'{server.url}: {exc}'))
                return result
            result.server = server.url
            try:
                self._test_user_on(server, conn, username, password, result)
            except (ServerDown, DirectoryError) as exc:
                result.checks.append(Check('search', False, str(exc)))
            finally:
                _close(conn)
            return result
        return result

    def _test_user_on(
        self, server: ServerAddress, conn: Connection, username: str, password: str | None, result: UserTest
    ) -> None:
        found = self.find_user(conn, username)
        if found is None:
            result.checks.append(
                Check('search', False, f'no entry matches {username!r} under {self.config.users.base_dn}')
            )
            return
        user, member_of = found
        result.found, result.user = True, user
        result.checks.append(Check('search', True, user.dn))
        if password:
            ok = self._user_bind(server, user.dn, password)
            result.bind_outcome = 'ok' if ok else 'failed'
            result.checks.append(Check('password', ok, 'password accepted' if ok else 'the directory refused the bind'))
        result.groups = self.find_groups(conn, user, member_of)
        result.checks.append(Check('groups', True, f'{len(result.groups)} group(s)'))
        result.require_group_ok = self.require_group_ok(result.groups)
        if result.require_group_ok is not None:
            message = 'member of the required group' if result.require_group_ok else 'not in the required group'
            result.checks.append(Check('require_group', result.require_group_ok, message))


def _close(conn: Connection) -> None:
    with contextlib.suppress(LDAPException, OSError):
        conn.unbind()


def _reason(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:300]


# ---------------------------------------------------------------------------- async front end
class BindCache:
    """Successful password checks (HMAC key → identity, with expiry). Never holds a password."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._secret = secrets.token_bytes(32)
        self._entries: OrderedDict[str, tuple[float, ExternalIdentity]] = OrderedDict()
        self._max = max_entries

    def key(self, provider_id: str, version: int, username: str, password: str) -> str:
        message = b'\0'.join(
            [provider_id.encode(), str(version).encode(), username.lower().encode(), password.encode()]
        )
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def get(self, key: str) -> ExternalIdentity | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            self._entries.pop(key, None)
            return None
        return hit[1]

    def put(self, key: str, identity: ExternalIdentity, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        self._entries[key] = (time.monotonic() + ttl_s, identity)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


def _ceiling(config: LdapConfigBase) -> float:
    """Upper bound for one conversation (several round trips per server, every server)."""
    per_server = config.connection.connect_timeout_s * 2 + config.connection.timeout_s * 6
    return min(300.0, per_server * len(config.connection.servers) + 5)


async def run_blocking[T](config: LdapConfigBase, fn: Callable[[], T]) -> T:
    """Run a directory conversation in the LDAP thread pool, bounded by the configuration's ceiling."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, fn), timeout=_ceiling(config))


class LdapAuthenticator:
    """Per-process LDAP front end used by sign-in (``security.login``) and the admin test endpoints."""

    def __init__(self) -> None:
        self.cache = BindCache()

    async def authenticate(
        self,
        *,
        provider_id: str,
        version: int,
        directory: LdapDirectory,
        username: str,
        password: str,
        use_cache: bool,
    ) -> AuthResult:
        key = self.cache.key(provider_id, version, username, password) if use_cache else ''
        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                return AuthResult('ok', identity=cached, detail='cached')
        try:
            result = await run_blocking(directory.config, lambda: directory.authenticate(username, password))
        except TimeoutError:
            log.warning('LDAP sign-in timed out', extra={'provider': provider_id})
            return AuthResult('unavailable', detail='timed out')
        if result.outcome == 'ok' and result.identity is not None:
            self.cache.put(
                self.cache.key(provider_id, version, username, password), result.identity, directory.config.cache_ttl_s
            )
        return result
