"""RFC 7807 problem+json errors."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


class Problem(Exception):
    def __init__(self, status: int, title: str, detail: str | None = None, *,
                 type_: str = "about:blank", errors: list[dict[str, Any]] | None = None,
                 headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None
                 ) -> None:
        super().__init__(detail or title)
        self.status, self.title, self.detail = status, title, detail
        self.type, self.errors, self.headers, self.extra = type_, errors, headers, extra


def problem_response(request: Request, status: int, title: str, detail: str | None = None, *,
                     type_: str = "about:blank", errors: list[dict[str, Any]] | None = None,
                     headers: dict[str, str] | None = None,
                     extra: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": type_, "title": title, "status": status,
                            "instance": request.url.path}
    if detail:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = errors
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(request, exc.status, exc.title, exc.detail, type_=exc.type,
                                errors=exc.errors, headers=exc.headers, extra=exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"key": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
                  for e in exc.errors()]
        return problem_response(request, 422, "Invalid request", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(request, exc.status_code, str(exc.detail),
                                headers=getattr(exc, "headers", None))
