"""Terraform/OpenTofu repository options, upstream probe and "Set me up" snippets.

- **Proxy** repositories are a provider network mirror (T-P5-TFP) and a module registry proxy (T-P5-TFM).
- The **hosted** repository (one per instance, T-P5-TFM) is a private module and provider registry, announced by
  ``/.well-known/terraform.json`` (``waystation_server.wellknown``).
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, host_of, on, powershell_env, snippet
from waystation_server.formats.terraform.paths import is_hostname
from waystation_server.repos.model import RepoType

DEFAULT_ORIGINS = ('registry.terraform.io', 'registry.opentofu.org')
DEFAULT_ARCHIVE_HOSTS = ('github.com',)

Origin = Annotated[str, StringConstraints(min_length=1, max_length=253, strip_whitespace=True, to_lower=True)]


class TerraformOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='TerraformOptions')

    origins: list[Origin] = Field(
        default_factory=lambda: list(DEFAULT_ORIGINS),
        max_length=50,
        description='Provider registry host names this mirror serves providers of (the first part of a provider '
        "address, e.g. `registry.terraform.io/hashicorp/null`). The upstream URL's host is always allowed too. "
        'Proxy repositories only',
    )
    verify_signatures: bool = Field(
        default=True,
        description="Verify each version's SHA256SUMS against its OpenPGP signature and the signing keys the origin "
        'registry publishes before anything of that version is served. Proxy repositories only',
    )
    module_archive_hosts: list[Origin] = Field(
        default_factory=lambda: list(DEFAULT_ARCHIVE_HOSTS),
        max_length=50,
        description='Hosts the module registry proxy fetches and caches module archives from: GitHub repositories '
        'at a tag (`github.com`, fetched as tag archives) and HTTP(S) or S3 archive URLs on these hosts. Other '
        "sources are passed to the client unchanged. The upstream URL's host is always allowed too. Proxy "
        'repositories only',
    )

    @field_validator('origins', 'module_archive_hosts')
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not is_hostname(host):
                raise ValueError(f'invalid host name: {host}')
        return list(dict.fromkeys(value))


def upstream_host(upstream_url: str | None) -> tuple[str, str] | None:
    """``(host[:port], scheme)`` of the upstream URL (port omitted when it is 443, as Terraform writes host names)."""
    if not upstream_url:
        return None
    parts = urlsplit(upstream_url)
    if not parts.hostname:
        return None
    port = parts.port
    return parts.hostname.lower() + (f':{port}' if port and port != 443 else ''), parts.scheme.lower()


def allowed_origins(options: TerraformOptions, upstream_url: str | None) -> frozenset[str]:
    """The registry host names a mirror serves: the ``origins`` option and the upstream URL's host."""
    upstream = upstream_host(upstream_url)
    return frozenset({*options.origins, *([upstream[0]] if upstream else [])})


def origin_scheme(hostname: str, upstream_url: str | None) -> str:
    """How an origin is reached: the upstream URL's scheme for its own host (a registry without TLS, in a lab),
    else ``https`` (Terraform's service discovery is HTTPS only)."""
    upstream = upstream_host(upstream_url)
    return upstream[1] if upstream and upstream[0] == hostname and upstream[1] in ('http', 'https') else 'https'


def _probe(options: BaseModel) -> str:
    return '.well-known/terraform.json'


def _credentials(host: str) -> str:
    return f'credentials "{host}" {{\n  token = "{TOKEN}"\n}}\n'


def _mirror_config(url: str, host: str, registry: str | None) -> str:
    text = f'provider_installation {{\n  network_mirror {{\n    url = "{url}"\n  }}\n}}\n'
    if registry:
        text += (
            '\n'
            f'# modules from {registry} through Waystation (module sources stay "<namespace>/<name>/<system>")\n'
            f'host "{registry}" {{\n'
            '  services = {\n'
            f'    "modules.v1" = "{url}v1/modules/"\n'
            '  }\n'
            '}\n'
            f'credentials "{registry}" {{\n  token = "{TOKEN}"\n}}\n'
        )
    return text + '\n' + _credentials(host)


