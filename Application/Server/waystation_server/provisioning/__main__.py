"""``python -m waystation_server.provisioning <command>`` (``Documents/Operations/PROVISIONING.md``).

- ``schema``: print the file's JSON Schema (no database).
- ``check FILE``: validate a file's syntax and schema offline (no database, secrets not resolved); exit 1 if invalid.
- ``plan [FILE] [--json]``: what applying would change, against the database of this environment (default file:
  ``WAYSTATION_PROVISION_FILE``); exit 1 if invalid.
- ``apply [FILE] [--json]``: apply it; exit 1 if invalid or a change failed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from waystation_common.logs import configure_logging
from waystation_common.settings import get_settings
from waystation_server.provisioning import loader, runner
from waystation_server.provisioning.model import json_schema

USAGE = 'usage: python -m waystation_server.provisioning schema | check FILE | plan [FILE] [--json] | apply [FILE] [--json]\n'


def check(path: str) -> int:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        sys.stderr.write(f'{path}: cannot read it: {exc.strerror}\n')
        return 1
    document, problems = loader.parse(raw)
    for problem in problems:
        sys.stderr.write(f'{path}: {problem.field}: {problem.message}\n')
    if document is None or problems:
        return 1
    sys.stdout.write(f'{path}: valid (version {document.version})\n')
    return 0


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point (exercised by make provision-*)
    args = [a for a in argv if a != '--json']
    as_json = '--json' in argv
    if args[:1] == ['schema']:
        sys.stdout.write(json.dumps(json_schema(), indent=2) + '\n')
        return 0
    if args[:1] == ['check'] and len(args) == 2:
        return check(args[1])
    if args[:1] not in (['plan'], ['apply']) or len(args) > 2:
        sys.stderr.write(USAGE)
        return 2
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    path = args[1] if len(args) == 2 else settings.provision_file
    if path is None:
        sys.stderr.write('no file given and WAYSTATION_PROVISION_FILE is not set\n')
        return 2
    result = asyncio.run(runner.run(settings, path, apply=args[0] == 'apply', by='cli'))
    sys.stdout.write(result.model_dump_json(indent=2) + '\n' if as_json else runner.render(result))
    return 0 if result.valid and not result.summary.failed else 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
