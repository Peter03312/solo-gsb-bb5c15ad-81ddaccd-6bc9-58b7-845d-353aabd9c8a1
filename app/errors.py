"""统一的错误响应类型。"""


class ApiError(Exception):
    """所有业务错误的基类，序列化为 {"error": {...}}。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
