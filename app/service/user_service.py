from datetime import timedelta
import logging
from typing import Optional, List
from uuid import UUID

from app.core.config import settings
from app.models import user
from langchain_classic.embeddings import awa

from app.core.security import create_access_token, get_password_hash, verify_password
from sqlalchemy import func, select, update,desc,asc
from app.models.user import Role, User, UserRoleAssociation
from app.schemas.user import Token, UserCreate, UserInDB
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

    async def login_user(self, username_or_email: str, password: str) -> dict:
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
        if not user:
            logger.warning(f"登录失败，用户名或密码错误: {username_or_email}")
            raise ValueError("用户名或密码错误")

        if not user.is_active:
            logger.warning(f"登录失败，用户未激活: {username_or_email}")
            raise ValueError("用户未激活")

        logger.info(f"为用户 {user.username} 创建访问令牌")
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": str(user.id)}, expires_delta=access_token_expires
        )
        logger.info(f"用户登录成功:{user.username}")
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }

    async def authenticate(
        self, username_or_email: str, password: str
    ) -> Optional[User]:
        """通过用户名或邮箱和密码认证用户
        Args:
            username_or_email (str): 用户名或邮箱
            password (str): 密码
        Returns:
            User: 认证成功的用户对象，如果认证失败则返回None
        """
        try:
            user = await self.get_user_by_username(username_or_email)
            if not user:
                user = await self.get_user_by_email(username_or_email)
            if not user:
                return None

            if not verify_password(password, user.hashed_password):  # type: ignore
                return None

            await self._update_last_login(user.id)  # type: ignore
            return user
        except Exception as e:
            logger.error(f"认证用户时出错: {e}")
            raise

    async def _update_last_login(self, userid: UUID) -> None:
        """更新用户的最后登录时间"""
        try:
            query = update(User).where(User.id == userid).values(last_login=func.now())
            await self.db.execute(query)
            await self.db.commit()

        except Exception as e:
            logger.error(f"更新用户 {userid} 的最后登录时间时出错: {e}")
            raise
    async def refresh_token(self,user_id:str)->dict:
        """
            刷新用户访问令牌

            Args:
                user_id: 用户ID

            Returns:
                包含新访问令牌的字典
            """
        from app.core.config import settings
        from datetime import timedelta
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": str(user_id)}, expires_delta=access_token_expires
        )
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        }

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """
        通过ID获取用户（兼容字符串ID）

        Args:
            user_id: 用户ID（字符串或UUID）

        Returns:
            用户对象或None
        """
        try:
            from uuid import UUID
            if isinstance(user_id, str):
                user_id = UUID(user_id)
            return await self.get_user(user_id)
        except Exception as e:
            logger.error(f"通过ID获取用户时出错: {e}")
            return None
    async  def get_user(self,user_id:UUID)->User:
        """
        获取用户
        Args:
            user_id (UUID): 用户ID
        Returns:
            User: 用户对象
        """
        query = select(User).where(User.id == user_id)
        result=await self.db.execute(query)
        return result.scalar_one_or_none()
    async  def get_user_with_roles(self,user_id:str)->dict:
        """
        获取用户及其角色
        Args:
            user_id (str): 用户ID
        Returns:
            User: 包含用户和角色的User对象
        """
        import  logging
        logger=logging.getLogger(__name__)
        logger.info(f"正在获取用户 {user_id} 及其角色")
        try:
            # 获取用户信息
            user = await self.get_user_by_id(user_id)
            if not user:
                raise ValueError("用户不存在")
            # 获取角色信息
            role_service = RoleService(self.db)
            roles = await role_service.list_user_roles(user.id)
            return {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "phone": user.phone,
                "department": user.department,
                "position": user.position,
                "employee_id": user.employee_id,
                "role": user.role,
                "is_superuser": user.is_superuser,
                "is_verified": user.is_verified,
                "is_active": user.is_active,
                "bio": user.bio,
                "avatar_url": user.avatar_url,
                "last_login": user.last_login,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
                "roles": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "description": r.description,
                        "is_builtin": r.is_builtin,
                        "created_at": r.created_at,
                        "updated_at": r.updated_at,
                    }
                    for r in roles
                ],
            }
        except Exception as e:
            logger.error(f"❌ 获取用户信息时出错: {e}")
            raise

class RoleService:
    """
    角色服务类
    """
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_user_roles(self, user_id: str) -> List[Role]:
        """
        获取用户角色列表
        Args:
            user_id (str): 用户ID
        Returns:
            List[Role]: 用户角色列表
        """
        try:
            query = (
                select(Role)
                .join(UserRoleAssociation, Role.id == UserRoleAssociation.role_id)
                .where(UserRoleAssociation.user_id == user_id,Role.is_active==True).order_by(desc(Role.created_at))
            )
            result = await self.db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"获取用户角色列表时出错: {e}")
            raise