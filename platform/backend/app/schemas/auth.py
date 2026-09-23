"""Pydantic schemas for auth endpoints."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    role: str


class AdminUserResponse(BaseModel):
    """User row for the admin Users page (adds created_at)."""
    id: int
    email: str
    username: str
    role: str
    created_at: datetime


class RoleUpdateRequest(BaseModel):
    role: str  # "admin" | "user"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ApiKeyResponse(BaseModel):
    """API key metadata — never includes the secret."""
    id: int
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Returned only at creation time — carries the plaintext key once."""
    key: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class AdminResetRequestResponse(BaseModel):
    """A password-reset request row for the super_admin queue."""
    id: int
    user_id: int
    username: str
    email: str
    role: str
    status: str  # pending | approved | used | expired | canceled
    created_at: datetime
    approved_at: datetime | None
    expires_at: datetime | None


class AdminApproveResetResponse(BaseModel):
    """Returned once when a super_admin approves — carries the plaintext token.

    `path` is a site-relative reset URL; the frontend prepends its own origin so
    the backend needs no knowledge of the frontend host.
    """
    token: str
    token_prefix: str
    expires_at: datetime
    path: str
