from pydantic import BaseModel


class AdminAIConfigPayload(BaseModel):
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    clear_api_key: bool = False


class AdminUserCreatePayload(BaseModel):
    username: str
    password: str
    role: str = "user"


class AdminUserUpdatePayload(BaseModel):
    username: str = ""
    role: str = ""


class AdminUserResetPasswordPayload(BaseModel):
    new_password: str


class AdminUserToggleActivePayload(BaseModel):
    is_active: bool


class DatasetCleanupPayload(BaseModel):
    filename: str
    preview: bool = True
