import logging
from typing import Optional

from app.core.security import get_password_hash, verify_password
from sqlalchemy import select
from app.models.user import Role, User, UserRoleAssociation
from app.schemas.user import Token, UserCreate
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class UserService:
    """基于管理用户的服务"""

    def __init__(self, db: AsyncSession) -> None:
        """初始化UserService实例
        Args:
              db (AsyncSession): 数据库会话对象
        """

        self.db = db

    async def register_user(self, user_data: UserCreate) -> Optional[User]:
        """
        注册新用户
        Args:
            user_data (UserCreate): 用户注册数据
        Returns:
            User: 注册成功的用户对象
        Raises:
            ValueError: 如果用户名或邮箱已存在
        """
        existing_user = await self.get_user_by_username(user_data.username)
        if existing_user:
            raise ValueError(f"用户名 {user_data.username} 已存在")
        existing_user = await self.get_user_by_email(user_data.email)
        if existing_user:
            raise ValueError(f"邮箱 {user_data.email} 已存在")
        return await self.create_user(user_data)

    async def create_user(self, user_data: UserCreate) -> Optional[User]:
        """
        创建新用户
        Args:
            user_data (UserCreate): 用户注册数据
        Returns:
            User: 创建成功的用户对象
        """
        try:
            # 检查用户是否已存在
            existing_user = await self.get_user_by_email(user_data.email)
            if existing_user:
                raise ValueError("使用此邮箱的用户已存在")

            # 检查用户名是否已被占用
            existing_username = await self.get_user_by_username(user_data.username)
            if existing_username:
                raise ValueError("用户名已被占用")
            hased_password = get_password_hash(user_data.password)
            user = User(
                username=user_data.username,
                email=user_data.email,
                full_name=user_data.full_name,
                phone=user_data.phone,
                department=user_data.department,
                position=user_data.position,
                employee_id=user_data.employee_id,
                role=user_data.role,
                bio=user_data.bio,
                hashed_password=hased_password,
            )
            self.db.add(user)
            await self.db.commit()
            await self.db.refresh(user)
            await self._assign_default_role(user)
            logger.info(f"用户 {user.username} 创建成功")
            return user

        except Exception as e:
            logger.error(f"创建用户时出错: {e}")
            raise

    async def _assign_default_role(self, user: User) -> None:
        """为新用户分配默认的普通用户角色"""
        try:
            result = await self.db.execute(select(Role).where(Role.name == "普通用户"))
            default_role = result.scalar_one_or_none()
            if default_role:
                user_role_assoc = UserRoleAssociation(
                    user_id=user.id, role_id=default_role.id
                )
                self.db.add(user_role_assoc)
                await self.db.commit()
                logger.info(f"为用户 {user.username} 分配默认角色成功")
        except Exception as e:
            logger.error(f"为用户 {user.username} 分配默认角色时出错: {e}")
            raise

    async def get_user_by_username(self, username: str) -> Optional[User]:
        """
        根据用户名获取用户
        Args:
            username (str): 用户名
        Returns:
            User: 用户对象，如果不存在则返回None
        """
        try:
            quer = select(User).where(User.username == username)
            result = await self.db.execute(quer)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"通过用户名{username}获取用户时出错: {e}")
            raise

    async def get_user_by_email(self, email: str) -> Optional[User]:
        """
        根据邮箱获取用户
        Args:
            email (str): 用户邮箱
        Returns:
            User: 用户对象，如果不存在则返回None
        """
        try:
            quer = select(User).where(User.email == email)
            result = await self.db.execute(quer)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"通过邮箱{email}获取用户时出错: {e}")
            raise

    async def login_user(self, username_or_email: str, password: str) -> Token:
        """
        用户登录
        Args:
            username (str): 用户名
            password (str): 密码
        Returns:
            Token: 登录成功后返回的Token对象
        Raises:
            ValueError: 如果用户名或密码错误
        """

        logger.info(f"登录尝试，用户名: {username_or_email}")
        # 认证用户
        user = await self.authenticate(username_or_email, password)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    async def authenticate(self, username_or_email: str, password: str) -> Optional[User]:
        