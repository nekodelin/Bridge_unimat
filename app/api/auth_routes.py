from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_auth_service, get_current_user
from app.schemas import AuthLoginRequest, AuthLoginResponse, AuthLogoutResponse, AuthMeResponse
from app.services import AuthService, AuthenticatedUser, BridgeRuntime

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _runtime(request: Request) -> BridgeRuntime:
    return request.app.state.runtime


@router.post("/login", response_model=AuthLoginResponse)
async def post_login(
    request: Request,
    body: AuthLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthLoginResponse:
    user = await auth_service.authenticate_credentials(body.username, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token, expires_in = auth_service.create_access_token(user)
    runtime = _runtime(request)
    await runtime.append_auth_event(username=user.username, action="login")
    return AuthLoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.get("/me", response_model=AuthMeResponse)
async def get_me(current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthMeResponse:
    return AuthMeResponse(
        id=current_user.id,
        username=current_user.username,
        createdAt=current_user.created_at,
    )


@router.post("/logout", response_model=AuthLogoutResponse)
async def post_logout(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthLogoutResponse:
    runtime = _runtime(request)
    await runtime.append_auth_event(username=current_user.username, action="logout")
    return AuthLogoutResponse(ok=True)
