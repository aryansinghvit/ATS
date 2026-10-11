"""Domain errors raised by services; mapped to HTTP responses in app.main."""


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


class PermissionDeniedError(ServiceError):
    status_code = 403


class UnprocessableError(ServiceError):
    status_code = 422
