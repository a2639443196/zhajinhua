from openai import AsyncOpenAI, APITimeoutError
import asyncio
import json
from typing import Callable, Awaitable

# 配置文件自己添加即可
try:
    from config_local import API_BASE_URL, API_KEY
except ImportError:
    API_KEY = ""
    API_BASE_URL = ""


class LLMClient:
    def __init__(self, api_key=API_KEY, base_url=API_BASE_URL):
        self.async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )

    async def chat_stream(self, messages, model, stream_callback: Callable[[str], Awaitable[None]]) -> str:
        full_content = ""
        REQUEST_TIMEOUT_SECONDS = 35.0

        try:
            stream = await self.async_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                stream=True,
                timeout=REQUEST_TIMEOUT_SECONDS
            )

            async for chunk in stream:

                # --- (关键 BUG 修复) ---
                # 安全检查：如果 choices 列表为空 (元数据块)，则跳过
                if not chunk.choices:
                    continue
                # --- (修复结束) ---

                delta = chunk.choices[0].delta

                text_to_stream = ""

                reasoning_chunk = getattr(delta, 'reasoning_content', None) or ""
                if reasoning_chunk:
                    text_to_stream = reasoning_chunk

                content_chunk = delta.content or ""
                if content_chunk:
                    full_content += content_chunk
                    text_to_stream = content_chunk

                if text_to_stream:
                    await stream_callback(text_to_stream)

            return full_content

        except APITimeoutError as e:
            error_msg = f"LLM 思考超时 ({REQUEST_TIMEOUT_SECONDS}秒)"
            print(f"【上帝(警告)】: {model} {error_msg}")
            await stream_callback(f"\n[LLM 思考超时，强制弃牌...]\n")

            error_json_str = f'\n{{\n  "action": "FOLD", "reason": "{error_msg}", "target_name": null, "mood": "超时" \n}}'
            return error_json_str

        except Exception as e:
            error_msg = f"LLM API 调用失败: {str(e)}"
            print(f"【上帝(错误)】: LLM调用出错: {str(e)}")
            await stream_callback(error_msg)

            error_json_str = f'\n{{\n  "action": "FOLD", "reason": "{error_msg}", "target_name": null, "mood": "错误" \n}}'
            return error_json_str
