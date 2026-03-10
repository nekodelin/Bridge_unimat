from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import User
from app.utils import now_utc

PASSWORD_CONTEXT = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


@dataclass(slots=True)
class AuthenticatedUser:
    id: int
    username: str
    created_at: datetime


class AuthService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory

    async def ensure_first_admin(self) -> AuthenticatedUser:
        username = self.settings.first_admin_username.strip()
        password = self.settings.first_admin_password
        if not username:
            raise RuntimeError("FIRST_ADMIN_USERNAME is empty")
        if not password:
            raise RuntimeError("FIRST_ADMIN_PASSWORD is empty")

        async with self._session_factory() as session:
            user = await self._get_user_by_username(session, username=username)
            if user is None:
                user = User(
                    username=username,
                    password_hash=self.hash_password(password),
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
            return AuthenticatedUser(id=user.id, username=user.username, created_at=user.created_at)

    async def authenticate_credentials(self, username: str, password: str) -> AuthenticatedUser | None:
        clean_username = username.strip()
        if not clean_username:
            return None
        async with self._session_factory() as session:
            user = await self._get_user_by_username(session, username=clean_username)
            if user is None:
                return None
            if not self.verify_password(password, user.password_hash):
                return None
            return AuthenticatedUser(id=user.id, username=user.username, created_at=user.created_at)

    async def authenticate_token(self, token: str) -> AuthenticatedUser | None:
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret_key,
                algorithms=[self.settings.jwt_algorithm],
            )
        except JWTError:
            return None

        sub = payload.get("sub")
        if not sub:
            return None

        try:
            user_id = int(sub)
        except (TypeError, ValueError):
            return None

        async with self._session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                return None
            return AuthenticatedUser(id=user.id, username=user.username, created_at=user.created_at)

    def create_access_token(self, user: AuthenticatedUser) -> tuple[str, int]:
        expires_in = int(self.settings.access_token_expire_minutes) * 60
        expire_at = now_utc() + timedelta(seconds=expires_in)
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "exp": expire_at,
        }
        token = jwt.encode(payload, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)
        return token, expires_in

    @staticmethod
    def verify_password(plain_password: str, password_hash: str) -> bool:
        if not plain_password:
            return False
        return PASSWORD_CONTEXT.verify(plain_password, password_hash)

    @staticmethod
    def hash_password(password: str) -> str:
        return PASSWORD_CONTEXT.hash(password)

    @staticmethod
    async def _get_user_by_username(session: AsyncSession, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
