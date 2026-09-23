"""Auth router: POST /auth/register, POST /auth/login."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    API_KEY_PREFIX,
    create_access_token,
    dummy_verify,
    generate_api_key,
    generate_reset_token,
    get_password_hash,
    hash_api_key,
    verify_password,
)
from app.core.config import settings
from app.core.ratelimit import (
    check_locked,
    client_ip,
    enforce_attempt,
    record_failure,
    reset as rl_reset,
)
from app.db.models import (
    ApiKey,
    PasswordResetRequest,
    User,
    UserRole,
    get_async_session,
    is_admin_or_above,
    is_service_or_above,
)
from app.schemas.auth import (
    AdminApproveResetResponse,
    AdminResetRequestResponse,
    AdminUserResponse,
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    RoleUpdateRequest,
    TokenResponse,
    UserResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def _touch_api_key_last_used(key_id: int) -> None:
    """Best-effort last-used stamp, in its OWN session.

    Deliberately NOT done on the request session: if this write failed there,
    the rollback would expire the request's `user` object (rollback expires
    regardless of expire_on_commit), and the route's next attribute read would
    trigger a sync lazy-load under async -> MissingGreenlet. Isolating it means
    a failed stamp can never break the actual request.
    """
    from app.db.models import _async_session_factory

    try:
        async with _async_session_factory() as s:
            await s.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=datetime.now(UTC))
            )
            await s.commit()
    except Exception:
        log.warning("Failed to update API key last_used_at (key_id=%s)", key_id, exc_info=True)


# Only stamp last_used_at once it's gone stale. Stamping on *every* request made
# each API-key call write the same api_keys row; under a burst for one key those
# writes piled up on the row lock (each holding a pooled connection while it
# waited, plus a WAL fsync per commit), exhausting the pool. Throttling collapses
# a burst to a single write per interval, so the hot path normally writes nothing.
_TOUCH_INTERVAL = timedelta(seconds=60)


def _should_touch(last_used_at: datetime | None) -> bool:
    if last_used_at is None:
        return True
    # last_used_at is timestamptz (migration 006) so it's tz-aware; guard a
    # legacy naive value defensively rather than risk a TypeError in auth.
    if last_used_at.tzinfo is None:
        last_used_at = last_used_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - last_used_at >= _TOUCH_INTERVAL


async def resolve_user_from_token(token: str, session: AsyncSession) -> User | None:
    """Resolve a raw bearer credential — personal API key OR JWT — to its User.

    Returns None if the credential is invalid/unknown (non-raising). This is the
    SINGLE source of truth for "token -> user", which is precisely what
    guarantees an API key confers exactly the same identity — and therefore the
    same permissions — as a JWT. Every endpoint authorizes off the returned
    User.role, so both credential types are treated identically.

    Used by get_current_user (the standard Authorization-header dependency) and
    by endpoints that receive the token out-of-band, e.g. the SSE log stream's
    ?token= query param (browser EventSource cannot set an Authorization header).
    """
    if token.startswith(API_KEY_PREFIX):
        result = await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(token))
        )
        key = result.scalar_one_or_none()
        if key is None:
            return None
        user = await session.get(User, key.user_id)
        if user is not None and _should_touch(key.last_used_at):
            await _touch_api_key_last_used(key.id)
        return user

    from app.core.auth import decode_access_token

    data = decode_access_token(token)
    if data is None:
        return None
    result = await session.execute(select(User).where(User.id == int(data.sub)))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    # A password reset invalidates every session issued before it. A token minted
    # before the reset (older iat, or a legacy token with no iat at all) no longer
    # authenticates. NULL password_changed_at (never reset) skips the check.
    #
    # JWT `iat` is second-granular (RFC 7519 uses whole-second timestamps), while
    # password_changed_at carries sub-second precision. Floor the threshold to
    # whole seconds so a token legitimately minted in the SAME second as the reset
    # (e.g. the user logging straight back in) isn't spuriously rejected; a stale
    # token from any earlier second is still refused.
    if user.password_changed_at is not None:
        threshold = user.password_changed_at.replace(microsecond=0)
        if data.iat is None or data.iat < threshold:
            return None
    return user


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_async_session),
) -> User:
    # Personal API keys and JWTs share the Authorization: Bearer header; the
    # prefix (API_KEY_PREFIX) tells them apart. Both resolve to a live DB User,
    # so the JWT's role claim is never trusted for authz — role always comes
    # from the user row, identical for both credential types.
    user = await resolve_user_from_token(token, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    return user


async def get_session_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_async_session),
) -> User:
    """Like get_current_user, but accepts ONLY a website session (JWT) — personal
    API keys are rejected.

    For endpoints that are interactive website helpers and have no place in the
    programmatic API surface, e.g. the pre-flight "Test connection" check. API
    keys are how CI/automation submits, and a pre-test there is just wasted work.
    The prefix (API_KEY_PREFIX) tells the credential types apart; a JWT never
    starts with it.
    """
    if token.startswith(API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available from the website, not via an API key.",
        )
    user = await resolve_user_from_token(token, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    return user


async def get_session_or_service_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_async_session),
) -> User:
    """Like get_session_user, but also accepts an API key that belongs to a
    service account or an admin. The website helpers it guards (endpoint
    preflight) are pointless for a person's CI key, and exactly what another
    platform needs before it commits a submission."""
    user = await resolve_user_from_token(token, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token.startswith(API_KEY_PREFIX) and not is_service_or_above(user.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires a website session, not an API key",
        )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    # admin OR super_admin — super_admin is strictly higher privilege.
    if not is_admin_or_above(user.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


async def require_service_or_admin(user: User = Depends(get_current_user)) -> User:
    """Admins, or a service account. Routes that act on a single benchmark must
    also call `ensure_can_manage` — a service account manages only its own."""
    if not is_service_or_above(user.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin or service account required")
    return user


def ensure_can_manage(user: User, benchmark) -> None:
    """An admin manages every benchmark; a service account only the ones it
    created. Raises 403 naming the rule, so an automated caller that trips it
    gets an answer rather than a bare refusal."""
    if is_admin_or_above(user.role):
        return
    if user.role == UserRole.SERVICE and benchmark.created_by_user_id == user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="A service account may manage only the benchmarks it created",
    )


async def require_super_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin required")
    return user


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    # Throttle per-IP so signup can't be used to mass-create accounts or probe
    # which emails/usernames are taken.
    await enforce_attempt(
        f"rl:register:ip:{client_ip(request)}",
        settings.REGISTER_IP_MAX_ATTEMPTS,
        settings.REGISTER_IP_WINDOW_SECONDS,
    )
    existing = await session.execute(
        select(User).where((User.email == body.email) | (User.username == body.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email or username taken")

    user = User(
        email=body.email,
        username=body.username,
        password_hash=get_password_hash(body.password),
        role="user",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    return TokenResponse(access_token=token, role=user.role.value)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    """Authenticate with email + password. Returns a JWT token."""
    # Two-layer brute-force defence, both counting only FAILED attempts: a per-IP
    # cap (spray across accounts from one host) and a per-account cap (targeted
    # brute force / lockout). Both are checked before any password work so a
    # locked bucket fails fast, and a successful login never accrues toward
    # either — so legitimate users, even behind a shared IP, aren't locked out.
    ip_key = f"rl:login:ipfail:{client_ip(request)}"
    email_key = f"rl:login:fail:{body.email.lower()}"
    await check_locked(ip_key, settings.LOGIN_IP_MAX_FAILURES)
    await check_locked(email_key, settings.LOGIN_EMAIL_MAX_FAILURES)

    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    async def _fail() -> HTTPException:
        await record_failure(ip_key, settings.LOGIN_IP_WINDOW_SECONDS)
        await record_failure(email_key, settings.LOGIN_EMAIL_WINDOW_SECONDS)
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user is None:
        # Spend a verify anyway so timing doesn't reveal that the email is unknown.
        dummy_verify(body.password)
        raise await _fail()
    if not verify_password(body.password, user.password_hash):
        raise await _fail()

    # Successful login clears only the account's failure count. We deliberately do
    # NOT clear the IP counter — otherwise an attacker holding one valid account
    # could reset it at will and spray indefinitely.
    await rl_reset(email_key)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    return TokenResponse(access_token=token, role=user.role.value)


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)):
    return UserResponse(id=user.id, email=user.email, username=user.username, role=user.role.value)


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Let an authenticated user change their own password.

    Requires the current password — an attacker with a stolen token still
    can't lock the owner out without it.

    Changing the password invalidates every OTHER active session (any JWT minted
    before now stops authenticating, same as a reset — so a leaked token can't
    outlive the password it was issued under). We then mint and return a fresh
    token so the caller's current session stays alive; the client should replace
    its stored token with it. Personal API keys are unaffected (revoke those
    separately).
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one")
    user.password_hash = get_password_hash(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    await session.commit()

    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    return TokenResponse(access_token=token, role=user.role.value)


# ---------------------------------------------------------------------------
# Forgot password — self-service request, fulfilled by a super_admin.
#
# There is no email service, so recovery is "admin hands it over": the user
# files a request from a public page; a super_admin approves it, which mints a
# single-use, short-TTL token; the super_admin passes the resulting link to the
# user out-of-band; the user sets their own new password via the token.
#
# By design the API can only reset a plain `user`. admin/super_admin passwords
# stay DB-script-only (scripts/reset-password.sh) so privileged-account takeover
# never has an API path. The public endpoints return a generic 202 regardless of
# whether the email exists or is eligible, so they leak neither.
# ---------------------------------------------------------------------------

# Reuse across the two public responses so they are byte-identical (no
# distinguishing existence/eligibility via the response body).
_FORGOT_PASSWORD_MESSAGE = (
    "If an eligible account exists for that email, an administrator will prepare "
    "a reset link. Contact your administrator to receive it."
)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_async_session),
):
    """Public: file a password-reset request. Always returns the same 202."""
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    # Only plain users are eligible; admins/super_admins are DB-script-only.
    if user is not None and user.role == UserRole.USER:
        # Dedupe: if a pending (un-approved, un-canceled, un-used) request already
        # exists, don't stack another — one open request per user is enough.
        existing = await session.execute(
            select(PasswordResetRequest).where(
                PasswordResetRequest.user_id == user.id,
                PasswordResetRequest.approved_at.is_(None),
                PasswordResetRequest.canceled_at.is_(None),
                PasswordResetRequest.used_at.is_(None),
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(PasswordResetRequest(user_id=user.id))
            await session.commit()

    return {"message": _FORGOT_PASSWORD_MESSAGE}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetPasswordRequest,
    session: AsyncSession = Depends(get_async_session),
):
    """Public: consume an approved reset token and set a new password."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(PasswordResetRequest).where(
            PasswordResetRequest.token_hash == hash_api_key(body.token)
        )
    )
    req = result.scalar_one_or_none()
    # One generic error for every failure mode — don't tell an attacker which
    # part (unknown / unapproved / used / expired) failed.
    invalid = HTTPException(status_code=400, detail="Invalid or expired reset token")
    if req is None or req.approved_at is None or req.used_at is not None:
        raise invalid
    if req.canceled_at is not None or req.expires_at is None or now >= req.expires_at:
        raise invalid

    user = await session.get(User, req.user_id)
    # Defense in depth: never let a token reset a privileged account even if a
    # row somehow points at one.
    if user is None or user.role != UserRole.USER:
        raise invalid

    user.password_hash = get_password_hash(body.new_password)
    user.password_changed_at = now  # invalidates JWTs minted before now
    req.used_at = now
    await session.commit()