def _env_host(host: str) -> str:
    return host.replace('-', '__').replace('.', '_')


def _proxy_snippets(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    host = host_of(ctx.base_url)
    upstream = upstream_host(ctx.repo.proxy.upstream_url if ctx.repo.proxy else None)
    registry = upstream[0] if upstream else None
    https = ctx.base_url.startswith('https://')
    note = (
        'Every provider is installed through Waystation (network mirror). The host block sends module lookups for '
        f'{registry or "the upstream registry"} to this repository: archives from GitHub tags and allowed hosts are '
        'cached here, other module sources are fetched by the client itself. The credentials blocks are needed only '
        'when anonymous reads are not allowed: the API token is sent as a bearer token to Waystation only.'
    )
    if not https:
        note += ' Terraform and OpenTofu accept only https:// URLs: put Waystation behind TLS.'
    env_host = _env_host(host)
    tf_config = snippet(
        'terraform-cli-config',
        'Terraform CLI configuration',
        _mirror_config(url, host, registry),
        language='hcl',
        filename='~/.terraformrc',
        description=f'{note} TF_CLI_CONFIG_FILE names another file.',
    )
    tf_token = snippet(
        'terraform-token-env',
        'Or pass the token in the environment',
        f'export TF_TOKEN_{env_host}={TOKEN}',
        description='Instead of the credentials block for this host (host names without a port only).',
    )
    terraform = [
        tf_config,
        on('windows', tf_config, filename='%APPDATA%\\terraform.rc'),
        snippet(
            'terraform-init',
            'Install providers and modules',
            'terraform init\n# record the hashes of every platform your team uses in .terraform.lock.hcl:\n'
            'terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=windows_amd64',
        ),
        tf_token,
        on('windows', tf_token, content=powershell_env([(f'TF_TOKEN_{env_host}', TOKEN)]), language='powershell'),
    ]
    tofu_config = snippet(
        'tofu-cli-config',
        'OpenTofu CLI configuration',
        _mirror_config(url, host, registry),
        language='hcl',
        filename='~/.tofurc',
        description=f'{note} TF_CLI_CONFIG_FILE names another file.',
    )
    tofu = [
        tofu_config,
        on('windows', tofu_config, filename='%APPDATA%\\tofu.rc'),
        snippet('tofu-init', 'Install providers and modules', 'tofu init'),
    ]
    return [
        SetupClient(id='terraform', label='Terraform', snippets=terraform),
        SetupClient(id='tofu', label='OpenTofu', snippets=tofu),
    ]


def _hosted_usage(host: str) -> str:
    return (
        'terraform {\n'
        '  required_providers {\n'
        '    example = {\n'
        f'      source  = "{host}/my-namespace/example"\n'
        '      version = "1.0.0"\n'
        '    }\n'
        '  }\n'
        '}\n'
        '\n'
        'module "network" {\n'
        f'  source  = "{host}/my-namespace/network/aws"\n'
        '  version = "1.0.0"\n'
        '}\n'
    )


def _publish(ctx: SetupContext) -> list[SetupSnippet]:
    url = ctx.repo_url
    return [
        snippet(
            'terraform-publish-module',
            'Publish a module',
            'tar -czf network.tar.gz -C ./network .\n'
            f'curl --fail -H "Authorization: Bearer {TOKEN}" --upload-file network.tar.gz \\\n'
            f'  {url}v1/modules/my-namespace/network/aws/1.0.0',
            description="The archive holds the module directory's contents (its .tf files at the root). Publishing "
            'needs the write permission; versions cannot be replaced (delete one with curl -X DELETE on the same URL).',
        ),
        snippet(
            'terraform-publish-provider',
            'Publish a provider release',
            'cd dist  # terraform-provider-example_1.0.0_<os>_<arch>.zip for each platform\n'
            'sha256sum terraform-provider-example_1.0.0_*.zip > terraform-provider-example_1.0.0_SHA256SUMS\n'
            'gpg --detach-sign terraform-provider-example_1.0.0_SHA256SUMS\n'
            'gpg --armor --export my-signing-key > signing-key.asc\n'
            f'curl --fail -H "Authorization: Bearer {TOKEN}" \\\n'
            '  $(for f in terraform-provider-example_1.0.0_*.zip; do printf -- "-F archive=@%s " "$f"; done) \\\n'
            '  -F shasums=@terraform-provider-example_1.0.0_SHA256SUMS \\\n'
            '  -F signature=@terraform-provider-example_1.0.0_SHA256SUMS.sig \\\n'
            '  -F signing_key=@signing-key.asc -F protocols=5.0 \\\n'
            f'  {url}v1/providers/my-namespace/example/1.0.0',
            description='SHA256SUMS must list every archive and verify with the signing key, as Terraform and '
            'OpenTofu check them when they install the provider. Releases cannot be replaced.',
        ),
    ]


def _hosted_snippets(ctx: SetupContext) -> list[SetupClient]:
    host = host_of(ctx.base_url)
    note = (
        'Module and provider addresses start with this host name, which Terraform finds through '
        f'https://{host}/.well-known/terraform.json (it must contain a dot, and Waystation must be served over '
        'https). The credentials block is needed only when anonymous reads are not allowed; file downloads get '
        'short-lived signed URLs, so no other configuration is needed.'
    )
    env_host = _env_host(host)
    tf_config = snippet(
        'terraform-cli-config',
        'Terraform CLI configuration',
        _credentials(host),
        language='hcl',
        filename='~/.terraformrc',
        description=f'{note} TF_CLI_CONFIG_FILE names another file; terraform login {host} works too.',
    )
    tf_token = snippet(
        'terraform-token-env',
        'Or pass the token in the environment',
        f'export TF_TOKEN_{env_host}={TOKEN}',
        description='Instead of the credentials block (host names without a port only).',
    )
    usage = snippet(
        'terraform-sources',
        'Use private providers and modules',
        _hosted_usage(host),
        language='hcl',
        filename='main.tf',
        description='Module sources are <host>/<namespace>/<name>/<system>, provider sources '
        '<host>/<namespace>/<type>.',
    )
    terraform = [
        tf_config,
        on('windows', tf_config, filename='%APPDATA%\\terraform.rc'),
        tf_token,
        on('windows', tf_token, content=powershell_env([(f'TF_TOKEN_{env_host}', TOKEN)]), language='powershell'),
        usage,
        snippet('terraform-init', 'Install', 'terraform init'),
        *_publish(ctx),
    ]
    tofu_config = snippet(
        'tofu-cli-config',
        'OpenTofu CLI configuration',
        _credentials(host),
        language='hcl',
        filename='~/.tofurc',
        description=f'{note} TF_CLI_CONFIG_FILE names another file; tofu login {host} works too.',
    )
    tofu = [
        tofu_config,
        on('windows', tofu_config, filename='%APPDATA%\\tofu.rc'),
        usage,
        snippet('tofu-init', 'Install', 'tofu init'),
    ]
    return [
        SetupClient(id='terraform', label='Terraform', snippets=terraform),
        SetupClient(id='tofu', label='OpenTofu', snippets=tofu),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    return _hosted_snippets(ctx) if ctx.repo.type is RepoType.HOSTED else _proxy_snippets(ctx)


DESCRIPTOR = FormatDescriptor(
    name='terraform',
    label='Terraform',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED}),
    options_model=TerraformOptions,
    ui=UiHints(icon='terraform', browse='terraform'),
    probe_path=_probe,
    setup_snippets=_snippets,
    notes=(
        'A proxy is a provider network mirror for every allowed registry host (registry.terraform.io, '
        'registry.opentofu.org...) and a module registry proxy of its upstream registry.',
        'The hosted repository is a private module and provider registry; an instance has one, announced at '
        '/.well-known/terraform.json.',
    ),
)
