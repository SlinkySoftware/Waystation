"""Shared dependencies for REST handlers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from waystation_server.services import Services


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


def client_ip(request: Request) -> str:
    return str(getattr(request.state, 'client_ip', '') or 'unknown')


ServicesDep = Annotated[Services, Depends(services)]
