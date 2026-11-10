"""Terraform/OpenTofu provider network mirror options, upstream probe and "Set me up" snippets (proxy repositories;
hosted Terraform repositories and the module registry are T-P5-TFM)."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, host_of, snippet
from waystation_server.formats.terraform.paths import is_hostname
from waystation_server.repos.model import RepoType

DEFAULT_ORIGINS = ('registry.terraform.io', 'registry.opentofu.org')

Origin = Annotated[str, StringConstraints(min_length=1, max_length=253, strip_whitespace=True, to_lower=True)]


class TerraformOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='TerraformOptions')

    origins: list[Origin] = Field(
        default_factory=lambda: list(DEFAULT_ORIGINS),
        max_length=50,
        description='Provider registry host names this mirror serves providers of (the first part of a provider '
        "address, e.g. `registry.terraform.io/hashicorp/null`). The upstream URL's host is always allowed too",
    )
    verify_signatures: bool = Field(
        default=True,
        description="Verify each version's SHA256SUMS against its OpenPGP signature and the signing keys the origin "
        'registry publishes before anything of that version is served',
    )

    @field_validator('origins')
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not is_hostname(host):
                raise ValueError(f'invalid registry host name: {host}')
        return list(dict.fromkeys(value))


def _upstream_host(upstream_url: str | None) -> tuple[str, str] | None:
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
    upstream = _upstream_host(upstream_url)
    return frozenset({*options.origins, *([upstream[0]] if upstream else [])})


def origin_scheme(hostname: str, upstream_url: str | None) -> str:
    """How an origin is reached: the upstream URL's scheme for its own host (a registry without TLS, in a lab),
    else ``https`` (Terraform's service discovery is HTTPS only)."""
    upstream = _upstream_host(upstream_url)
    return upstream[1] if upstream and upstream[0] == hostname and upstream[1] in ('http', 'https') else 'https'


def _probe(options: BaseModel) -> str:
    return '.well-known/terraform.json'


def _cli_config(url: str, host: str) -> str:
    return (
        'provider_installation {\n'
        '  network_mirror {\n'
        f'    url = "{url}"\n'
        '  }\n'
        '}\n'
        '\n'
        f'credentials "{host}" {{\n'
        f'  token = "{TOKEN}"\n'
        '}\n'
    )


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    host = host_of(ctx.base_url)
    https = ctx.base_url.startswith('https://')
    note = (
        'Every provider is installed through Waystation. The credentials block is needed only when anonymous reads '
        'are not allowed: the API token is sent as a bearer token to this host only.'
    )
    if not https:
        note += ' Terraform and OpenTofu accept only https:// mirror URLs: put Waystation behind TLS.'
    env_host = host.replace('-', '__').replace('.', '_')
    terraform = [
        snippet(
            'terraform-cli-config',
            'Terraform CLI configuration',
            _cli_config(url, host),
            language='hcl',
            filename='~/.terraformrc',
            description=f'{note} On Windows the file is %APPDATA%\\terraform.rc; TF_CLI_CONFIG_FILE names another.',
        ),
        snippet(
            'terraform-init',
            'Install providers',
            'terraform init\n# record the hashes of every platform your team uses in .terraform.lock.hcl:\n'
            'terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=windows_amd64',
        ),
        snippet(
            'terraform-token-env',
            'Or pass the token in the environment',
            f'export TF_TOKEN_{env_host}={TOKEN}',
            description='Instead of the credentials block (host names without a port only).',
        ),
    ]
    tofu = [
        snippet(
            'tofu-cli-config',
            'OpenTofu CLI configuration',
            _cli_config(url, host),
            language='hcl',
            filename='~/.tofurc',
            description=f'{note} On Windows the file is %APPDATA%\\tofu.rc; TF_CLI_CONFIG_FILE names another.',
        ),
        snippet('tofu-init', 'Install providers', 'tofu init'),
    ]
    return [
        SetupClient(id='terraform', label='Terraform', snippets=terraform),
        SetupClient(id='tofu', label='OpenTofu', snippets=tofu),
    ]


DESCRIPTOR = FormatDescriptor(
    name='terraform',
    label='Terraform',
    types=frozenset({RepoType.PROXY}),
    options_model=TerraformOptions,
    ui=UiHints(icon='terraform', browse='terraform'),
    probe_path=_probe,
    setup_snippets=_snippets,
    notes=(
        'A provider network mirror: one repository serves providers of every allowed registry host '
        '(registry.terraform.io, registry.opentofu.org...).',
        'Hosted providers and modules are not available yet.',
    ),
)
