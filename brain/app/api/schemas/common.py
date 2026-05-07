from pydantic import BaseModel

class ErrorResponse(BaseModel):
    error: str
    code: str | None = None
