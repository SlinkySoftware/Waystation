"""Error responses (ARCHITECTURE §12).

- REST API (``/api/...``) and everything that is not a format endpoint: RFC 9457 ``application/problem+json``.
  Validation failures are 422 with ``errors[]`` (``{field, message, code}``, ``field`` a dotted path into the body).
- Docker/OCI endpoints (``/v2/``, ``/auth/token``): the OCI error document ``{"errors": [{code, message, detail}]}``.
- Other format endpoints (``/repository/<repo>/...``): a short plain-text body; format plugins that need their own
  shape (npm JSON errors...) return it themselves.

``detail`` texts must be safe for clients: never secrets, internal paths or stack traces. Unhandled exceptions are
logged with their traceback and answered with a generic 500.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse, Response

from waystation_common.errors import WaystationError

log = logging.getLogger('waystation.errors')

PROBLEM_JSON = 'application/problem+json'

TITLES = {
    400: 'Bad Request',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not Found',
    405: 'Method Not Allowed',
    409: 'Conflict',
    413: 'Content Too Large',
    422: 'Unprocessable Content',
    429: 'Too Many Requests',
    500: 'Internal Server Error',
    502: 'Bad Gateway',
    503: 'Service Unavailable',
    504: 'Gateway Timeout',
    507: 'Insufficient Storage',
}

OCI_CODES = {
    400: 'UNSUPPORTED',
    401: 'UNAUTHORIZED',
    403: 'DENIED',
    404: 'NAME_UNKNOWN',
    405: 'UNSUPPORTED',
    413: 'SIZE_INVALID',
    429: 'TOOMANYREQUESTS',
}


class FieldError(BaseModel):
    field: str
    message: str
    code: str | None = None


class Problem(BaseModel):
    """RFC 9457 problem details (OpenAPI component ``Problem``)."""

    type: str = 'about:blank'
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[FieldError] | None = None


class FieldErrors(WaystationError):  # noqa: N818 - reads as a state, like NotFound
    """422 with field-level detail (``errors[]``)."""

    code = 'invalid'
    status = 422

    def __init__(self, errors: Sequence[FieldError], detail: str = 'The request has invalid fields.') -> None:
        super().__init__(detail)
        self.errors = list(errors)


def problem(
    status: int,
    detail: str | None = None,
    *,
    title: str | None = None,
    type_: str = 'about:blank',
    errors: Sequence[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        type=type_, title=title or TITLES.get(status, 'Error'), status=status, detail=detail, errors=errors or None
    )
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_JSON, headers=dict(headers or {})
    )


def oci_error(
    status: int,
    message: str,
    *,
    code: str | None = None,
    detail: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    entry: dict[str, Any] = {'code': code or OCI_CODES.get(status, 'UNKNOWN'), 'message': message}
    if detail is not None:
        entry['detail'] = detail
    return JSONResponse({'errors': [entry]}, status_code=status, headers=dict(headers or {}))


def native_error(status: int, message: str, *, headers: Mapping[str, str] | None = None) -> Response:
    return PlainTextResponse(f'{message}\n', status_code=status, headers=dict(headers or {}))


def _style(path: str) -> str:
    if path == '/v2' or path.startswith(('/v2/', '/auth/token')):
        return 'oci'
    if path.startswith('/repository/'):
        return 'native'
    return 'problem'


def error_response(request: Request, status: int, detail: str, *, headers: Mapping[str, str] | None = None) -> Response:
    style = _style(request.url.path)
    if style == 'oci':
        return oci_error(status, detail, headers=headers)
    if style == 'native':
        return native_error(status, detail, headers=headers)
    return problem(status, detail, headers=headers)


def _field_path(loc: Sequence[int | str]) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] in ('body', 'query', 'path', 'header', 'cookie'):
        parts = parts[1:]
    return '.'.join(parts) or 'body'


def validation_errors(raw: Sequence[Any]) -> list[FieldError]:
    out: list[FieldError] = []
    for err in raw:
        loc = [p for p in err.get('loc', ()) if not (isinstance(p, str) and p.startswith('function-'))]
        message = str(err.get('msg', 'invalid'))
        if message.startswith('Value error, '):
            message = message[len('Value error, ') :]
        out.append(FieldError(field=_field_path(loc), message=message, code=str(err.get('type', 'invalid'))))
    return out


def install(app: FastAPI) -> None:
    @app.exception_handler(FieldErrors)
    async def _field_errors(request: Request, exc: FieldErrors) -> Response:
        return problem(422, exc.detail, errors=exc.errors)

    @app.exception_handler(WaystationError)
    async def _domain(request: Request, exc: WaystationError) -> Response:
        if exc.status >= 500:
            log.warning('request failed', extra={'error_code': exc.code, 'status': exc.status})
        headers = getattr(exc, 'headers', None)
        return error_response(request, exc.status, exc.detail, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors = validation_errors(exc.errors())
        style = _style(request.url.path)
        if style != 'problem':
            return error_response(request, 400, errors[0].message if errors else 'invalid request')
        return problem(422, 'The request has invalid fields.', errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        detail = exc.detail if isinstance(exc.detail, str) else TITLES.get(exc.status_code, 'Error')
        return error_response(request, exc.status_code, detail, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        log.error('unhandled error', exc_info=exc)
        return error_response(request, 500, 'internal error')
