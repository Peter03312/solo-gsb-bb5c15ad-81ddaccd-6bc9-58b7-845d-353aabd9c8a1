"""Uniform API error shape: {"error": {"code": ..., "message": ...}}."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def bad_request(code: str, message: str) -> ApiError:
    return ApiError(400, code, message)
