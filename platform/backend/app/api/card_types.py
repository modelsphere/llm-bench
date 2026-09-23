"""Card-types router: public (authenticated) list + admin CRUD.

The card-type list feeds the submission "Hardware" section's dropdown. Any
authenticated user may read it; only admins may mutate it. Ordered by
`display_order`, ties broken by `name`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user, require_admin
from app.db.models import CardType, User, get_async_session
from app.schemas.card_types import (
    CardTypeCreate,
    CardTypeListResponse,
    CardTypeResponse,
    CardTypeUpdate,
)

router = APIRouter(prefix="/card-types", tags=["card-types"])


def _to_response(card: CardType) -> CardTypeResponse:
    return CardTypeResponse(
        id=card.id,
        name=card.name,
        display_order=card.display_order,
        created_at=card.created_at,
    )


async def _list_ordered(session: AsyncSession) -> list[CardType]:
    result = await session.execute(
        select(CardType).order_by(CardType.display_order, CardType.name)
    )
    return list(result.scalars().all())


@router.get("", response_model=CardTypeListResponse)
async def list_card_types(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """List card types (any authenticated user) for the submit-form dropdown."""
    cards = await _list_ordered(session)
    return CardTypeListResponse(card_types=[_to_response(c) for c in cards])


@router.post("", response_model=CardTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_card_type(
    body: CardTypeCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    existing = await session.execute(select(CardType).where(CardType.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Card type {body.name!r} already exists",
        )
    card = CardType(name=body.name, display_order=body.display_order)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return _to_response(card)


@router.put("/{card_type_id}", response_model=CardTypeResponse)
async def update_card_type(
    card_type_id: int,
    body: CardTypeUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(select(CardType).where(CardType.id == card_type_id))
    card = result.scalar_one_or_none()
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Card type not found")

    if body.name is not None and body.name != card.name:
        clash = await session.execute(
            select(CardType).where(CardType.name == body.name, CardType.id != card_type_id)
        )
        if clash.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Card type {body.name!r} already exists",
            )
        card.name = body.name
    if body.display_order is not None:
        card.display_order = body.display_order

    await session.commit()
    await session.refresh(card)
    return _to_response(card)


@router.delete("/{card_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card_type(
    card_type_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(select(CardType).where(CardType.id == card_type_id))
    card = result.scalar_one_or_none()
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Card type not found")
    # No FK from submissions (card_type is a free string snapshot), so deleting a
    # card type only removes it as a future choice — historical rows are untouched.
    await session.delete(card)
    await session.commit()
