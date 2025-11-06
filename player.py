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
# (新)
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

    # (新) 人设创建方法
    async def create_persona(self,
                             stream_start_cb: Callable[[str], Awaitable[None]],
                             stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:
        """
        在游戏开始时调用，让 LLM 创建自己的社会人设。
        """
        template = self._read_file(CREATE_PERSONA_PROMPT_PATH)
        if not template:
            print(f"【上帝(警告)】: {self.name} 无法读取人设文件，跳过。")
            return f"大家好，我是 {self.name}。"

        prompt = template.format(self_name=self.name)
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"【上帝(赛前介绍)】: [{self.name}]: ")
        try:
            full_intro = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )
            await stream_chunk_cb("\n")
            intro_text = full_intro.strip().replace("\n", " ")
            if not intro_text:  # 处理空回复
                return f"大家好，我是 {self.name}，很高兴认识各位。"
            return intro_text

        except Exception as e:
            await stream_chunk_cb(f"\n创建人设时出错: {str(e)}\n")
            return f"大家好... 我是 {self.name}，我系统出了点问题。"

    # (已修改) 接收所有人设/笔记/发言/密信参数
    async def decide_action(self,
                            game_state_summary: str,
                            my_hand: str,
                            available_actions_str: str,
                            next_player_name: str,
                            # (旧)
                            my_persona: str,
                            opponent_personas: str,
                            opponent_reflections: str,
                            opponent_private_impressions_str: str,
                            observed_speech_str: str,
                            # (新)
                            received_secret_messages: str,
                            # (旧)
                            min_raise_increment: int,
                            dealer_name: str,
                            observed_moods: str,
                            multiplier: int,
                            call_cost: int,
                            stream_start_cb: Callable[[str], Awaitable[None]],
                            stream_chunk_cb: Callable[[str], Awaitable[None]]) -> dict:
        """
        (已修改)
        接收所有情报（包括密信），做出决策。
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
            # (旧)
            my_persona=my_persona,
            opponent_personas=opponent_personas,
            opponent_reflections=opponent_reflections,
            opponent_private_impressions_str=opponent_private_impressions_str,
            observed_speech_str=observed_speech_str,
            # (新)
            received_secret_messages=received_secret_messages,
            # (旧)
            min_raise_increment=min_raise_increment,
            dealer_name=dealer_name,
            observed_moods=observed_moods,
            multiplier=multiplier,
            call_cost=call_cost
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
            # (新) 增加默认的 secret_message: None
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
            # 投票不应流式传输
            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=lambda s: asyncio.sleep(0.001)
            )

            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|({[\s\S]*})', full_content)
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

    # (已修改) 升级为情报更新
    async def reflect(self,
                      round_history: str,
                      round_result: str,
                      # (新) 传入当前的私有笔记
                      current_impressions_json: str,
                      stream_start_cb: Callable[[str], Awaitable[None]],
                      stream_chunk_cb: Callable[[str], Awaitable[None]]) -> (str, dict):
        """
        (已修改)
        让 LLM 同时返回“公开垃圾话”和“私有笔记更新”
        返回: (public_reflection, private_impressions_dict)
        """
        template = self._read_file(REFLECT_PROMPT_PATH)

        if not template:
            print(f"【上帝(警告)】: {self.name} 无法读取复盘文件，跳过复盘。")
            return "...", {}

        prompt = template.format(
            self_name=self.name,
            round_history=round_history,
            round_result=round_result,
            # (新)
            current_impressions_json=current_impressions_json
        )
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"【上帝(复盘中)】: [{self.name}]: ")
        try:
            # 复盘现在是一个 JSON 块，不适合流式传输
            # 我们可以模拟一个 "思考中..." 的流
            await stream_chunk_cb("正在更新情报...")

            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                # 使用一个虚拟的回调，我们稍后手动广播公开部分
                stream_callback=lambda s: asyncio.sleep(0.001)
            )

            # 解析 JSON
            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|({[\s\S]*})', full_content)
            if not json_match:
                raise ValueError("LLM did not return valid JSON for reflection.")

            json_str = json_match.group(1) or json_match.group(2)
            result = json.loads(json_str)

            public_reflection = result.get("public_reflection", "...")
            private_impressions = result.get("private_impressions", {})

            # 手动广播公开的垃圾话
            await stream_chunk_cb(f" (发言): {public_reflection}\n")

            # 返回公开的发言 和 私有的笔记
            return public_reflection, private_impressions

        except Exception as e:
            error_msg = f"复盘时出错: {str(e)}"
            await stream_chunk_cb(f"\n{error_msg}\n")
            return f"({error_msg})", {}
