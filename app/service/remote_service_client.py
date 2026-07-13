import json
import logging
from uuid import UUID

import httpx

from app.core.config import settings
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class RemoteServiceClient:
    """
    远程服务客户端类
    """

    def __init__(self):
        self.base_url = (
            f"http://{settings.HR_SERVICE_HOST}:{settings.HR_SERVICE_PORT}/api/v1"
        )
        self.api_key = settings.HR_SERVICE_APIKEY
        self.timeout = 30.0
        self.log_enabled = settings.REMOTE_SERVICE_LOG_ENABLED

    def _get_headers(self) -> dict[str, str]:
        """
        获取请求头
        Returns:
            请求头字典
        """
        return {"Content-Type": "application/json", "X-API-KEY": self.api_key}

    def _get_params(self, user_id: UUID, **kwargs) -> dict[str, str]:
        """
        获取请求参数
        Args:
            user_id: 用户ID
            kwargs: 其他参数

        Returns:
            请求参数字典
        """
        return {"current_user_id": str(user_id), **kwargs}

    def _handler_response(
        self, response: httpx.Response, expected_status: int = 200
    ) -> dict[str, Any]:
        """
        统一处理HTTP响应

         Args:
             response: HTTP响应对象
             expected_status: 期望的状态码，默认为200

         Returns:
             解析后的JSON数据

         Raises:
             ValueError: 当响应状态码不符合预期时
        """

        # 检查响应状态码
        if response.status_code == 401:
            raise ValueError("API密钥未提供")
        elif response.status_code == 403:
            raise ValueError("API密钥认证失败或余额不足或超过有效期")
        elif response.status_code == 404:
            raise ValueError("资源未找到")
        elif response.status_code != expected_status:
            raise ValueError(
                f"远程服务返回错误: {response.status_code} - {response.text}"
            )

        remaining_calls = response.headers.get("X-RateLimit-Remaining")
        if remaining_calls:
            logger.info(f"剩余调用次数: {remaining_calls}")
        return response.json()

    async def post(
        self,
        endpoint: str,
        data: Dict[str, Any],
        user_id: UUID,
        additional_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发送POST请求

        Args:
            endpoint: 请求的端点
            data: 请求的数据
            user_id: 用户ID
            additional_params: 额外的参数

        Returns:
            响应数据

        Raises:
            ValueError: 当响应状态码不符合预期时
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))
        logger.info(f"准备请求远程服务:{url},参数:{self.api_key}")

        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 40)
            logger.info(f"[REQUEST] POST {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info(
                f"[REQUEST] Body: {json.dumps(data, ensure_ascii=False, indent=2)}"
            )
            logger.info("=" * 40)
        async with httpx.AsyncClient() as client:

            response = await client.post(
                url, json=data, headers=headers, params=params, timeout=self.timeout
            )

            if self.log_enabled:
                try:
                    logger.info("-" * 40)
                    logger.info(
                        f"[RESPONSE] {response.status_code} {response.reason_phrase}"
                    )
                    logger.info(
                        f"[RESPONSE] Headers: {json.dumps(response.headers, ensure_ascii=False)}"
                    )

                except httpx.RequestError as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")
            return self._handler_response(response)

    async def put(
        self,
        endpoint: str,
        data: Dict[str, Any],
        user_id: UUID,
        additional_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发送PUT请求

        Args:
            endpoint: API端点路径
            data: 请求体数据
            user_id: 用户ID
            additional_params: 额外的查询参数

        Returns:
            响应数据
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))

        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 80)
            logger.info(f"[REQUEST] PUT {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info(
                f"[REQUEST] Body: {json.dumps(data, ensure_ascii=False, indent=2)}"
            )
            logger.info("=" * 80)

        async with httpx.AsyncClient() as client:
            response = await client.put(
                url, json=data, headers=headers, params=params, timeout=self.timeout
            )

            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("=" * 80)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(
                        f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}"
                    )
                    logger.info(
                        f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}"
                    )
                    logger.info("=" * 80)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")

            return self._handler_response(response)

    async def get(
        self,
        endpoint: str,
        user_id: UUID,
        additional_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发送GET请求

        Args:
            endpoint: API端点路径
            user_id: 用户ID
            additional_params: 额外的查询参数

        Returns:
            响应数据
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))

        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 80)
            logger.info(f"[REQUEST] GET {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info("=" * 80)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers=headers, params=params, timeout=self.timeout
            )

            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("=" * 80)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(
                        f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}"
                    )
                    logger.info(
                        f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}"
                    )
                    logger.info("=" * 80)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")

            return self._handler_response(response)

    async def delete(
        self,
        endpoint: str,
        user_id: UUID,
        additional_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发送DELETE请求

        Args:
            endpoint: API端点路径
            user_id: 用户ID
            additional_params: 额外的查询参数

        Returns:
            响应数据
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))

        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 80)
            logger.info(f"[REQUEST] DELETE {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info("=" * 80)

        async with httpx.AsyncClient() as client:
            response = await client.delete(
                url, headers=headers, params=params, timeout=self.timeout
            )

            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("=" * 80)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(
                        f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}"
                    )
                    logger.info(
                        f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}"
                    )
                    logger.info("=" * 80)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")

            return self._handler_response(response)


# 创建全局实例
remote_service_client = RemoteServiceClient()
