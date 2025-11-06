import json
import re
import time
import asyncio
from typing import List, Dict, Callable, Awaitable
from llm_client import LLMClient
import pathlib

BASE_DIR = pathlib.Path(__file__).parent.resolve()
DECIDE_ACTION_PROMPT_PATH = BASE_DIR / "prompt/decide_action_prompt.txt"
REFLECT_PROMPT_PATH = BASE_DIR / "prompt/reflect_prompt_template.txt"


class Player:
    def __init__(self, name: str, model_name: str):
        self.name = name
        self.model_name = model_name
        self.llm_client = LLMClient()
        self.alive = True

    def _read_file(self, filepath: str) -> str:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            print(f"【上帝(系统)】: 读取文件 {filepath} 失败: {str(e)}")
            return ""

    async def decide_action(self,
                            game_state_summary: str,
                            my_hand: str,
                            available_actions_str: str,
                            next_player_name: str,
                            opponent_reflections: str,
                            min_raise_increment: int,
                            dealer_name: str,
                            observed_moods: str,
                            multiplier: int,  # <-- (新) 1. 接收倍率
                            call_cost: int,  # <-- (新) 2. 接收跟注成本
                            stream_start_cb: Callable[[str], Awaitable[None]],
                            stream_chunk_cb: Callable[[str], Awaitable[None]]) -> dict:
        """
        (已修改)
        1. 增加 observed_moods。
        2. (新) 增加 multiplier, call_cost。
        """
        template = self._read_file(DECIDE_ACTION_PROMPT_PATH)

        if not template:
            raise RuntimeError("无法读取 Prompt 模板文件。")

        prompt = template.format(
            self_name=self.name,
            game_state_summary=game_state_summary,
            my_hand=my_hand,
            available_actions=available_actions_str,
            next_player_name=next_player_name,
            opponent_reflections=opponent_reflections,
            min_raise_increment=min_raise_increment,
            dealer_name=dealer_name,
            observed_moods=observed_moods,
            multiplier=multiplier,  # <-- (新) 3. 传入 Prompt
            call_cost=call_cost  # <-- (新) 4. 传入 Prompt
        )
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"[{self.name} 思考中...]: ")

        try:
            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )

            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|({[\s\S]*})', full_content)

            if json_match:
                json_str = json_match.group(1) or json_match.group(2)
                result = json.loads(json_str)

                if "action" in result and "reason" in result and "mood" in result:
                    return result

            raise ValueError("LLM did not return valid JSON with 'action', 'reason', and 'mood'.")

        except Exception as e:
            error_msg = f"LLM 解析失败: {str(e)}"
            print(f"【上帝(警告)】: {self.name} 解析流式JSON失败: {str(e)}")
            await stream_chunk_cb(f"\n[LLM 解析失败: {error_msg}]")
            return {"action": "FOLD", "reason": error_msg, "target_name": None, "mood": "解析失败"}

    # (reflect 方法 - 无修改)
    async def reflect(self,
                      round_history: str,
                      round_result: str,
                      stream_start_cb: Callable[[str], Awaitable[None]],
                      stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:

        template = self._read_file(REFLECT_PROMPT_PATH)

        if not template:
            print(f"【上帝(警告)】: {self.name} 无法读取复盘文件，跳过复盘。")
            return "..."

        prompt = template.format(
            self_name=self.name,
            round_history=round_history,
            round_result=round_result
        )
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"【上帝(人设发言)】: [{self.name}]: ")
        try:
            full_reflection = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )
            await stream_chunk_cb("\n")
            return full_reflection.strip().replace("\n", " ")

        except Exception as e:
            await stream_chunk_cb(f"\n复盘时出错: {str(e)}\n")
            return f"复盘时出错: {str(e)}"
