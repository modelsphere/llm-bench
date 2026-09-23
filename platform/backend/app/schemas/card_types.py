"""Pydantic schemas for the admin-managed GPU card-type list."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CardTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    display_order: int = Field(default=0, ge=0)


class CardTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    display_order: int | None = Field(default=None, ge=0)


class CardTypeResponse(BaseModel):
    id: int
    name: str
    display_order: int
    created_at: datetime


class CardTypeListResponse(BaseModel):
    card_types: list[CardTypeResponse]
