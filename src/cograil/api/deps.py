"""Dependencies the routers share: the Services of the app and the signed-in Principal."""

from typing import Annotated

from fastapi import Depends, Request

from cograil.api.auth import current_principal
from cograil.api.services import Services
from cograil.domain import Principal


def get_services(request: Request) -> Services:
    services: Services = request.app.state.cograil_services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
