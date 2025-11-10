import json
import re
import time
import asyncio
import ast
from typing import List, Dict, Callable, Awaitable, Optional, Tuple
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
        self.cheat_attempts: int = 0
        self.cheat_success: int = 0
        self.mindgame_moves: int = 0

        # [新] 道具系统：背包内存放玩家已经获得的道具ID
        self.inventory: List[str] = []

        # [新] 贷款系统：记录未清贷款的到期手数与金额
        self.loan_data: Dict[str, int] = {}

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

        self.experience = max(self.experience, base_score)

    def update_pressure_snapshot(self, chips: int, call_cost: int) -> None:
        """(新) 根据筹码与成本估算压力值 (0~1)。"""
        if chips <= 0:
            pressure = 1.0
        else:
            ratio = call_cost / max(chips, 1)
            pressure = min(1.0, ratio * 0.85)

        low_stack_factor = 0.0
        if chips < 300:
            deficit_ratio = (300 - max(chips, 0)) / 300.0
            low_stack_factor = 0.35 + deficit_ratio * 0.45
        elif chips < call_cost * 2 and call_cost > 0:
            low_stack_factor = max(low_stack_factor, 0.15)

        if call_cost <= 0 and chips > 0:
            pressure = max(pressure, 0.12)

        pressure = min(1.0, pressure + low_stack_factor)

        mitigation = min(0.4, self.experience / 220.0)
        pressure *= (1.0 - mitigation)

        pressure += min(0.25, self.current_pressure * 0.3)

        self.current_pressure = max(0.0, min(1.0, pressure))

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
        if self.cheat_success >= 4 and self.cheat_success >= max(1, self.cheat_attempts // 2):
            return "千王"
        if self.mindgame_moves >= 6 and self.experience >= 45:
            return "心理学大师"
        if self.experience >= 140:
            return "宗师"
        if self.experience >= 90:
            return "大师"
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
        # (调整) 轻微提升整体泄露概率，以反映更高的紧张与侦测环境
        base += 0.05
        if self.experience >= 90:
            base *= 0.45
        elif self.experience >= 60:
            base *= 0.6
        elif self.experience <= 15:
            base *= 1.25
        return max(0.05, min(0.75, base))

    def update_experience_after_action(
            self,
            action_json: dict,
            cheat_context: Optional[dict] = None,
            call_cost: int = 0,
            current_pot: int = 0
    ) -> None:
        """(新 V2) 根据动作、风险比例和心理战提升经验。"""
        if not isinstance(action_json, dict):
            return

        action_name = str(action_json.get("action", "")).upper()
        if not action_name:
            return

        base_gain = 0.2  # 基础参与经验

        # 维度1：动作风险
        if action_name == "RAISE":
            # 加注：收益与加注额相对于底池的比例挂钩
            try:
                raise_amount = int(action_json.get("amount", 0))
            except (ValueError, TypeError):
                raise_amount = 0

            # (跟注成本 + (加注增量 * 倍率)) - 这太复杂，我们只看增量
            # 我们用 call_cost (它代表了当前下注水平) 作为基准
            risk_ratio = min((raise_amount + call_cost) / max(current_pot, 1), 2.0)
            base_gain += 1.0 + (risk_ratio * 1.5)  # 基础 1.0 + 风险比例加成

        elif action_name == "COMPARE":
            # 比牌：中等风险
            base_gain += 1.2

        elif action_name == "CALL":
            # 跟注：收益与跟注额相对于底池的比例挂钩
            risk_ratio = min(call_cost / max(current_pot, 1), 1.0)
            base_gain += 0.5 + (risk_ratio * 1.0)  # 基础 0.5 + 风险比例加成

        elif action_name == "FOLD":
            # 弃牌：低收益
            base_gain += 0.3

        # 维度2：人设加成 (贯彻人设)
        if "aggressive" in self.persona_tags and action_name in {"RAISE", "COMPARE", "ALL_IN_SHOWDOWN"}:
            base_gain += 0.5
        if "cautious" in self.persona_tags and action_name in {"FOLD", "CALL"}:
            base_gain += 0.4
        if "deceptive" in self.persona_tags and action_json.get("speech"):
            base_gain += 0.6  # 贯彻欺诈人设并发言
            self.mindgame_moves += 1

        # 维度3：作弊 (高风险/回报，这由另一个函数处理)
        # cheat_context 仅用于标记，实际的作弊经验由 update_experience_from_cheat 处理

        # 维度4：社交 (秘密消息)
        secret_payload = action_json.get("secret_message")
        if isinstance(secret_payload, dict) and secret_payload.get("message"):
            base_gain += 1.0  # 社交/密谋 额外加成
            self.mindgame_moves += 2

        self.experience = max(0.0, self.experience + base_gain)

    def update_experience_from_cheat(self, success: bool, cheat_type: str, context: Optional[dict] = None) -> None:
        """(新 V3) 作弊结果反馈经验，包含消耗和次数奖励。"""
        self.cheat_attempts += 1  # 无论成败，总次数+1

        if success:
            self.cheat_success += 1

            # 1. [您的要求 3] 成功作弊的固定消耗
            # 无论如何，成功作弊都会消耗 8.0 经验值
            CHEAT_SUCCESS_COST = 8.0

            # 2. [您的要求 2] 成功作弊的收益 (基础 + 类型)
            base_gain = 5.0  # 基础成功收益

            # (保留 V2 逻辑：不同类型收益不同)
            if cheat_type.upper() == "SWAP_RANK":
                base_gain += 2.0  # 换点数收益更高
            elif cheat_type.upper() == "SWAP_SUIT":
                base_gain += 1.5

            # [您的要求 2] 次数越多，成功收益越高 (奖励"千王")
            # (self.cheat_success 是成功次数)
            # 每次成功额外 +0.75 经验，封顶 10.0
            frequency_bonus = min(self.cheat_success * 0.75, 10.0)

            # 总 delta = (收益 + 次数奖励) - 消耗
            delta = (base_gain + frequency_bonus) - CHEAT_SUCCESS_COST

        else:
            # 3. 失败的惩罚 (逻辑不变)
            delta = -6.0  # 基础失败惩罚
            if self.experience < 40:
                delta -= 2.0  # 新手失败打击更大
            if context and context.get("detected"):
                delta -= 2.5  # 被当场抓住的额外惩罚

        self.experience = max(0.0, self.experience + delta)

    def update_experience_from_win(self, pot_at_showdown: int):
        """
        (新 V2) 获胜者专属奖励。
        奖励 = 基础 5.0 + 10% 的最终底池 (上限 20)
        """
        if pot_at_showdown <= 0:
            return

        # 算法：基础分 5.0
        base_gain = 5.0

        # 比例算法：奖励 10% 的底池大小，设置一个合理的上限
        pot_bonus = min(pot_at_showdown * 0.1, 20.0)

        total_gain = base_gain + pot_bonus
        self.experience = max(0.0, self.experience + total_gain)

    def update_experience_from_reflection(self, reflection_text: str, private_notes: dict) -> None:
        """(新) 复盘也能提升经验。"""
        gain = 0.5
        if reflection_text:
            length_factor = min(len(reflection_text) / 120.0, 2.0)
            gain += length_factor
        if private_notes:
            gain += min(len(private_notes) * 0.6, 3.0)
        self.experience = max(0.0, self.experience + gain)

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
        # [健壮性修复]：改为返回 *最后* 一个有效的 JSON，以允许 LLM 进行自我修正。
        last_valid_json: Optional[Dict] = None
        for candidate in self._extract_json_candidates(text):
            parsed = self._safe_parse_json(candidate)
            if isinstance(parsed, dict):
                last_valid_json = parsed  # 持续覆盖，直到最后一个

        return last_valid_json  # 返回最后一个找到的 JSON

        # (已修改)

    async def create_persona(self,
                             persona_prompt_template: str,
                             used_aliases: List[str],
                             stream_chunk_cb: Callable[[str], Awaitable[None]]) -> Tuple[str, str | None]:
        """
        (已修改)
        接收已使用的代号列表，直接返回完整的 intro_text，并使用 self.name 作为默认 alias。
        """
        template = persona_prompt_template
        if not template:
            return f"(错误: 无法读取人设 Prompt)", None

        used_aliases_str = "\n".join(used_aliases) if used_aliases else "无"

        # 📌 [修复] 传递已使用的完整人设文本，让 AI 自己去理解和解析避免重复
        prompt = template.format(self_name=self.name, used_aliases_str=used_aliases_str)
        messages = [{"role": "user", "content": prompt}]

        try:
            full_intro = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )

            intro_text = full_intro.strip().replace("\n", " ")
            if not intro_text:
                return f"大家好，我是 {self.name}，很高兴认识各位。", None

            # 📌 [简化逻辑] 直接使用玩家名作为 Alias。让 AI 自己去理解避免重复。
            # AI 的任务现在是：确保它输出的 "代号/姓名" 在 used_aliases_str 中没有出现。
            alias = self.name

            return intro_text, alias

        except Exception as e:
            error_msg = f"(创建人设时出错: {str(e)})"
            return error_msg, None

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
                            player_inventory: str,
                            field_item_intel: str,  # (新) 添加
                            min_raise_increment: int,
                            dealer_name: str,
                            observed_moods: str,
                            multiplier: int,
                            call_cost: int,
                            table_seating_str: str,
                            opponent_reference_str: str,
                            public_event_log: str, # (新) 添加
                    prompt_template: str, # (新) 添加
                    stream_start_cb: Callable[[str], Awaitable[None]],
                    stream_chunk_cb: Callable[[str], Awaitable[None]]) -> dict:
        """
        (已修改)
        极大增强 except 块的日志记录能力。
        """
        full_content_debug = ""  # (新) 用于在出错时记录
        try:
            # template = self._read_file(DECIDE_ACTION_PROMPT_PATH) # <-- [修复] 移除
            template = prompt_template  # <-- [修复] 使用传入的模板
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
                player_inventory=player_inventory,
                field_item_intel=field_item_intel,  # (新) 添加
                min_raise_increment=min_raise_increment,
                dealer_name=dealer_name,
                observed_moods=observed_moods,
                multiplier=multiplier,
                call_cost=call_cost,
                table_seating=table_seating_str,
                public_event_log=public_event_log, # (新) 添加
                opponent_reference=opponent_reference_str
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

            return {"action": "FOLD", "reason": error_msg_oneline, "target_name": None, "mood": "解析失败",
                    "speech": None,
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
                     defend_prompt_template: str,  # <-- [修复] 添加模板参数
                     accuser_name: str,
                     partner_name: str,
                     evidence_log: str,
                     my_persona: str,  # (新) 添加人设参数
                     stream_start_cb: Callable[[str], Awaitable[None]],
                     stream_chunk_cb: Callable[[str], Awaitable[None]]) -> str:

        # template = self._read_file(DEFEND_PROMPT_PATH) # <-- [修复] 移除
        template = defend_prompt_template  # <-- [修复] 使用传入的模板
        if not template:
            return "我无话可说。"

        prompt = template.format(
            self_name=self.name,
            accuser_name=accuser_name,
            partner_name=partner_name,
            evidence_log=evidence_log,
            my_persona=my_persona  # (新) 添加人设
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
                   vote_prompt_template: str,  # <-- [修复] 添加模板参数
                   accuser_name: str,
                   target_name_1: str,
                   target_name_2: str,
                   evidence_log: str,
                   defense_speech_1: str,
                   defense_speech_2: str,
                   my_persona: str,  # (新) 添加人设参数
                   previous_jury_reason: str = "",  # (新增) 前一位陪审团成员的理由
                   stream_start_cb: Callable[[str], Awaitable[None]] = None,
                   stream_chunk_cb: Callable[[str], Awaitable[None]] = None) -> dict:

        # template = self._read_file(VOTE_PROMPT_PATH) # <-- [修复] 移除
        template = vote_prompt_template  # <-- [修复] 使用传入的模板
        if not template:
            return {"vote": "NOT_GUILTY", "reason": "模板错误", "thinking_process": ""}

        # 处理前一位陪审团理由的显示
        if not previous_jury_reason:
            previous_jury_reason = "无前一位陪审团成员的意见"

        prompt = template.format(
            self_name=self.name,
            accuser_name=accuser_name,
            target_name_1=target_name_1,
            target_name_2=target_name_2,
            evidence_log=evidence_log,
            defense_speech_1=defense_speech_1,
            defense_speech_2=defense_speech_2,
            my_persona=my_persona,  # (新) 添加人设
            previous_jury_reason=previous_jury_reason  # (新增) 前一位陪审团理由
        )
        messages = [{"role": "user", "content": prompt}]

        if stream_start_cb:
            await stream_start_cb(f"【上帝(陪审团投票)】: [{self.name} 正在秘密投票...]: ")

        try:
            thinking_buffer = ""
            def collect_thinking(chunk: str):
                nonlocal thinking_buffer
                thinking_buffer += chunk
                if stream_chunk_cb:
                    asyncio.create_task(stream_chunk_cb(chunk))

            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=collect_thinking
            )

            json_match = re.search(r'```json\s*({[\s\S]*?})\s*```|\s*({[\s\S]*})', full_content)
            if json_match:
                json_str = json_match.group(1) or json_match.group(2)
                result = json.loads(json_str)
                vote = result.get("vote", "NOT_GUILTY").upper()
                thinking_process = result.get("thinking_process", "")
                reason = result.get("reason", "分析过程出错")

                # 确保返回完整的投票信息
                return {
                    "vote": vote,
                    "reason": reason,
                    "thinking_process": thinking_process,
                    "full_response": full_content
                }

            # 如果解析失败，返回默认值
            return {
                "vote": "NOT_GUILTY",
                "reason": "投票解析失败",
                "thinking_process": "无法解析AI回复",
                "full_response": full_content
            }

        except Exception as e:
            error_msg = f"投票时出错: {str(e)}"
            if stream_chunk_cb:
                await stream_chunk_cb(f"\n{error_msg} (自动投: 无罪)\n")
            return {
                "vote": "NOT_GUILTY",
                "reason": error_msg,
                "thinking_process": "系统错误",
                "full_response": ""
            }

    async def decide_bribe(self,
                           bribe_prompt_template: str,
                           bribe_cost: int,
                           success_chance: float,
                           penalty_chips: int,
                           # ↓↓ 在这里添加两个新参数 ↓↓
                           payment_method_string: str,
                           consequence_string: str,
                           # ↑↑ 添加完毕 ↑↑
                           stream_start_cb: Callable[[str], Awaitable[None]],
                           stream_chunk_cb: Callable[[str], Awaitable[None]]) -> dict:
        """(新) 玩家决定是否贿赂"""
        if not bribe_prompt_template:
            return {"bribe": False, "reason": "系统错误：贿赂模板未加载"}

        prompt = bribe_prompt_template.format(
            self_name=self.name,
            bribe_cost=bribe_cost,
            success_chance_percent=success_chance * 100.0,
            penalty_chips=penalty_chips,
            success_chance=success_chance,  # (为防万一，也传入原始小数)
            # ↓↓ 在这里添加两个新参数 ↓↓
            payment_method_string=payment_method_string,
            consequence_string=consequence_string
            # ↑↑ 添加完毕 ↑↑
        )
        messages = [{"role": "user", "content": prompt}]

        await stream_start_cb(f"【上帝(密谈)】: [{self.name} 正在紧急决策...]: ")
        try:
            full_content = await self.llm_client.chat_stream(
                messages,
                model=self.model_name,
                stream_callback=stream_chunk_cb
            )
            await stream_chunk_cb("\n")

            result = self._parse_first_valid_json(full_content)
            if result and "bribe" in result:
                return result

            # 如果 JSON 解析失败，默认拒绝贿赂
            return {"bribe": False, "reason": "JSON 解析失败或未提供 bribe 键"}

        except Exception as e:
            await stream_chunk_cb(f"\n决策贿赂时出错: {str(e)}\n")
            return {"bribe": False, "reason": f"决策时发生异常: {str(e)}"}

    # (已修改)
    async def reflect(self,
                      reflect_prompt_template: str,  # <-- [修复] 添加模板参数
                      round_history: str,
                      round_result: str,
                      current_impressions_json: str,
                      # (新) 添加 2 个参数
                      player_self_details: str,
                      opponent_name_list: str,
                      stream_start_cb: Callable[[str], Awaitable[None]],
                      stream_chunk_cb: Callable[[str], Awaitable[None]]) -> (str, dict):
        """
        (已修改)
        增强 reflect 的错误处理
        """
        full_content_debug = ""  # (新)
        try:
            # template = self._read_file(REFLECT_PROMPT_PATH) # <-- [修复] 移除
            template = reflect_prompt_template  # <-- [修复] 使用传入的模板
            if not template:
                raise RuntimeError("无法读取复盘 Prompt")

            prompt = template.format(
                self_name=self.name,
                round_history=round_history,
                round_result=round_result,
                current_impressions_json=current_impressions_json,
                # (新) 传入 2 个参数
                player_self_details=player_self_details,
                opponent_name_list=opponent_name_list
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
