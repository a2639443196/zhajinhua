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
CREATE_PERSONA_PROMPT_PATH = BASE_DIR / "prompt/create_persona_prompt.txt"
DEFEND_PROMPT_PATH = BASE_DIR / "prompt/defend_prompt.txt"
VOTE_PROMPT_PATH = BASE_DIR / "prompt/vote_prompt.txt"


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

    # (已修改)
    async def create_persona(self,
                             stream_start_cb: Callable[[str], Awaitable[None]],
                             stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:
        """
        (已修改)
        1. 移除此处的 stream_start_cb，由控制器统一打印。
        2. 在出错时 *返回* 错误信息。
        """
        template = self._read_file(CREATE_PERSONA_PROMPT_PATH)
        if not template:
            return f"大家好，我是 {self.name}。(错误: 无法读取人设 Prompt)"

        prompt = template.format(self_name=self.name)
        messages = [{"role": "user", "content": prompt}]

        # (新) 移除了这里的 stream_start_cb(f"【上帝(赛前介绍)】: [{self.name}]: ")
        #    控制器将在收到 *返回* 的 intro_text 后再打印标题。

        try:
            full_intro = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb  # 仍然流式传输 *内容*
            )
            await stream_chunk_cb("\n")  # 确保流式传输后换行

            intro_text = full_intro.strip().replace("\n", " ")
            if not intro_text:
                return f"大家好，我是 {self.name}，很高兴认识各位。"
            return intro_text

        except Exception as e:
            # (新) *返回* 详细的错误信息，以便控制器打印。
            error_msg = f"大家好... 我是 {self.name}。(创建人设时出错: {str(e)})"
            # (我们仍然尝试流式传输错误，以防万一)
            await stream_chunk_cb(f"\n{error_msg}\n")
            return error_msg

    # (已修改)
    async def decide_action(self,
                            game_state_summary: str,
                            my_hand: str,
                            available_actions_str: str,
                            next_player_name: str,
                            my_persona: str,
                            opponent_personas: str,
                            opponent_reflections: str,
                            opponent_private_impressions_str: str,
                            observed_speech_str: str,
                            received_secret_messages: str,
                            min_raise_increment: int,
                            dealer_name: str,
                            observed_moods: str,
                            multiplier: int,
                            call_cost: int,
                            stream_start_cb: Callable[[str], Awaitable[None]],
                            stream_chunk_cb: Callable[[str], Awaitable[None]]) -> dict:
        """
        (已修改)
        将所有逻辑移入 try/except 块，确保任何失败（Regex, JSON）
        都会被内部捕获并返回一个安全的 FOLD JSON。
        """
        try:
            template = self._read_file(DECIDE_ACTION_PROMPT_PATH)
            if not template:
                raise RuntimeError("无法读取 Prompt 模板文件。")

            prompt = template.format(
                self_name=self.name,
                game_state_summary=game_state_summary,
                my_hand=my_hand,
                available_actions=available_actions_str,
                next_player_name=next_player_name,
                my_persona=my_persona,
                opponent_personas=opponent_personas,
                opponent_reflections=opponent_reflections,
                opponent_private_impressions_str=opponent_private_impressions_str,
                observed_speech_str=observed_speech_str,
                received_secret_messages=received_secret_messages,
                min_raise_increment=min_raise_increment,
                dealer_name=dealer_name,
                observed_moods=observed_moods,
                multiplier=multiplier,
                call_cost=call_cost
            )
            messages = [{"role": "user", "content": prompt}]

            await stream_start_cb(f"[{self.name} 思考中...]: ")

            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )

            # (新) 修改 Regex，使其更严格，只查找以 { 开头的 JSON
            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|\s*({[\s\S]*})', full_content)

            if json_match:
                json_str = json_match.group(1) or json_match.group(2)
                result = json.loads(json_str)

                if "action" in result and "reason" in result and "mood" in result:
                    return result

            raise ValueError(f"LLM 未返回有效的 JSON。收到内容: {full_content[:100]}...")

        except Exception as e:
            # (新) 捕获所有此函数内的错误
            error_msg = f"LLM 解析失败: {str(e)}"
            print(f"【上帝(警告)】: {self.name} 解析流式JSON失败: {str(e)}")
            await stream_chunk_cb(f"\n[LLM 解析失败: {error_msg}]")
            return {"action": "FOLD", "reason": error_msg, "target_name": None, "mood": "解析失败", "speech": None,
                    "secret_message": None}

    # (新) 被告辩护
    async def defend(self,
                     accuser_name: str,
                     partner_name: str,
                     evidence_log: str,
                     stream_start_cb: Callable[[str], Awaitable[None]],
                     stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:

        template = self._read_file(DEFEND_PROMPT_PATH)
        if not template:
            return "我无话可说。"

        prompt = template.format(
            self_name=self.name,
            accuser_name=accuser_name,
            partner_name=partner_name,
            evidence_log=evidence_log
        )
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"【上帝(被告辩护)】: [{self.name}]: ")
        try:
            full_defense = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )
            await stream_chunk_cb("\n")
            return full_defense.strip().replace("\n", " ")
        except Exception as e:
            await stream_chunk_cb(f"\n辩护时出错: {str(e)}\n")
            return f"辩护时出错: {str(e)}"

    # (新) 陪审团投票
    async def vote(self,
                   accuser_name: str,
                   target_name_1: str,
                   target_name_2: str,
                   evidence_log: str,
                   defense_speech_1: str,
                   defense_speech_2: str,
                   stream_start_cb: Callable[[str], Awaitable[None]],
                   stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:

        template = self._read_file(VOTE_PROMPT_PATH)
        if not template:
            return "NOT_GUILTY"  # 默认

        prompt = template.format(
            self_name=self.name,
            accuser_name=accuser_name,
            target_name_1=target_name_1,
            target_name_2=target_name_2,
            evidence_log=evidence_log,
            defense_speech_1=defense_speech_1,
            defense_speech_2=defense_speech_2
        )
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"【上帝(陪审团投票)】: [{self.name} 正在秘密投票...]: ")

        try:
            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=lambda s: asyncio.sleep(0.001)
            )

            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|\s*({[\s\S]*})', full_content)
            if json_match:
                json_str = json_match.group(1) or json_match.group(2)
                result = json.loads(json_str)
                vote = result.get("vote", "NOT_GUILTY").upper()
                if vote == "GUILTY":
                    await stream_chunk_cb(" (已投: 有罪)\n")
                    return "GUILTY"

            await stream_chunk_cb(" (已投: 无罪)\n")
            return "NOT_GUILTY"

        except Exception as e:
            await stream_chunk_cb(f"\n投票时出错: {str(e)} (自动投: 无罪)\n")
            return "NOT_GUILTY"

    # (已修改)
    async def reflect(self,
                      round_history: str,
                      round_result: str,
                      current_impressions_json: str,
                      stream_start_cb: Callable[[str], Awaitable[None]],
                      stream_chunk_cb: Callable[[str], Awaitable[None]]) -> (str, dict):
        """
        (已修改)
        将 JSON 解析移入 try 块，确保返回安全
        """
        try:
            template = self._read_file(REFLECT_PROMPT_PATH)
            if not template:
                raise RuntimeError("无法读取复盘 Prompt")

            prompt = template.format(
                self_name=self.name,
                round_history=round_history,
                round_result=round_result,
                current_impressions_json=current_impressions_json
            )
            messages = [{"role": "user", "content": prompt}]

            await stream_start_cb(f"【上帝(复盘中)】: [{self.name}]: ")

            await stream_chunk_cb("正在更新情报...")
            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=lambda s: asyncio.sleep(0.001)
            )

            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|\s*({[\s\S]*})', full_content)
            if not json_match:
                raise ValueError(f"LLM 未返回有效的复盘 JSON。收到: {full_content[:100]}...")

            json_str = json_match.group(1) or json_match.group(2)
            result = json.loads(json_str)

            public_reflection = result.get("public_reflection", "...")
            private_impressions = result.get("private_impressions", {})

            await stream_chunk_cb(f" (发言): {public_reflection}\n")
            return public_reflection, private_impressions

        except Exception as e:
            error_msg = f"复盘时出错: {str(e)}"
            await stream_chunk_cb(f"\n{error_msg}\n")
            return f"({error_msg})", {}