@router.get("/admin/password-resets", response_model=list[AdminResetRequestResponse])
async def admin_list_password_resets(
    _admin: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """The super_admin queue: open reset requests (pending + approved-unused)."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(PasswordResetRequest, User)
        .join(User, User.id == PasswordResetRequest.user_id)
        .where(
            PasswordResetRequest.used_at.is_(None),
            PasswordResetRequest.canceled_at.is_(None),
        )
        .order_by(PasswordResetRequest.created_at.desc())
    )
    return [
        AdminResetRequestResponse(
            id=req.id,
            user_id=user.id,
            username=user.username,
            email=user.email,
            role=user.role.value,
            status=req.status(now),
            created_at=req.created_at,
            approved_at=req.approved_at,
            expires_at=req.expires_at,
        )
        for req, user in result.all()
    ]


@router.post("/admin/password-resets/{request_id}/approve", response_model=AdminApproveResetResponse)
async def admin_approve_password_reset(
    request_id: int,
    admin: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Approve a request → mint a single-use token, shown to the admin once."""
    req = await session.get(PasswordResetRequest, request_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if req.used_at is not None or req.canceled_at is not None:
        raise HTTPException(status_code=409, detail="Request is already used or canceled")

    target = await session.get(User, req.user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")
    if target.role != UserRole.USER:
        # Mirrors the role-mutation model: privileged accounts are DB-only.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admins and super_admins must be reset via the DB script, not the API",
        )

    now = datetime.now(UTC)
    full_token, prefix, token_hash = generate_reset_token()
    # (Re-)approving mints a fresh token and resets the clock — covers a lost or
    # expired link without needing a brand-new request.
    req.token_hash = token_hash
    req.token_prefix = prefix
    req.approved_at = now
    req.approved_by_user_id = admin.id
    expires_at = now + timedelta(minutes=settings.RESET_TOKEN_TTL_MINUTES)
    req.expires_at = expires_at
    await session.commit()

    return AdminApproveResetResponse(
        token=full_token,  # only time the plaintext is ever returned
        token_prefix=prefix,
        expires_at=expires_at,
        path=f"/reset-password?token={full_token}",
    )


@router.post(
    "/admin/password-resets/{request_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def admin_reject_password_reset(
    request_id: int,
    _admin: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Reject/cancel a request. A used request can't be canceled."""
    req = await session.get(PasswordResetRequest, request_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if req.used_at is not None:
        raise HTTPException(status_code=409, detail="Request is already used")
    if req.canceled_at is None:
        req.canceled_at = datetime.now(UTC)
        await session.commit()


# ---------------------------------------------------------------------------
# Personal API keys — long-lived credentials a user can use in place of the
# expiring JWT. Presented as `Authorization: Bearer <key>`. The plaintext is
# shown exactly once, at creation; only its hash is stored.
# ---------------------------------------------------------------------------


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    full_key, prefix, key_hash = generate_api_key()
    key = ApiKey(user_id=user.id, name=body.name.strip(), key_prefix=prefix, key_hash=key_hash)
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return ApiKeyCreatedResponse(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        key=full_key,  # only time the plaintext is ever returned
    )


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in result.scalars()
    ]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Permanently revoke (delete) one of the caller's own keys."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    await session.delete(key)
    await session.commit()


# ---------------------------------------------------------------------------
# Admin user management — list users (any admin) and grant/revoke admin
# (super_admin only).
#
# Role mutation is reserved for super_admins: plain admins can VIEW the user
# list but cannot promote or revoke anyone. super_admin itself is never
# grantable through this API — it is set only by direct DB access from inside
# the backend pod/container (see the runbook). A super_admin row cannot be
# modified here at all, which means admins can't touch super_admins and
# super_admins can't revoke each other.
# ---------------------------------------------------------------------------


@router.get("/admin/users", response_model=list[AdminUserResponse])
async def admin_list_users(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(select(User).order_by(User.created_at))
    return [
        AdminUserResponse(
            id=u.id,
            email=u.email,
            username=u.username,
            role=u.role.value,
            created_at=u.created_at,
        )
        for u in result.scalars()
    ]


@router.patch("/admin/users/{user_id}/role", response_model=AdminUserResponse)
async def admin_set_user_role(
    user_id: int,
    body: RoleUpdateRequest,
    _admin: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
):
    new_role = body.role.strip().lower()
    # super_admin can only be granted/revoked out-of-band (direct DB), never here.
    if new_role == "super_admin":
        raise HTTPException(status_code=422, detail="super_admin cannot be granted via the API")
    if new_role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")

    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # A super_admin's role is managed only in the DB. This single guard covers
    # both "admins can't touch super_admins" and "super_admins can't revoke each
    # other" (incl. a super_admin trying to demote itself).
    if target.role == UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="A super_admin's role can only be changed directly in the database",
        )

    # Safety: never empty the in-band admin pool by demoting the last admin.
    if target.role == UserRole.ADMIN and new_role == "user":
        admin_count = await session.scalar(
            select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
        )
        if (admin_count or 0) <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last remaining admin")

    target.role = UserRole.ADMIN if new_role == "admin" else UserRole.USER
    await session.commit()
    await session.refresh(target)
    return AdminUserResponse(
        id=target.id,
        email=target.email,
        username=target.username,
        role=target.role.value,
        created_at=target.created_at,
    )
