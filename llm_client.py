from openai import AsyncOpenAI, APITimeoutError
import asyncio
import json
from typing import Callable, Awaitable

# ！！重要！！
# ！！请将此处替换为你的 API 终端地址和 Key ！！
API_BASE_URL = "http://116.196.117.30:3000/v1"  # 示例地址
API_KEY = "sk-Ea6XIuVezjgVfC3o01LsMsmwKQWp5x29i06zUayLu2n6tjWo"  # 示例 Key


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
                delta = chunk.choices[0].delta

                text_to_stream = ""

                reasoning_chunk = delta.reasoning_content or ""
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

            # (新) 修正：返回一个包含 JSON 的*字符串*，而不是 JSON 对象
            # 这样 player.py 中的 re.search 才能正确捕获它
            error_json_str = f'\n{{\n  "action": "FOLD", "reason": "{error_msg}", "target_name": null, "mood": "超时" \n}}'
            return error_json_str

        except Exception as e:
            error_msg = f"LLM API 调用失败: {str(e)}"
            print(f"【上帝(错误)】: LLM调用出错: {str(e)}")
            await stream_callback(error_msg)

            error_json_str = f'\n{{\n  "action": "FOLD", "reason": "{error_msg}", "target_name": null, "mood": "错误" \n}}'
            return error_json_str