from fastapi import APIRouter

from .auth_routes import router as auth_router
from .routes import router as api_router

router = APIRouter()
router.include_router(api_router)
router.include_router(auth_router)

__all__ = ["router", "api_router", "auth_router"]
