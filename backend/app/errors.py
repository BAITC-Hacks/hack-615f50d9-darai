"""Uniform error envelope: {"error": {"code", "message", "details"}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("darai.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str = "Объект") -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} не найден")


def forbidden(message: str = "Недостаточно прав") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, code, message, details)


def validation(message: str, loc: list[str] | None = None) -> ApiError:
    return ApiError(
        422, "VALIDATION_ERROR", message, {"fields": [{"loc": loc or [], "msg": message}]}
    )


def _body(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


_HTTP_CODES = {400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
               405: "BAD_REQUEST", 409: "CONFLICT", 413: "FILE_TOO_LARGE"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=_body(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        fields = [
            {"loc": [str(p) for p in err.get("loc", [])], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_body("VALIDATION_ERROR", "Некорректные данные запроса", {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = _HTTP_CODES.get(exc.status_code, "BAD_REQUEST")
        message = exc.detail if isinstance(exc.detail, str) else code
        return JSONResponse(status_code=exc.status_code, content=_body(code, message))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        # Only the exception type is logged: messages may contain meeting content.
        log.error("unhandled error: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content=_body("INTERNAL_ERROR", "Внутренняя ошибка сервера"))
