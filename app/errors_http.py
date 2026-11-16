from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import DomainError


def install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    def handle_domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})
