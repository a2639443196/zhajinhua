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
        self.experience: float = 0.0  # (新) 记录牌局经验
        self.persona_tags: set[str] = set()
        self.persona_text: str = ""
        self.play_history: List[str] = []
        self.current_pressure: float = 0.0

    # --- (新) 经验系统辅助常量 ---
    _EXPERIENCE_KEYWORDS: Dict[str, float] = {
        "老手": 18.0,
        "资深": 16.0,
        "职业": 15.0,
        "冠军": 14.0,
        "高手": 12.0,
        "宗师": 20.0,
        "冷静": 6.0,
        "沉着": 6.0,
        "老练": 10.0,
        "经验": 8.0,
        "从容": 5.0,
        "算计": 7.0,
        "心理战": 7.5,
    }
    _AGGRESSIVE_KEYWORDS = {"激进", "进攻", "攻击", "冒险", "豪赌"}
    _CAUTIOUS_KEYWORDS = {"稳健", "谨慎", "保守", "冷静", "理性"}
    _DECEPTIVE_KEYWORDS = {"伪装", "隐藏", "掩饰", "迷惑", "诈唬"}

    def register_persona(self, persona_text: str) -> None:
        """(新) 根据人设初始化经验标签。"""
        self.persona_text = persona_text or ""
        base_score = 12.0  # 默认给一个基础经验，避免纯 0
        lowered = self.persona_text.lower()
        for keyword, value in self._EXPERIENCE_KEYWORDS.items():
            if keyword in persona_text:
                base_score += value
        # 检查英文关键字 (以防 prompt 中有英文描述)
        for keyword, value in self._EXPERIENCE_KEYWORDS.items():
            if keyword.lower() in lowered:
                base_score += value * 0.6

        self.persona_tags.clear()
        if any(k in persona_text for k in self._AGGRESSIVE_KEYWORDS):
            self.persona_tags.add("aggressive")
        if any(k in persona_text for k in self._CAUTIOUS_KEYWORDS):
            self.persona_tags.add("cautious")
        if any(k in persona_text for k in self._DECEPTIVE_KEYWORDS):
            self.persona_tags.add("deceptive")

        self.experience = max(self.experience, min(base_score, 80.0))

    def update_pressure_snapshot(self, chips: int, call_cost: int) -> None:
        """(新) 根据筹码与成本估算压力值 (0~1)。"""
        if chips <= 0:
            pressure = 1.0
        elif call_cost <= 0:
            pressure = 0.1
        else:
            ratio = call_cost / max(chips, 1)
            pressure = min(1.0, ratio * 0.8 + (0.2 if chips < call_cost * 2 else 0.0))
        # 经验越高越能压制压力
        mitigation = min(0.35, self.experience / 200.0)
        self.current_pressure = max(0.0, min(1.0, pressure * (1.0 - mitigation)))

    def get_pressure_descriptor(self) -> str:
        """(新) 用中文描述当前压力。"""
        if self.current_pressure >= 0.8:
            return "濒临崩溃"
        if self.current_pressure >= 0.6:
            return "压力山大"
        if self.current_pressure >= 0.4:
            return "紧张"
        if self.current_pressure >= 0.2:
            return "尚算从容"
        return "悠然自得"

    def get_experience_level(self) -> str:
        """(新) 根据经验值给出等级。"""
        if self.experience >= 90:
            return "宗师"
        if self.experience >= 60:
            return "高手"
        if self.experience >= 30:
            return "熟练"
        return "新手"

    def get_experience_summary(self) -> str:
        return f"{self.get_experience_level()} (经验值: {self.experience:.1f})"

    def get_mood_leak_probability(self) -> float:
        """(新) 依据经验与压力决定情绪泄露概率。"""
        base = 0.33 + (self.current_pressure - 0.5) * 0.22
        if self.experience >= 90:
            base *= 0.45
        elif self.experience >= 60:
            base *= 0.6
        elif self.experience <= 15:
            base *= 1.25
        return max(0.05, min(0.75, base))

    def update_experience_after_action(self, action_json: dict, cheat_context: Optional[dict] = None) -> None:
        """(新) 根据动作和玩法提升经验。"""
        if not isinstance(action_json, dict):
            return
        action_name = str(action_json.get("action", "")).upper()
        if not action_name:
            return

        gain = 1.0
        if action_name in {"RAISE", "COMPARE"}:
            gain += 1.5
        elif action_name == "CALL":
            gain += 0.8
        elif action_name == "FOLD":
            gain += 0.3

        if "aggressive" in self.persona_tags and action_name in {"RAISE", "COMPARE", "ALL_IN_SHOWDOWN"}:
            gain += 0.8
        if "cautious" in self.persona_tags and action_name in {"FOLD", "CALL"}:
            gain += 0.6
        if "deceptive" in self.persona_tags and action_json.get("speech"):
            gain += 0.4

        mood = action_json.get("mood") or ""
        if isinstance(mood, str) and any(k in mood for k in ("紧张", "恐惧", "崩溃")):
            gain += 0.2  # 在压力中累积经验

        self.play_history.append(action_name)

        if cheat_context and cheat_context.get("attempted"):
            if cheat_context.get("success"):
                gain += 2.5
            else:
                gain -= 1.2

        self.experience = max(0.0, min(120.0, self.experience + gain))

    def update_experience_from_cheat(self, success: bool, cheat_type: str, context: Optional[dict] = None) -> None:
        """(新) 作弊结果反馈经验。"""
        delta = 4.0 if success else -6.0
        if not success and self.experience < 40:
            delta -= 2.0  # 新手失败打击更大
        if cheat_type.upper() == "SWAP_SUIT" and success:
            delta += 1.5
        self.experience = max(0.0, min(130.0, self.experience + delta))

    def update_experience_from_reflection(self, reflection_text: str, private_notes: dict) -> None:
        """(新) 复盘也能提升经验。"""
        gain = 0.5
        if reflection_text:
            length_factor = min(len(reflection_text) / 120.0, 2.0)
            gain += length_factor
        if private_notes:
            gain += min(len(private_notes) * 0.6, 3.0)
        self.experience = max(0.0, min(140.0, self.experience + gain))

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

            # (新) 尝试根据自然语言描述推断动作，避免直接判定失败
            inferred_action = self._infer_action_from_text(full_content)
            if inferred_action:
                await stream_chunk_cb("\n[系统提示: 未检测到 JSON，已根据描述推断动作。]")
                return inferred_action

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

    def _infer_action_from_text(self, text: str) -> Optional[Dict]:
        """(新) 当 LLM 未输出合法 JSON 时，根据文本内容推测玩家意图。"""
        if not text:
            return None

        normalized = text.strip()
        if not normalized:
            return None

        # 定义关键字映射
        action_keywords = {
            "ALL_IN_SHOWDOWN": ["ALL_IN_SHOWDOWN", "ALL IN", "ALL-IN", "ALLIN", "全下", "孤注一掷", "梭哈"],
            "CALL": ["CALL", "跟注", "跟上", "跟到底"],
            "FOLD": ["FOLD", "弃牌", "放弃", "扔牌"],
            "LOOK": ["LOOK", "看牌", "先看牌"],
            "CHECK": ["CHECK", "过牌", "过一下"],
        }

        lowered = normalized.lower()

        detected_action: Optional[str] = None
        detected_phrase: Optional[str] = None
        detected_reason: Optional[str] = None
        detected_mood: Optional[str] = None
        detected_speech: Optional[str] = None

        # (新) 优先尝试解析形如 “动作: XXX” 的结构化描述
        structured_patterns = {
            "action": [r"(?:动作|决定|选择|行动|move|action)[:：]\s*([^\n。！？]+)"],
            "reason": [r"(?:理由|原因|解析|说明)[:：]\s*([^\n。！？]+)"],
            "mood": [r"(?:情绪|心情|状态|Mood)[:：]\s*([^\n。！？]+)"],
            "speech": [r"(?:发言|话语|台词|宣言|说)[:：]\s*([^\n。！？]+)"],
        }

        structured_info: Dict[str, str] = {}
        for field, patterns in structured_patterns.items():
            for pattern in patterns:
                match = re.search(pattern, normalized, re.IGNORECASE)
                if match:
                    structured_info[field] = match.group(1).strip()
                    break

        if "action" in structured_info:
            action_value = structured_info["action"].lower()
            for action, keywords in action_keywords.items():
                for keyword in keywords:
                    if keyword.lower() in action_value:
                        detected_action = action
                        detected_phrase = structured_info["action"]
                        break
                if detected_action:
                    break

        detected_reason = structured_info.get("reason")
        detected_mood = structured_info.get("mood")
        detected_speech = structured_info.get("speech")

        sentences = re.split(r"[。！？\n]", normalized)
        if not detected_action:
            for sentence in sentences:
                sentence_stripped = sentence.strip()
                if not sentence_stripped:
                    continue
                sentence_lower = sentence_stripped.lower()
                for action, keywords in action_keywords.items():
                    for keyword in keywords:
                        kw_lower = keyword.lower()
                        if kw_lower in sentence_lower:
                            detected_action = action
                            detected_phrase = sentence_stripped
                            break
                    if detected_action:
                        break
                if detected_action:
                    break

        if not detected_action:
            # 尝试直接从全文中搜索
            for action, keywords in action_keywords.items():
                for keyword in keywords:
                    if keyword.lower() in lowered:
                        detected_action = action
                        detected_phrase = keyword
                        break
                if detected_action:
                    break

        if not detected_action:
            return None

        # 仅当动作不需要额外信息时才返回，避免产生非法决策
        if detected_action in {"RAISE", "COMPARE", "ACCUSE"}:
            return None

        if not detected_reason:
            # (新) 如果未通过结构化信息获得理由，则尝试使用包含动作的语句
            detected_reason = detected_phrase

        mood_keywords = [
            "自信", "紧张", "沮丧", "愤怒", "兴奋", "平静", "淡定", "恐惧", "绝望", "期待", "冷静", "忐忑", "激动", "焦虑"
        ]
        if not detected_mood:
            for mood_word in mood_keywords:
                if mood_word in normalized:
                    detected_mood = mood_word
                    break

        if not detected_mood:
            detected_mood = self.get_pressure_descriptor()

        if detected_reason:
            detected_reason = detected_reason.strip()
        else:
            detected_reason = detected_phrase or detected_action

        action_display = {
            "ALL_IN_SHOWDOWN": "全下",
            "CALL": "跟注",
            "FOLD": "弃牌",
            "LOOK": "看牌",
            "CHECK": "过牌",
        }
        action_cn = action_display.get(detected_action, detected_action)
        phrase_for_reason = detected_reason or detected_phrase or detected_action
        reason = f"LLM 未输出 JSON，依据描述“{phrase_for_reason}”推测执行 {action_cn}。"

        inferred_result = {
            "action": detected_action,
            "amount": None,
            "target_name": None,
            "target_name_2": None,
            "reason": reason,
            "mood": detected_mood,
            "speech": detected_speech,
            "secret_message": None,
            "cheat_move": None,
        }

        return inferred_result

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
