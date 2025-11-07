import time
import json
import asyncio
import random
from typing import List, Dict, Callable, Awaitable, Tuple, Optional

from zhajinhua import ZhajinhuaGame, GameConfig, Action
from game_rules import ActionType, INT_TO_RANK, SUITS, GameConfig, evaluate_hand, Card, RANK_TO_INT
from player import Player


class GameController:
    """
    (已修改：修复 _build_panel_data 中的 NameError)
    """

    def __init__(self,
                 player_configs: List[Dict],
                 god_print_callback: Callable[..., Awaitable[None]],
                 god_stream_start_callback: Callable[..., Awaitable[None]],
                 god_stream_chunk_callback: Callable[..., Awaitable[None]],
                 god_panel_update_callback: Callable[..., Awaitable[None]]):

        self.player_configs = player_configs
        self.num_players = len(player_configs)
        self.players = [Player(config["name"], config["model"]) for config in player_configs]

        default_chips = GameConfig.initial_chips
        self.persistent_chips: List[int] = [default_chips] * self.num_players

        self.god_print = god_print_callback
        self.god_stream_start = god_stream_start_callback
        self.god_stream_chunk = god_stream_chunk_callback
        self.god_panel_update = god_panel_update_callback

        self.hand_count = 0
        self.last_winner_id = 0

        self.player_personas: Dict[int, str] = {}
        self.player_reflections: Dict[int, str] = {}
        self.player_observed_moods: Dict[int, str] = {}
        self.player_last_speech: Dict[int, str | None] = {}
        self.player_private_impressions: Dict[int, Dict[int, str]] = {}

        self.secret_message_log: List[Tuple[int, int, int, str]] = []
        self.cheat_action_log: List[Tuple[int, int, str, Dict]] = []  # (新) 记录作弊

        # (新) 用于在解析动作后输出额外的警告信息
        self._parse_warnings: List[str] = []

        self._suit_alias_map = {
            "♠": "♠", "黑桃": "♠", "黑心": "♠", "spade": "♠", "spades": "♠",
            "♥": "♥", "红桃": "♥", "红心": "♥", "heart": "♥", "hearts": "♥",
            "♣": "♣", "梅花": "♣", "草花": "♣", "club": "♣", "clubs": "♣",
            "♦": "♦", "方块": "♦", "diamond": "♦", "diamonds": "♦"
        }

        self.CHEAT_SWAP_REQUIRED_EXPERIENCE = 55.0
        self.CHEAT_RANK_REQUIRED_EXPERIENCE = 75.0
        self._cheat_detection_base = {1: 0.16, 2: 0.32, 3: 0.48}

        base_config = GameConfig(num_players=self.num_players)
        self._base_ante_total = base_config.base_bet * self.num_players
        self._ante_increase_interval = 5
        self._ante_increment = 20

    def get_alive_player_count(self) -> int:
        return sum(1 for chips in self.persistent_chips if chips > 0)

    def _get_total_ante_for_current_hand(self) -> int:
        if self._ante_increase_interval <= 0:
            return self._base_ante_total
        hand_index = max(self.hand_count, 1)
        increments = (hand_index - 1) // self._ante_increase_interval
        return self._base_ante_total + increments * self._ante_increment

    def _build_ante_distribution(self) -> tuple[int, List[int], int]:
        alive_indices = [i for i, chips in enumerate(self.persistent_chips) if chips > 0]
        total_ante = self._get_total_ante_for_current_hand()
        distribution = [0] * self.num_players
        if not alive_indices:
            return 0, distribution, total_ante

        base_share = total_ante // len(alive_indices)
        remainder = total_ante % len(alive_indices)

        for order, player_idx in enumerate(alive_indices):
            ante = base_share + (1 if order < remainder else 0)
            distribution[player_idx] = ante

        per_player_base = base_share + (1 if remainder > 0 else 0)
        return per_player_base, distribution, total_ante

    def _build_panel_data(self, game: ZhajinhuaGame | None, start_player_id: int = -1) -> dict:
        # (已修改)
        players_data = []
        for i, p in enumerate(self.players):
            hand_str = "..."
            player_looked = False
            player_is_active = False
            is_dealer = (i == start_player_id)
            player_chips = self.persistent_chips[i]
            if self.persistent_chips[i] <= 0:
                hand_str = "已淘汰"
            elif game and game.state and game.state.players:
                p_state = game.state.players[i]
                player_chips = p_state.chips
                player_looked = p_state.looked
                if not p_state.alive:
                    hand_str = "已弃牌"
                else:
                    player_is_active = True
                    if p_state.hand:
                        # --- (BUG 修复) ---
                        # sorted_hand = sorted(ps.hand, key=lambda c: c.rank, reverse=True) # (错误)
                        sorted_hand = sorted(p_state.hand, key=lambda c: c.rank, reverse=True)  # (正确)
                        # --- (修复结束) ---
                        hand_str = ' '.join([INT_TO_RANK[c.rank] + SUITS[c.suit] for c in sorted_hand])
                    else:
                        hand_str = "..."
                self.players[i].update_pressure_snapshot(player_chips, game.get_call_cost(i) if game else 0)
            players_data.append({
                "id": i,
                "name": p.name,
                "chips": player_chips,
                "hand_str": hand_str,
                "looked": player_looked,
                "is_active": player_is_active,
                "is_dealer": is_dealer,
                "experience_level": p.get_experience_level(),
                "experience_value": round(p.experience, 1),
                "pressure_state": p.get_pressure_descriptor()
            })
        return {
            "hand_count": self.hand_count,
            "current_pot": game.state.pot if game and game.state else 0,
            "players": players_data
        }

    async def run_game(self):
        # ... (此函数无修改) ...
        await self.god_print(f"--- 锦标赛开始 ---", 1)
        await self.god_print(f"初始筹码: {self.persistent_chips}", 1)
        await self.god_panel_update(self._build_panel_data(None, -1))

        await self.god_print(f"--- 牌桌介绍开始 ---", 1.5)
        await self.god_print(f"（AI 正在为自己杜撰人设...）", 0.5)

        for i, player in enumerate(self.players):
            if self.persistent_chips[i] <= 0 and player.alive:
                self.player_personas[i] = f"我是 {player.name} (已淘汰)"
                continue

            await self.god_stream_start(f"【上帝(赛前介绍)】: [{player.name}]: ")

            intro_text = await player.create_persona(
                stream_chunk_cb=self.god_stream_chunk
            )

            if "(创建人设时出错:" in intro_text:
                await self.god_stream_chunk(f" {intro_text}")

            await self.god_stream_chunk("\n")

            self.player_personas[i] = intro_text
            self.players[i].register_persona(intro_text)  # (新) 初始化经验
            await asyncio.sleep(0.5)

        await self.god_print(f"--- 牌桌介绍结束 ---", 2)
        await asyncio.sleep(3)

        while self.get_alive_player_count() > 1:
            self.hand_count += 1
            start_player_id = (self.last_winner_id + 1) % self.num_players
            start_attempts = 0
            while self.persistent_chips[start_player_id] <= 0:
                start_player_id = (start_player_id + 1) % self.num_players
                start_attempts += 1
                if start_attempts > self.num_players:
                    start_player_id = 0
                    break
            p_name = self.players[start_player_id].name
            await self.god_print(f"--- 第 {self.hand_count} 手牌开始 (庄家: {p_name}) ---", 1.5)

            try:
                await self.run_round(start_player_id)
            except Exception as e:
                await self.god_print(f"!! run_round 发生严重错误: {e} !!", 1)
                import traceback
                traceback.print_exc()
                await self.god_print("!! 游戏循环已崩溃，停止锦标赛 !!", 1)
                break

            if self.get_alive_player_count() <= 1:
                break

            alive_players_post_hand = []
            for i, p in enumerate(self.players):
                if self.persistent_chips[i] > 0:
                    alive_players_post_hand.append(f"{p.name} ({self.persistent_chips[i]})")
                else:
                    if p.alive:
                        await self.god_print(f"!!! 玩家 {p.name} 筹码输光，已被淘汰 !!!", 1)
                        p.alive = False
            await self.god_print(f"本手牌结束。存活玩家: {', '.join(alive_players_post_hand)}", 2)
            await self.god_panel_update(self._build_panel_data(None, -1))
            await asyncio.sleep(3)

        await self.god_print(f"--- 锦标赛结束 ---", 2)
        for i, p in enumerate(self.players):
            if self.persistent_chips[i] > 0:
                await self.god_print(f"最终胜利者是: {p.name} (剩余筹码: {self.persistent_chips[i]})!", 5)
                break

    def _build_llm_prompt(self, game: ZhajinhuaGame, player_id: int, start_player_id: int) -> tuple:
        # ... (此函数无修改) ...
        st = game.state
        ps = st.players[player_id]

        state_summary_lines = [
            f"当前是 {self.players[st.current_player].name} 的回合。",
            f"底池 (Pot): {st.pot}", f"当前暗注 (Base Bet): {st.current_bet}",
            f"最后加注者: {self.players[st.last_raiser].name if st.last_raiser is not None else 'N/A'}"
        ]
        state_summary_lines.append("\n玩家信息:")
        player_status_list: list[str] = []
        for i, p in enumerate(st.players):
            p_name = self.players[i].name
            if self.persistent_chips[i] <= 0:
                status = "已淘汰"
            elif not game.state.players[i].alive:
                status = "已弃牌"
            elif game.state.players[i].all_in:  # <-- 修复：增加此项
                status = "已All-In"
            elif game.state.players[i].looked:
                status = "已看牌"
            else:
                status = "未看牌"
            status_line = f"  - {p_name}: 筹码={p.chips}, 状态={status}"
            state_summary_lines.append(status_line)
            player_status_list.append(status)

        my_hand = "你还未看牌。"
        if ps.looked:
            sorted_hand = sorted(ps.hand, key=lambda c: c.rank, reverse=True)
            hand_str_list = [INT_TO_RANK[c.rank] + SUITS[c.suit] for c in sorted_hand]
            try:
                hand_rank_obj = evaluate_hand(ps.hand)
                my_hand = f"你的手牌是: {' '.join(hand_str_list)} (牌型: {hand_rank_obj.hand_type.name})"
            except Exception:
                my_hand = f"你的手牌是: {' '.join(hand_str_list)} (牌型: 评估失败)"

        available_actions_tuples = []
        raw_actions = game.available_actions(player_id)
        call_cost = 0
        for act_type, display_cost in raw_actions:
            if act_type == ActionType.CALL: call_cost = display_cost
            available_actions_tuples.append((act_type.name, display_cost))
        available_actions_str = "\n".join(f"  - {name}: 成本={cost}" for name, cost in available_actions_tuples)

        next_player_id = game.next_player(start_from=player_id)
        next_player_name = self.players[next_player_id].name

        seating_lines = []
        opponent_reference_lines = []
        for seat_offset in range(self.num_players):
            seat_player_id = (start_player_id + seat_offset) % self.num_players
            seat_player = self.players[seat_player_id]
            seat_role_parts = [f"座位{seat_offset + 1}"]
            if seat_offset == 0:
                seat_role_parts.append("庄家")
            if seat_player_id == player_id:
                seat_role_parts.append("你")
            relation_offset = (seat_player_id - player_id) % self.num_players
            if relation_offset == 1:
                relation_desc = "你的下家"
            elif relation_offset == 0:
                relation_desc = "你自己"
            elif relation_offset == self.num_players - 1:
                relation_desc = "你的上家"
            else:
                relation_desc = f"距离你 {relation_offset} 位"

            seat_role = " / ".join(seat_role_parts)
            status = player_status_list[seat_player_id] if seat_player_id < len(player_status_list) else "未知"
            seat_chip_info = st.players[seat_player_id].chips if seat_player_id < len(st.players) else \
                self.persistent_chips[seat_player_id]
            seating_lines.append(
                f"  - {seat_role}: {seat_player.name} (筹码={seat_chip_info}, 状态={status})"
            )

            if seat_player_id != player_id:
                opponent_reference_lines.append(
                    f"  - {seat_player.name}: 座位={seat_role}，相对位置={relation_desc}，筹码={seat_chip_info}，状态={status}"
                )

        table_seating_str = "\n".join(seating_lines)
        opponent_reference_str = "\n".join(opponent_reference_lines) if opponent_reference_lines else "暂无其他对手。"

        player_obj = self.players[player_id]
        opponent_personas_lines = []
        for i, p in enumerate(self.players):
            if i == player_id: continue
            persona = self.player_personas.get(i)
            if persona: opponent_personas_lines.append(f"  - {p.name}: {persona}")
        opponent_personas_str = "\n".join(opponent_personas_lines) if opponent_personas_lines else "暂无对手的开场介绍。"

        reflection_lines = []
        for i, p in enumerate(self.players):
            if i == player_id: continue
            reflection = self.player_reflections.get(i)
            if reflection: reflection_lines.append(f"  - {p.name}: {reflection}")
        opponent_reflections_str = "\n".join(reflection_lines) if reflection_lines else "暂无对手的过往复盘发言。"

        private_impressions_lines = []
        player_notes = self.player_private_impressions.get(player_id, {})
        for opp_id, note in player_notes.items():
            if opp_id != player_id:
                private_impressions_lines.append(f"  - {self.players[opp_id].name}: {note}")
        opponent_private_impressions_str = "\n".join(
            private_impressions_lines) if private_impressions_lines else "暂无你对对手的私有笔记。"

        speech_lines = []
        for i, p in enumerate(self.players):
            if i == player_id: continue
            speech = self.player_last_speech.get(i)
            if speech: speech_lines.append(f"  - {p.name} (上一轮) 说: {speech}")
        observed_speech_str = "\n".join(speech_lines) if speech_lines else "暂无牌桌发言。"

        mood_lines = []
        for i, p in enumerate(self.players):
            if i == player_id: continue
            mood = self.player_observed_moods.get(i)
            if mood: mood_lines.append(f"  - {p.name} 看起来: {mood}")
        observed_moods_str = "\n".join(mood_lines) if mood_lines else "暂未观察到对手的明显情绪。"

        secret_message_lines = []
        for (hand_num, sender, recipient, message) in self.secret_message_log:
            if hand_num == self.hand_count and recipient == player_id:
                sender_name = self.players[sender].name
                secret_message_lines.append(f"  - [密信] 来自 {sender_name}: {message}")
        received_secret_messages_str = "\n".join(secret_message_lines) if secret_message_lines else "你没有收到任何秘密消息。"

        min_raise_increment = st.config.min_raise
        dealer_name = self.players[start_player_id].name
        multiplier = 2 if ps.looked else 1

        player_obj.update_pressure_snapshot(ps.chips, call_cost)
        my_persona_str = f"你正在扮演: {self.player_personas.get(player_id, '(暂无)')}"
        my_persona_str += f"\n【你的牌局经验】{player_obj.get_experience_summary()}"
        my_persona_str += f"\n【当前心理压力】{player_obj.get_pressure_descriptor()}"
        if ps.chips < 300:
            my_persona_str += f"\n【筹码警报】你的筹码只有 {ps.chips} (<300)，再不出招就会被淘汰。权衡是否需要孤注一掷或动用作弊手段。"
        else:
            my_persona_str += f"\n【筹码状态】当前筹码 {ps.chips}，警戒线为 300。"

        return (
            "\n".join(state_summary_lines), my_hand, available_actions_str, available_actions_tuples,
            next_player_name, my_persona_str, opponent_personas_str, opponent_reflections_str,
            opponent_private_impressions_str, observed_speech_str,
            received_secret_messages_str,
            min_raise_increment, dealer_name, observed_moods_str, multiplier, call_cost,
            table_seating_str, opponent_reference_str
        )

    def _parse_action_json(self, game: ZhajinhuaGame, action_json: dict, player_id: int,
                           available_actions: list) -> (Action, str):
        # ... (此函数无修改) ...
        self._parse_warnings.clear()
        action_name = action_json.get("action", "FOLD").upper()

        def find_target_id(target_name_key: str) -> (int | None, str):
            target_name = action_json.get(target_name_key)
            if not target_name:
                return None, f"未提供 {target_name_key} (比牌或指控时必须明确指定目标)"
            for i, p in enumerate(self.players):
                if p.name.strip() == target_name.strip():
                    if game.state.players[i].alive:
                        return i, ""
                    else:
                        return None, f"目标 {target_name} 已弃牌"
            return None, f"未找到目标 {target_name}"

        action_type = None
        for (name, cost) in available_actions:
            if name == action_name:
                action_type = ActionType[action_name]
                break

        if action_type is None and action_name == "LOOK":
            # 特殊处理：如果玩家已经看过牌，LLM 仍然可能再次选择 LOOK。
            # 这种情况下不应强制弃牌，而是允许其作为一次“无效”的再看牌操作。
            player_state = game.state.players[player_id]
            if player_state.looked:
                action_type = ActionType.LOOK

        if action_type is None and action_name == "RAISE":
            player_state = game.state.players[player_id]
            call_cost = game.get_call_cost(player_id)
            chips = player_state.chips
            multiplier = 2 if player_state.looked else 1
            min_raise_inc = game.state.config.min_raise
            amount_val: Optional[int] = None
            try:
                amount_val = int(action_json.get("amount"))
            except (TypeError, ValueError):
                amount_val = None

            can_call = any(name == "CALL" for name, _ in available_actions)
            can_all_in = any(name == "ALL_IN_SHOWDOWN" for name, _ in available_actions)
            max_affordable_increment = (chips - call_cost) // multiplier if chips >= call_cost else -1

            fallback_applied = False
            if chips < call_cost:
                if can_all_in:
                    action_type = ActionType.ALL_IN_SHOWDOWN
                    fallback_applied = True
                    self._parse_warnings.append(
                        f"警告: {self.players[player_id].name} 加注失败 (筹码不足 {chips}/{call_cost})，自动改为 ALL_IN_SHOWDOWN。"
                    )
            else:
                insufficient_raise = (
                        amount_val is None
                        or amount_val < min_raise_inc
                        or max_affordable_increment < min_raise_inc
                        or amount_val > max_affordable_increment
                )
                total_cost = call_cost + (amount_val or 0) * multiplier if amount_val is not None else None
                if total_cost is not None and chips <= total_cost:
                    insufficient_raise = True

                if insufficient_raise and can_call:
                    action_type = ActionType.CALL
                    fallback_applied = True
                    self._parse_warnings.append(
                        f"警告: {self.players[player_id].name} 筹码不足以加注 (尝试 amount={amount_val})，自动改为 CALL。"
                    )

            if fallback_applied:
                action_json["action"] = action_type.name
                action_json["amount"] = None
                action_name = action_type.name

        if action_type is None:
            error_msg = f"警告: {self.players[player_id].name} S 选择了无效动作 '{action_name}' (可能筹码不足)。强制弃牌。"
            return Action(player=player_id, type=ActionType.FOLD), error_msg

        amount = None
        target = None
        target2 = None

        if action_type == ActionType.RAISE:
            min_inc = game.state.config.min_raise
            try:
                amount_increment_str = action_json.get("amount")
                amount = int(amount_increment_str)
                if amount < min_inc:
                    return Action(player=player_id,
                                  type=ActionType.FOLD), f"警告: {self.players[player_id].name} 试图加注 {amount}，小于最小增量 {min_inc}。强制弃牌。"
            except (ValueError, TypeError):
                return Action(player=player_id,
                              type=ActionType.FOLD), f"警告: {self.players[player_id].name} RAISE 动作未提供有效的 'amount'。强制弃牌。"

        elif action_type == ActionType.COMPARE:
            target_id, err = find_target_id("target_name")
            if err:
                return Action(player=player_id,
                              type=ActionType.FOLD), f"警告: {self.players[player_id].name} COMPARE 失败: {err}。强制弃牌。"
            target = target_id

        elif action_type == ActionType.ACCUSE:
            target_id_1, err1 = find_target_id("target_name")
            target_id_2, err2 = find_target_id("target_name_2")
            if err1 or err2:
                return Action(player=player_id,
                              type=ActionType.FOLD), f"警告: {self.players[player_id].name} ACCUSE 失败: {err1} / {err2}。强制弃牌。"
            if target_id_1 == target_id_2:
                return Action(player=player_id,
                              type=ActionType.FOLD), f"警告: {self.players[player_id].name} ACCUSE 失败: 不能指控同一个人。强制弃牌。"
            target = target_id_1
            target2 = target_id_2

        return Action(player=player_id, type=action_type, amount=amount, target=target, target2=target2), ""

    async def _handle_secret_message(self, game: ZhajinhuaGame, sender_id: int, message_json: dict):
        # ... (此函数无修改) ...
        target_name = message_json.get("target_name")
        message = message_json.get("message")
        sender_name = self.players[sender_id].name

        if not target_name or not message:
            await self.god_print(f"!! {sender_name} 试图发送格式错误的秘密消息。", 0.5)
            return

        target_id = -1
        for i, p in enumerate(self.players):
            if p.name == target_name:
                target_id = i
                break

        valid_recipients = [
            i for i, st_player in enumerate(game.state.players)
            if i != sender_id and st_player.alive and self.players[i].alive
        ]

        if target_id == -1 or target_id not in valid_recipients:
            if not valid_recipients:
                await self.god_print(f"!! {sender_name} 想发送秘密消息，但没有有效的接收者。", 0.5)
                return
            original_target = target_name
            target_id = valid_recipients[0]
            target_name = self.players[target_id].name
            await self.god_print(
                f"!! {sender_name} 指定的秘密消息目标 {original_target} 无效，已改为 {target_name}。",
                0.5
            )

        if target_id == sender_id:
            await self.god_print(f"!! {sender_name} 试图给自己发送秘密消息。", 0.5)
            return

        self.secret_message_log.append((self.hand_count, sender_id, target_id, message))
        await self.god_print(f"【上帝(密信)】: {sender_name} -> {target_name} (消息已记录)", 0.5)

    def _normalize_suit_symbol(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        cleaned = str(raw).strip().lower()
        # 优先匹配原始符号
        if raw in self._suit_alias_map:
            return self._suit_alias_map[raw]
        return self._suit_alias_map.get(cleaned)

    def _normalize_rank_symbol(self, raw: Optional[str]) -> Optional[str]:
        if raw is None:
            return None
        text = str(raw).strip().upper()
        if not text:
            return None
        if text in RANK_TO_INT:
            return text
        if text in {"14", "1", "A"}:
            return "A"
        if text in {"13", "K"}:
            return "K"
        if text in {"12", "Q"}:
            return "Q"
        if text in {"11", "J"}:
            return "J"
        return text if text in RANK_TO_INT else None

    def _calculate_detection_probability(self, player_obj: Player, cheat_type: str, cards_count: int,
                                         chips: int) -> float:
        base = self._cheat_detection_base.get(cards_count, 0.48 + 0.18 * max(0, cards_count - 3))
        if cheat_type == "SWAP_RANK":
            base += 0.08
        experience_mitigation = min(0.4, player_obj.experience / 320.0)
        pressure_penalty = min(0.25, player_obj.current_pressure * 0.45)
        low_stack_penalty = 0.0
        if chips < 300:
            low_stack_penalty = 0.2 + min(0.3, (300 - max(chips, 0)) / 400.0)
        probability = base - experience_mitigation + pressure_penalty + low_stack_penalty
        return max(0.05, min(0.95, probability))

    async def _handle_cheat_move(self, game: ZhajinhuaGame, player_id: int, cheat_move: Optional[dict]) -> Dict[
        str, object]:
        """(新) 处理换花色/点数作弊。"""
        result = {"attempted": False, "success": False, "type": None, "detected": False, "cards": []}
        if not cheat_move or not isinstance(cheat_move, dict):
            return result

        result["attempted"] = True
        cheat_type_raw = str(cheat_move.get("type", "")).upper()
        result["type"] = cheat_type_raw or "UNKNOWN"
        player_obj = self.players[player_id]
        player_name = player_obj.name

        if cheat_type_raw not in {"SWAP_SUIT", "SWAP_RANK"}:
            await self.god_print(f"【上帝(警告)】: {player_name} 试图执行未知作弊动作 {cheat_type_raw}。", 0.5)
            log_payload = {"success": False, "error": "未知作弊类型", "raw": cheat_move}
            self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
            player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
            return result

        required_exp = self.CHEAT_SWAP_REQUIRED_EXPERIENCE if cheat_type_raw == "SWAP_SUIT" else self.CHEAT_RANK_REQUIRED_EXPERIENCE
        if player_obj.experience < required_exp:
            await self.god_print(
                f"【上帝(警告)】: {player_name} 试图换牌 ({'花色' if cheat_type_raw == 'SWAP_SUIT' else '点数'})，但经验不足 ({player_obj.experience:.1f} < {required_exp})。",
                0.5
            )
            log_payload = {"success": False, "error": "经验不足", "raw": cheat_move}
            self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
            player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
            return result

        cards_payload = cheat_move.get("cards")
        single_card_payload = None
        if not cards_payload:
            single_card_payload = {
                "card_index": cheat_move.get("card_index"),
                "new_suit": cheat_move.get("new_suit"),
                "new_rank": cheat_move.get("new_rank")
            }
            cards_payload = [single_card_payload]

        if not isinstance(cards_payload, list):
            await self.god_print(f"【上帝(警告)】: {player_name} 的作弊请求缺少有效的 cards 列表。", 0.5)
            log_payload = {"success": False, "error": "cards 无效", "raw": cheat_move}
            self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
            player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
            return result

        ps = game.state.players[player_id]
        modifications = []
        for entry in cards_payload:
            try:
                card_index = int(entry.get("card_index"))
            except (TypeError, ValueError):
                await self.god_print(f"【上帝(警告)】: {player_name} 提供的换牌索引无效: {entry.get('card_index')}。", 0.5)
                log_payload = {"success": False, "error": "索引无效", "raw": cheat_move}
                self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
                player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
                return result

            idx = card_index - 1 if card_index > 0 else card_index
            if idx < 0 or idx >= len(ps.hand):
                await self.god_print(f"【上帝(警告)】: {player_name} 试图修改不存在的第 {card_index} 张牌。", 0.5)
                log_payload = {"success": False, "error": "索引越界", "raw": cheat_move, "card_index": card_index}
                self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
                player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
                return result

            old_card = ps.hand[idx]
            if cheat_type_raw == "SWAP_SUIT":
                target_suit_symbol = self._normalize_suit_symbol(entry.get("new_suit"))
                if target_suit_symbol is None:
                    await self.god_print(f"【上帝(警告)】: {player_name} 提供的目标花色无效: {entry.get('new_suit')}。", 0.5)
                    log_payload = {"success": False, "error": "花色无效", "raw": cheat_move}
                    self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
                    player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
                    return result
                if SUITS[old_card.suit] == target_suit_symbol:
                    continue
                new_card = Card(rank=old_card.rank, suit=SUITS.index(target_suit_symbol))
                modifications.append({
                    "index": idx,
                    "card_index_display": card_index,
                    "old": old_card,
                    "new": new_card,
                    "from": SUITS[old_card.suit],
                    "to": target_suit_symbol,
                })
            else:
                target_rank_symbol = self._normalize_rank_symbol(entry.get("new_rank"))
                if target_rank_symbol is None or target_rank_symbol not in RANK_TO_INT:
                    await self.god_print(f"【上帝(警告)】: {player_name} 提供的目标点数无效: {entry.get('new_rank')}。", 0.5)
                    log_payload = {"success": False, "error": "点数无效", "raw": cheat_move}
                    self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
                    player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
                    return result
                if old_card.rank == RANK_TO_INT[target_rank_symbol]:
                    continue
                new_card = Card(rank=RANK_TO_INT[target_rank_symbol], suit=old_card.suit)
                modifications.append({
                    "index": idx,
                    "card_index_display": card_index,
                    "old": old_card,
                    "new": new_card,
                    "from": INT_TO_RANK[old_card.rank],
                    "to": target_rank_symbol,
                })

        if not modifications:
            await self.god_print(f"【上帝(提示)】: {player_name} 的作弊请求未产生有效变化。", 0.5)
            log_payload = {"success": False, "error": "无变更", "raw": cheat_move}
            self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
            player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
            return result

        detection_probability = self._calculate_detection_probability(
            player_obj, cheat_type_raw, len(modifications), ps.chips)

        detected = random.random() < detection_probability
        if detected:
            await self.god_print(
                f"【上帝(抓现行)】: {player_name} 偷换牌被巡逻荷官发现！({len(modifications)} 张, 类型: {cheat_type_raw})",
                0.5
            )
            log_payload = {
                "success": False,
                "detected": True,
                "error": "被当场抓住",
                "raw": cheat_move,
                "cards": [
                    {
                        "card_index": m["card_index_display"],
                        "from": m.get("from"),
                        "to": m.get("to"),
                    }
                    for m in modifications
                ],
                "probability": round(detection_probability, 3)
            }
            result["detected"] = True
            self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
            player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
            return result

        for m in modifications:
            ps.hand[m["index"]] = m["new"]

        cover_story = cheat_move.get("cover_story")
        log_payload = {
            "success": True,
            "cards": [
                {
                    "card_index": m["card_index_display"],
                    "from": m.get("from"),
                    "to": m.get("to"),
                }
                for m in modifications
            ],
            "cover_story": cover_story,
            "probability": round(detection_probability, 3)
        }
        self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
        player_obj.update_experience_from_cheat(True, cheat_type_raw, log_payload)

        if cheat_type_raw == "SWAP_SUIT":
            changes_desc = ", ".join(
                f"第 {m['card_index_display']} 张 {m['from']}→{m['to']}" for m in modifications
            )
        else:
            changes_desc = ", ".join(
                f"第 {m['card_index_display']} 张 {m['from']}→{m['to']}" for m in modifications
            )

        await self.god_print(
            f"【上帝(作弊日志)】: {player_name} 偷偷修改了 {len(modifications)} 张牌 ({changes_desc})。",
            0.5
        )

        result["success"] = True
        result["cards"] = log_payload["cards"]
        return result

    async def _handle_accusation(self, game: ZhajinhuaGame, action: Action, start_player_id: int) -> bool:
        # ... (此函数无修改) ...
        accuser_id = action.player
        target_id_1 = action.target
        target_id_2 = action.target2
        accuser_name = self.players[accuser_id].name

        await self.god_print(f"--- !! 审判 !! ---", 1)

        if target_id_1 is None or target_id_2 is None:
            await self.god_print(f"!! {accuser_name} 指控失败：目标无效。", 0.5)
            return False

        target_name_1 = self.players[target_id_1].name
        target_name_2 = self.players[target_id_2].name
        await self.god_print(f"玩家 {accuser_name} 发起了指控！", 1)
        await self.god_print(f"指控目标: {target_name_1} 和 {target_name_2}", 1)

        jury_list = [
            i for i in game.alive_players()
            if not game.state.players[i].all_in
               and i not in [accuser_id, target_id_1, target_id_2]
        ]

        if not jury_list:
            await self.god_print(f"没有足够的陪审团成员 (0人)。审判自动失败。", 1)
            await self.god_print(f"{accuser_name} 的指控无效，但游戏继续。", 1)
            return False

        jury_names = ', '.join([self.players[i].name for i in jury_list])
        await self.god_print(f"陪审团成员: {jury_names}", 1)

        cost = game.get_accuse_cost(accuser_id)
        accuser_state = game.state.players[accuser_id]

        if accuser_state.chips < cost:
            await self.god_print(f"{accuser_name} 筹码不足 ({accuser_state.chips}) 支付指控成本 ({cost})。指控自动失败。", 1)
            return False

        accuser_state.chips -= cost
        game.state.pot += cost
        await self.god_print(f"{accuser_name} 支付 {cost} 筹码作为“指控堂费”(不退还)。", 1)
        await self.god_panel_update(self._build_panel_data(game, start_player_id))

        await self._run_trial_sub_loop(game, accuser_id, target_id_1, target_id_2, jury_list, start_player_id)
        return True

    async def _run_trial_sub_loop(self, game: ZhajinhuaGame, accuser_id: int, target_id_1: int, target_id_2: int,
                                  jury_list: List[int], start_player_id: int):
        # ... (此函数无修改) ...
        accuser_name = self.players[accuser_id].name
        target_name_1 = self.players[target_id_1].name
        target_name_2 = self.players[target_id_2].name

        await self.god_print(f"--- 审判阶段 1: 呈堂证供 ---", 1)
        await self.god_print(f"上帝正在审查 {target_name_1} 和 {target_name_2} (及相关者) 的*所有*秘密通讯...", 2)

        evidence_log_entries = []
        for (hand_num, sender, recipient, message) in self.secret_message_log:
            if sender == target_id_1 or recipient == target_id_1 or \
                    sender == target_id_2 or recipient == target_id_2:
                sender_name = self.players[sender].name
                recipient_name = self.players[recipient].name
                log = f"  - [H{hand_num}] {sender_name} -> {recipient_name}: {message}"
                evidence_log_entries.append(log)
                await self.god_print(log, 0.5)

        for (hand_num, actor_id, cheat_type, payload) in self.cheat_action_log:
            if actor_id == target_id_1 or actor_id == target_id_2:
                actor_name = self.players[actor_id].name
                status = "成功" if payload.get("success") else "失败"
                detail = payload.get(
                    "error") or f"第 {payload.get('card_index')} 张: {payload.get('from')} -> {payload.get('to')}"
                log = f"  - [H{hand_num}] {actor_name} 试图使用非法动作 {cheat_type} ({status}): {detail}"
                evidence_log_entries.append(log)
                await self.god_print(log, 0.5)

        if not evidence_log_entries:
            evidence_log_entries.append("  - (未发现任何相关秘密通讯)")
            await self.god_print("  - (未发现任何相关秘密通讯)", 0.5)

        evidence_log_str = "\n".join(evidence_log_entries)
        await asyncio.sleep(2)

        await self.god_print(f"--- 审判阶段 2: 被告辩护 ---", 1)

        defense_speech_1 = await self.players[target_id_1].defend(
            accuser_name, target_name_2, evidence_log_str,
            self.god_stream_start, self.god_stream_chunk
        )
        await asyncio.sleep(1)

        defense_speech_2 = await self.players[target_id_2].defend(
            accuser_name, target_name_1, evidence_log_str,
            self.god_stream_start, self.god_stream_chunk
        )
        await asyncio.sleep(2)

        await self.god_print(f"--- 审判阶段 3: 陪审团投票 ---", 1)

        vote_tasks = []
        for jury_id in jury_list:
            vote_tasks.append(
                self.players[jury_id].vote(
                    accuser_name, target_name_1, target_name_2,
                    evidence_log_str, defense_speech_1, defense_speech_2,
                    self.god_stream_start, self.god_stream_chunk
                )
            )

        votes = await asyncio.gather(*vote_tasks)
        await asyncio.sleep(1)

        await self.god_print(f"--- 审判阶段 4: 裁决 ---", 1)

        all_guilty = True
        for i, jury_id in enumerate(jury_list):
            vote_result = "有罪" if votes[i] == "GUILTY" else "无罪"
            await self.god_print(f"陪审团 {self.players[jury_id].name} 投票: {vote_result}", 1)
            if votes[i] != "GUILTY":
                all_guilty = False

        await asyncio.sleep(2)

        await self.god_print(f"--- 审判阶段 5: 执行判决 ---", 1)

        accuser_state = game.state.players[accuser_id]
        target_1_state = game.state.players[target_id_1]
        target_2_state = game.state.players[target_id_2]

        if all_guilty:
            await self.god_print(f"裁决：**一致有罪**！", 1)
            await self.god_print(f"{target_name_1} 和 {target_name_2} 联合作弊成立，立即处决！", 1)

            penalty_pool = target_1_state.chips + target_2_state.chips
            target_1_state.chips = 0
            target_2_state.chips = 0
            target_1_state.alive = False
            target_2_state.alive = False
            self.players[target_id_1].alive = False
            self.players[target_id_2].alive = False

            await self.god_print(f"没收 {target_name_1} 和 {target_name_2} 的全部筹码，共 {penalty_pool}。", 1)

            reward_accuser = int(penalty_pool * 0.7)
            reward_jury_pool = penalty_pool - reward_accuser

            accuser_state.chips += reward_accuser
            await self.god_print(f"指控者 {accuser_name} 获得 70% 奖励: {reward_accuser} 筹码。", 1)

            if jury_list:
                reward_per_jury = reward_jury_pool // len(jury_list)
                for i, jury_id in enumerate(jury_list):
                    game.state.players[jury_id].chips += reward_per_jury
                    if i == 0:
                        game.state.players[jury_id].chips += (reward_jury_pool % len(jury_list))
                await self.god_print(f"陪审团 (共 {len(jury_list)} 人) 瓜分 30% 奖励: {reward_jury_pool} 筹码。", 1)
            else:
                game.state.pot += reward_jury_pool
                await self.god_print(f"无人陪审团，{reward_jury_pool} 筹码进入底池。", 1)

        else:
            await self.god_print(f"裁决：**指控失败**！", 1)
            await self.god_print(f"未达到 100% 一致有罪。", 1)
            await self.god_print(f"指控者 {accuser_name} 因虚假指控，立即处决！", 1)

            penalty_pool = accuser_state.chips
            accuser_state.chips = 0
            accuser_state.alive = False
            self.players[accuser_id].alive = False

            await self.god_print(f"没收 {accuser_name} 的全部筹码: {penalty_pool}。", 1)

            reward_per_target = penalty_pool // 2
            target_1_state.chips += reward_per_target
            target_2_state.chips += (penalty_pool - reward_per_target)

            await self.god_print(f"{target_name_1} 和 {target_name_2} 瓜分了 {accuser_name} 的所有筹码。", 1)

        await self.god_print(f"--- 审判结束 ---", 1)
        await self.god_panel_update(self._build_panel_data(game, start_player_id))
        await asyncio.sleep(5)

    async def run_round(self, start_player_id: int):
        # (已修改) 增加调试打印
        config = GameConfig(num_players=self.num_players)
        per_player_base, ante_distribution, total_ante = self._build_ante_distribution()
        config.base_bet = per_player_base
        config.base_bet_distribution = ante_distribution

        alive_for_ante = sum(1 for amount in ante_distribution if amount > 0)
        if alive_for_ante > 0:
            await self.god_print(
                f"本手底注总额 {total_ante}，由 {alive_for_ante} 名玩家分摊 (基础暗注 {config.base_bet})。",
                0.5
            )

        game = ZhajinhuaGame(config, self.persistent_chips, start_player_id)

        self.player_observed_moods.clear()
        self.player_last_speech.clear()
        self.cheat_action_log.clear()

        await self.god_panel_update(self._build_panel_data(game, start_player_id))
        for i, p in enumerate(game.state.players):
            if self.persistent_chips[i] <= 0: p.alive = False
            if not p.alive and self.persistent_chips[i] > 0:
                ante_required = 0
                if config.base_bet_distribution:
                    ante_required = config.base_bet_distribution[i]
                else:
                    ante_required = config.base_bet
                await self.god_print(
                    f"玩家 {self.players[i].name} 筹码 ({self.persistent_chips[i]}) 不足支付底注 ({ante_required})，本手自动弃牌。",
                    0.5)

        await self.god_print("--- 初始发牌 (上帝视角已在看板) ---", 1)

        while not game.state.finished:
            if self.get_alive_player_count() <= 1:
                await self.god_print("审判导致只剩一名玩家，本局提前结束。", 1)
                game._force_showdown()
                break

            current_player_idx = game.state.current_player
            current_player_obj = self.players[current_player_idx]
            p_state = game.state.players[current_player_idx]

            if not p_state.alive or p_state.all_in:
                active_players = [i for i in game.alive_players() if not game.state.players[i].all_in]
                if len(active_players) <= 1:
                    game._force_showdown()
                    await self.god_panel_update(self._build_panel_data(game, start_player_id))
                    continue
                await self.god_print(f"跳过 {current_player_obj.name} (状态: {'All-In' if p_state.all_in else '已弃牌'})", 0.5)
                game._handle_next_turn()
                await self.god_panel_update(self._build_panel_data(game, start_player_id))
                continue

            await self.god_print(f"--- 轮到 {current_player_obj.name} ---", 1)

            (state_summary, my_hand, actions_str, actions_list,
             next_player_name, my_persona_str, opponent_personas_str, opponent_reflections_str,
             opponent_private_impressions_str, observed_speech_str,
             received_secret_messages_str,
             min_raise_increment, dealer_name,
             observed_moods_str, multiplier, call_cost,
             table_seating_str, opponent_reference_str) = self._build_llm_prompt(game, current_player_idx,
                                                                                 start_player_id)

            try:
                action_json = await current_player_obj.decide_action(
                    state_summary, my_hand, actions_str, next_player_name,
                    my_persona_str, opponent_personas_str, opponent_reflections_str,
                    opponent_private_impressions_str, observed_speech_str,
                    received_secret_messages_str,
                    min_raise_increment,
                    dealer_name,
                    observed_moods_str,
                    multiplier,
                    call_cost,
                    table_seating_str,
                    opponent_reference_str,
                    stream_start_cb=self.god_stream_start,
                    stream_chunk_cb=self.god_stream_chunk
                )
            except Exception as e:
                await self.god_print(f"!! 玩家 {current_player_obj.name} 决策失败 (Controller 捕获): {e}。强制弃牌。", 0)
                action_json = {"action": "FOLD", "reason": f"决策系统崩溃: {e}", "target_name": None, "mood": "崩溃",
                               "speech": None, "secret_message": None}

                # --- (新) 调试块：打印详细的错误原因 (已修正) ---
                player_mood = action_json.get("mood", "")
                player_action = action_json.get("action", "")

                # 只有当动作真的是 FOLD 且 mood 表明是错误时，才触发
                if (player_action == "FOLD" and
                        ("失败" in player_mood or "错误" in player_mood or "超时" in player_mood)):
                    error_reason = action_json.get("reason", "(原因未知)")
                    await self.god_print(f"【上帝(错误详情)】: [{current_player_obj.name}] 决策失败并强制弃牌，原因: {error_reason}", 0.5)
                # --- 调试块结束 ---

            cheat_context = await self._handle_cheat_move(game, current_player_idx, action_json.get("cheat_move"))

            secret_message_json = action_json.get("secret_message")
            if secret_message_json:
                await self._handle_secret_message(game, current_player_idx, secret_message_json)

            action_obj, error_msg = self._parse_action_json(game, action_json, current_player_idx, actions_list)
            if self._parse_warnings:
                for warning in self._parse_warnings:
                    await self.god_print(warning, 0.5)
                self._parse_warnings.clear()
            if error_msg:
                await self.god_print(error_msg, 0.5)
                action_obj = Action(player=current_player_idx, type=ActionType.FOLD)

            if action_obj.type == ActionType.ACCUSE:
                trial_happened = await self._handle_accusation(game, action_obj, start_player_id)
                if not game.state.finished:
                    game._handle_next_turn()
                continue

            player_speech = action_json.get("speech")
            self.player_last_speech[current_player_idx] = player_speech

            player_mood = action_json.get("mood", "未知")
            leak_probability = current_player_obj.get_mood_leak_probability()
            if random.random() < leak_probability:
                self.player_observed_moods[current_player_idx] = player_mood
                await self.god_print(f"【上帝视角】: {current_player_obj.name} 似乎泄露了一丝情绪: {player_mood}", 0.5)
            else:
                self.player_observed_moods.pop(current_player_idx, None)

            action_desc = f"{action_obj.type.name}"
            if action_obj.amount: action_desc += f" (加注 {action_obj.amount})"
            if action_obj.target is not None: action_desc += f" (目标 {self.players[action_obj.target].name})"
            await self.god_print(f"[{current_player_obj.name} 动作]: {action_desc}", 1.5)

            if player_speech:
                await self.god_print(f"[{current_player_obj.name} 发言]: {player_speech}", 1)

            try:
                game.step(action_obj)
                await self.god_panel_update(self._build_panel_data(game, start_player_id))
            except Exception as e:
                await self.god_print(f"!! 动作执行失败: {e}。强制玩家 {current_player_obj.name} 弃牌。", 0)
                if not game.state.finished:
                    game.step(Action(player=current_player_idx, type=ActionType.FOLD))
                    await self.god_panel_update(self._build_panel_data(game, start_player_id))

            current_player_obj.update_experience_after_action(
                action_json,
                cheat_context,
                call_cost,
                game.state.pot
            )

            if action_obj.type == ActionType.LOOK and not game.state.finished:
                await self.god_print(f"{current_player_obj.name} 刚刚看了牌，现在轮到他/她再次行动...", 1)
                continue

            await asyncio.sleep(1)

        if not game.state.finished:
            game._force_showdown()

        await self.god_print(f"--- 本手结束 ---", 1)
        winner_name = "N/A"
        if game.state.winner is not None:
            winner_name = self.players[game.state.winner].name
            await self.god_print(f"赢家是 {winner_name}!", 1)
            self.last_winner_id = game.state.winner
        else:
            await self.god_print("没有赢家 (流局)。", 1)

        await self.god_print("--- 最终亮牌 (上帝视角已在看板) ---", 1)
        await self.god_panel_update(self._build_panel_data(game, start_player_id))
        await self.god_print("--- 本手筹码结算 ---", 1)
        for i, p_state in enumerate(game.state.players):
            old_chips = self.persistent_chips[i]
            new_chips = p_state.chips
            self.persistent_chips[i] = new_chips
            await self.god_print(f"  {self.players[i].name}: {old_chips} -> {new_chips}", 0.5)
        await self.god_panel_update(self._build_panel_data(None, -1))

        # --- [新] 经验系统 V2：调用获胜者奖励 ---
        winner_id = game.state.winner
        final_pot_size = game.state.pot_at_showdown  # (来自步骤一)

        if winner_id is not None and final_pot_size > 0:
            winner_obj = self.players[winner_id]
            if winner_obj.alive:
                winner_obj.update_experience_from_win(final_pot_size)
                await self.god_print(
                    f"【上帝(经验)】: {winner_obj.name} (获胜者) 额外获得 {(5.0 + min(final_pot_size * 0.1, 20.0)):.1f} 点经验 (来自底池奖励)",
                    0.5
                )
        # --- [新] 插入结束 ---

        await self.god_print(f"--- LLM 人设发言开始 (同时私下更新笔记) ---", 1)
        final_state_data = game.export_state(view_player=None)
        round_history_json = json.dumps(final_state_data['history'], indent=2, ensure_ascii=False)
        round_result_str = f"赢家是 {winner_name}"

        new_impressions_map = {}

        for i, player in enumerate(self.players):
            if self.persistent_chips[i] > 0 and self.players[i].alive:

                current_player_impressions = self.player_private_impressions.get(i, {})
                opponent_impressions_data = {}
                for opponent_id, impression_text in current_player_impressions.items():
                    if opponent_id != i:
                        opponent_name = self.players[opponent_id].name
                        opponent_impressions_data[opponent_name] = impression_text

                current_impressions_json_str = json.dumps(opponent_impressions_data, indent=2, ensure_ascii=False)

                (reflection_text, private_impressions_dict) = await player.reflect(
                    round_history_json,
                    round_result_str,
                    current_impressions_json_str,
                    stream_start_cb=self.god_stream_start,
                    stream_chunk_cb=self.god_stream_chunk
                )

                self.player_reflections[i] = reflection_text
                new_impressions_map[i] = private_impressions_dict
                player.update_experience_from_reflection(reflection_text, private_impressions_dict)
                await asyncio.sleep(0.5)

        for player_id, impressions_dict in new_impressions_map.items():
            if not isinstance(impressions_dict, dict): continue
            current_player_impressions = self.player_private_impressions.get(player_id, {})
            for opponent_name, impression_text in impressions_dict.items():
                found_opponent_id = -1
                for opp_id, opp_player in enumerate(self.players):
                    if opp_player.name == opponent_name:
                        found_opponent_id = opp_id
                        break
                if found_opponent_id != -1 and found_opponent_id != player_id:
                    current_player_impressions[found_opponent_id] = impression_text
            self.player_private_impressions[player_id] = current_player_impressions
