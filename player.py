import json
import re
import time
import asyncio
import ast
from typing import List, Dict, Callable, Awaitable, Optional
from llm_client import LLMClient
import pathlib
import traceback  # (新) 导入 traceback

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

    def _extract_json_candidates(self, text: str) -> List[str]:
        """从给定文本中提取所有可能的 JSON 片段。"""
        candidates: List[str] = []
        stack: List[str] = []
        start_idx: Optional[int] = None

        for idx, ch in enumerate(text):
            if ch == '{':
                if not stack:
                    start_idx = idx
                stack.append(ch)
            elif ch == '}':
                if stack:
                    stack.pop()
                    if not stack and start_idx is not None:
                        candidates.append(text[start_idx: idx + 1])
                        start_idx = None

        return candidates

    def _safe_parse_json(self, candidate: str) -> Optional[Dict]:
        """尽可能地将字符串解析为 JSON 对象。"""
        candidate = candidate.strip()
        if not candidate:
            return None

        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        last_brace = candidate.rfind('}')
        if last_brace != -1 and last_brace < len(candidate) - 1:
            trimmed = candidate[:last_brace + 1]
            try:
                return json.loads(trimmed)
            except json.JSONDecodeError:
                candidate = trimmed

        python_like = re.sub(r'\bnull\b', 'None', candidate)
        python_like = re.sub(r'\btrue\b', 'True', python_like)
        python_like = re.sub(r'\bfalse\b', 'False', python_like)

        try:
            data = ast.literal_eval(python_like)
        except Exception:
            return None

        if isinstance(data, dict):
            return data
        return None

    def _parse_first_valid_json(self, text: str) -> Optional[Dict]:
        for candidate in self._extract_json_candidates(text):
            parsed = self._safe_parse_json(candidate)
            if isinstance(parsed, dict):
                return parsed
        return None

    # (已修改)
    async def create_persona(self,
                             stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:
        """
        (已修改)
        1. 只接收 stream_chunk_cb。
        2. Controller 负责 stream_start 和 换行。
        3. 在出错时 *返回* 错误信息。
        """
        template = self._read_file(CREATE_PERSONA_PROMPT_PATH)
        if not template:
            return f"(错误: 无法读取人设 Prompt)"

        prompt = template.format(self_name=self.name)
        messages = [{"role": "user", "content": prompt}]

        try:
            full_intro = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb  # 只传递 chunk
            )

            intro_text = full_intro.strip().replace("\n", " ")
            if not intro_text:
                return f"大家好，我是 {self.name}，很高兴认识各位。"
            return intro_text

        except Exception as e:
            error_msg = f"(创建人设时出错: {str(e)})"
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
        极大增强 except 块的日志记录能力。
        """
        full_content_debug = ""  # (新) 用于在出错时记录
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
            full_content_debug = full_content  # (新) 存储

            result = self._parse_first_valid_json(full_content)

            if result and "action" in result and "reason" in result and "mood" in result:
                return result

            raise ValueError(f"LLM 未返回有效的 JSON。")

        except Exception as e:
            # --- (新) 增强的错误报告 ---
            tb_str = traceback.format_exc()  # 获取完整的堆栈跟踪

            # 准备一个非常详细的错误信息
            error_msg = f"""LLM 解析失败:

            Exception Type: {type(e)}
            Exception: {str(e)}

            Full Content Received:
            ---
            {full_content_debug[:200]}...
            ---

            Traceback:
            {tb_str}
            """

            # (新) 替换换行符，以便在 JSON 和日志中安全传输
            error_msg_oneline = error_msg.replace("\n", " || ")

            print(f"【上帝(警告)】: {self.name} 解析流式JSON失败: {error_msg_oneline}")
            await stream_chunk_cb(f"\n[LLM 解析失败: {error_msg_oneline}]")

            return {"action": "FOLD", "reason": error_msg_oneline, "target_name": None, "mood": "解析失败", "speech": None,
                    "secret_message": None}
            # --- 修复结束 ---

    # (已修改)
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

    # (已修改)
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
            return "NOT_GUILTY"

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
        增强 reflect 的错误处理
        """
        full_content_debug = ""  # (新)
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
            full_content_debug = full_content  # (新)

            result = self._parse_first_valid_json(full_content)
            if not result:
                raise ValueError(f"LLM 未返回有效的复盘 JSON。")

            public_reflection = result.get("public_reflection", "...")
            private_impressions = result.get("private_impressions", {})

            await stream_chunk_cb(f" (发言): {public_reflection}\n")
            return public_reflection, private_impressions

        except Exception as e:
            # (新) 增强的错误报告
            tb_str = traceback.format_exc()
            tb_str_oneline = tb_str.replace("\n", " || ")
            error_msg = (
                "复盘时出错: "
                f"{type(e)}: {str(e)} || Content: {full_content_debug[:100]}... || Traceback: {tb_str_oneline}"
            )
            await stream_chunk_cb(f"\n{error_msg}\n")
            return f"({error_msg})", {}
