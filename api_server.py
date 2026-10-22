"""Provide a small HTTP API for configured Quickbase table refreshes."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qb_refresh import (
    RefreshError,
    RefreshSettings,
    load_environment,
    refresh_table,
)


_bearer = HTTPBearer(auto_error=False)
_error_status = {
    "BUSY": status.HTTP_409_CONFLICT,
    "AUTHENTICATION_REQUIRED": status.HTTP_401_UNAUTHORIZED,
    "CAPTCHA_FAILED": 422,
    "REFRESH_TIMEOUT": status.HTTP_504_GATEWAY_TIMEOUT,
    "REFRESH_FAILED": status.HTTP_502_BAD_GATEWAY,
    "CONFIG_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def create_app(config_path: str | Path | None = None) -> FastAPI:
    """Create the API. Read the settings and API key at startup."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        selected_path = Path(
            config_path
            if config_path is not None
            else os.environ.get("QB_REFRESH_CONFIG", "refresh_config.json")
        ).expanduser().resolve()
        load_environment(selected_path.parent / ".env")

        api_key = os.environ.get("QB_REFRESH_API_KEY", "")
        if not api_key.strip():
            raise RuntimeError("Set QB_REFRESH_API_KEY before the API starts.")

        application.state.refresh_settings = RefreshSettings.from_file(selected_path)
        application.state.refresh_api_key = api_key.encode("utf-8")
        try:
            yield
        finally:
            application.state.refresh_api_key = b""

    application = FastAPI(
        title="Quickbase Table Refresh",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def require_api_key(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> None:
        """Require the API key before the request can select a table."""
        supplied_key = (
            credentials.credentials.encode("utf-8") if credentials is not None else b""
        )
        expected_key = request.app.state.refresh_api_key
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(supplied_key, expected_key)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "UNAUTHORIZED",
                    "message": "A valid bearer token is required.",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Report that the API started. Do not expose account information."""
        return {"status": "ok"}

    @application.post(
        "/refresh/{table_name}",
        dependencies=[Depends(require_api_key)],
    )
    async def refresh(table_name: str, request: Request) -> dict:
        """Refresh a configured table and wait for the result."""
        settings: RefreshSettings = request.app.state.refresh_settings
        if table_name not in settings.tables:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "UNKNOWN_TABLE",
                    "message": "The table name is not configured.",
                },
            )

        try:
            result = await refresh_table(settings, table_name)
        except RefreshError as error:
            raise HTTPException(
                status_code=_error_status.get(
                    error.code, status.HTTP_502_BAD_GATEWAY
                ),
                detail={"code": error.code, "message": error.message},
            ) from None

        return result.to_dict()

    return application


app = create_app()
