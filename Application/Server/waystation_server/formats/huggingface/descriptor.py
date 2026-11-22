"""Hugging Face repository options, upstream probe and "Set me up" snippets (proxy repositories only: the Hub's
upload API is not implemented, and a group would have to merge the commit histories of its members)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, on, powershell_env, snippet
from waystation_server.formats.huggingface import prefetch
from waystation_server.repos.model import RepoType

# Where the Hub redirects file downloads: its own host (relative redirects) and its CDNs (``cdn-lfs.hf.co``,
# ``cas-bridge.xethub.hf.co``, ``us.aws.cdn.hf.co``...).
DEFAULT_CDN_HOSTS = ('huggingface.co', '*.huggingface.co', '*.hf.co')
EXAMPLE_MODEL = 'julien-c/dummy-unknown'

Host = Annotated[str, StringConstraints(min_length=1, max_length=253, strip_whitespace=True, to_lower=True)]
_HOST = re.compile(r'^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')


class HuggingfaceOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='HuggingfaceOptions')

    cdn_hosts: list[Host] = Field(
        default_factory=lambda: list(DEFAULT_CDN_HOSTS),
        max_length=50,
        description='Hosts the proxy may follow file download redirects to (the Hub sends LFS and Xet files to its '
        'CDN with signed URLs): exact host names or `*.<domain>` for any host below that domain. The upstream '
        "URL's host is always allowed too; a redirect anywhere else fails the download (502)",
    )

    @field_validator('cdn_hosts')
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not _HOST.match(host):
                raise ValueError(f'invalid host name: {host} (a host name or *.<domain>)')
        return list(dict.fromkeys(value))


def host_allowed(host: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """Is ``host`` one of ``patterns`` (exact names, ``*.<domain>`` = any host below the domain)?"""
    host = host.lower().rstrip('.')
    for pattern in patterns:
        if pattern.startswith('*.'):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def _probe(options: BaseModel) -> str:
    return f'api/models/{EXAMPLE_MODEL}/revision/main'


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    endpoint = ctx.repo_url.rstrip('/')
    env = [('HF_ENDPOINT', endpoint)]
    auth = [('HF_ENDPOINT', endpoint), ('HF_TOKEN', TOKEN)]
    shell_env = snippet(
        'hf-endpoint',
        'Point the Hugging Face libraries at this repository',
        '\n'.join(f'export {k}={v}' for k, v in env),
        description='huggingface_hub (and transformers, diffusers, datasets, sentence-transformers... which use it) '
        'then downloads models through Waystation. No trailing slash.',
    )
    token = snippet(
        'hf-token',
        'Credentials (repositories that need them)',
        '\n'.join(f'export {k}={v}' for k, v in auth),
        description='HF_TOKEN is a Waystation API token here, sent as a bearer token to Waystation only. Gated and '
        "private models are fetched with the repository's own Hugging Face token, configured on the repository.",
    )
    cli = [
        shell_env,
        on('windows', shell_env, content=powershell_env(env), language='powershell'),
        token,
        on('windows', token, content=powershell_env(auth), language='powershell'),
        snippet(
            'hf-download',
            'Download a model',
            f'hf download {EXAMPLE_MODEL}\nhf download {EXAMPLE_MODEL} config.json --revision main',
            description='huggingface_hub 1.0 and later (pip install -U huggingface_hub).',
        ),
        snippet('hf-cli-legacy', 'Older huggingface_hub (before 1.0)', f'huggingface-cli download {EXAMPLE_MODEL}'),
    ]
    python = [
        snippet(
            'hf-python',
            'Download from Python',
            'import os\n'
            f"os.environ['HF_ENDPOINT'] = '{endpoint}'  # before huggingface_hub is imported\n\n"
            'from huggingface_hub import hf_hub_download, snapshot_download\n\n'
            f"config = hf_hub_download('{EXAMPLE_MODEL}', 'config.json')\n"
            f"folder = snapshot_download('{EXAMPLE_MODEL}', revision='main')\n",
            language='python',
            description='huggingface_hub reads HF_ENDPOINT when it is imported; setting it in the environment is '
            'simpler. Add HF_TOKEN (a Waystation API token) when the repository needs credentials.',
        ),
        snippet(
            'hf-transformers',
            'transformers',
            f'HF_ENDPOINT={endpoint} python -c "from transformers import AutoConfig; '
            f"print(AutoConfig.from_pretrained('{EXAMPLE_MODEL}'))\"",
        ),
    ]
    return [
        SetupClient(id='hf-cli', label='hf CLI', snippets=cli),
        SetupClient(id='hf-python', label='Python', snippets=python),
    ]


DESCRIPTOR = FormatDescriptor(
    name='huggingface',
    label='Hugging Face',
    types=frozenset({RepoType.PROXY}),
    options_model=HuggingfaceOptions,
    ui=UiHints(icon='huggingface', browse='huggingface'),
    probe_path=_probe,
    setup_snippets=_snippets,
    notes=(
        'Proxy repositories only: hosted and group Hugging Face repositories are not supported (uploads through the '
        'Hub API are not implemented).',
        'Models and datasets: file downloads, revision info and file listings (huggingface_hub, transformers, the hf '
        'CLI). Xet transfers are not offered: clients download every file over HTTP through Waystation.',
    ),
    warm_item=prefetch.check_item,
    refresh_request=prefetch.refresh_request,
)
