from typing import Any

from fastapi.security import OAuth2PasswordRequestForm
from app.service.user_service import UserService
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas.user import Token, User as UserSchema, UserCreate
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.post("/register", response_model=UserSchema)
async def register_user(
    user_data: UserCreate, db: AsyncSession = Depends(get_db)
) -> Any:
    """注册新用户"""
    user_service = UserService(db)
    try:
        user = await user_service.register_user(user_data)  # type: ignore
        return user
    except ValueError as e:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/login", response_model=Token)
async def loin(
    from_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)
) -> Any:
    user_service = UserService(db)
    try:
        token_ata = await user_service.login_user(from_data.username, from_data.password)  # type: ignore
        return token_ata
    except ValueError as e:
        if "用户名或密码错误" in str(e):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
