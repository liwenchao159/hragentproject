import logging
from http.client import HTTPException
from typing import Optional, Dict, Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class DifyService:
    def __init__(self) -> None:
        self.base_url = settings.DIFY_BASE_URL
        self.api_key = settings.DIFY_API_KEY
        self.user_id = settings.DIFY_USER_ID

        if not self.api_key:
            raise ValueError("DIFY_API_KEY是必需的但未配置")

    async def call_workflow_async(self, workflow_type: int, query: str, conversation_id: Optional[str] = None,
                                  additional_inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        调用Dify工作流并同步响应

        Args:
            workflow_type: 工作流类型
            query: 用户查询/提示
            conversation_id: 可选的对话ID
            additional_inputs: 额外的输入参数

        Returns:
            完整的响应数据
        """
        try:
            inputs = {"type": workflow_type}
            if additional_inputs:
                inputs.update(additional_inputs)
            request_data = {
                "inputs": inputs,
                "query": query,
                "response_mode": "blocking",
                "conversation_id": conversation_id or "",
                "user":self.user_id
            }

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            logger.info(f"调用Dify工作流类型 {workflow_type} (同步)，查询: {query[:100]}...")
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    url=f"{self.base_url}/chat-messages",
                    json=request_data,
                    headers=headers
                )
                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"Dify API错误: {response.status_code} - {error_text}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Dify API错误:{error_text}"
                    )
                return response.json()

        except httpx.TimeoutException:
            logger.error("Dify API请求超时")
            raise HTTPException(status_code=504, detail="Dify API请求超时")

        except httpx.RequestError as e:
            logger.error(f"Dify API请求错误: {str(e)}")
            raise HTTPException(status_code=503, detail=f"Dify API请求错误: {str(e)}")

        except Exception as e:
            logger.error(f"Dify工作流调用中出现意外错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")



