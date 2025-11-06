import time
import json
import asyncio
import random  # (新) 1. 导入 random (修复 Bug)
from typing import List, Dict, Callable, Awaitable

from zhajinhua import ZhajinhuaGame, GameConfig, Action
from game_rules import ActionType, INT_TO_RANK, SUITS, GameConfig, evaluate_hand
from player import Player


class GameController:
    """
    (已修改：手牌排序和牌型评估)
    (已修改：增加 'is_dealer' (庄家) 标记)
    (已修改：增加 'mood' (情绪) 泄露机制)
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
        self.player_reflections: Dict[int, str] = {}
        self.player_observed_moods: Dict[int, str] = {}

    def get_alive_player_count(self) -> int:
        return sum(1 for chips in self.persistent_chips if chips > 0)

    def _build_panel_data(self, game: ZhajinhuaGame | None, start_player_id: int = -1) -> dict:
        # (此函数无修改)
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
                        sorted_hand = sorted(p_state.hand, key=lambda c: c.rank, reverse=True)
                        hand_str = ' '.join([INT_TO_RANK[c.rank] + SUITS[c.suit] for c in sorted_hand])
                    else:
                        hand_str = "..."
            players_data.append({
                "id": i,
                "name": p.name,
                "chips": player_chips,
                "hand_str": hand_str,
                "looked": player_looked,
                "is_active": player_is_active,
                "is_dealer": is_dealer
            })
        return {
            "hand_count": self.hand_count,
            "current_pot": game.state.pot if game and game.state else 0,
            "players": players_data
        }

    async def run_game(self):
        # (此函数无修改)
        await self.god_print(f"--- 锦标赛开始 ---", 1)
        await self.god_print(f"初始筹码: {self.persistent_chips}", 1)
        await self.god_panel_update(self._build_panel_data(None, -1))
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
            await self.run_round(start_player_id)
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
        # (此函数无修改)
        st = game.state
        ps = st.players[player_id]
        state_summary_lines = [
            f"当前是 {self.players[st.current_player].name} 的回合。",
            f"底池 (Pot): {st.pot}", f"当前暗注 (Base Bet): {st.current_bet}",
            f"最后加注者: {self.players[st.last_raiser].name if st.last_raiser is not None else 'N/A'}"
        ]
        state_summary_lines.append("\n玩家信息:")
        for i, p in enumerate(st.players):
            p_name = self.players[i].name
            if self.persistent_chips[i] <= 0:
                status = "已淘汰"
            elif not game.state.players[i].alive:
                status = "已弃牌"
            elif game.state.players[i].looked:
                status = "已看牌"
            else:
                status = "未看牌"
            state_summary_lines.append(f"  - {p_name}: 筹码={p.chips}, 状态={status}")
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
            if act_type == ActionType.CALL:
                call_cost = display_cost
            available_actions_tuples.append((act_type.name, display_cost))
        available_actions_str = "\n".join(
            f"  - {name}: 成本={cost}"
            for name, cost in available_actions_tuples
        )
        next_player_id = game.next_player(start_from=player_id)
        next_player_name = self.players[next_player_id].name
        reflection_lines = []
        for i, p in enumerate(self.players):
            if i == player_id: continue
            reflection = self.player_reflections.get(i)
            if reflection:
                reflection_lines.append(f"  - {p.name}: {reflection}")
        if not reflection_lines:
            opponent_reflections_str = "暂无对手的过往人设信息。"
        else:
            opponent_reflections_str = "\n".join(reflection_lines)
        mood_lines = []
        for i, p in enumerate(self.players):
            if i == player_id: continue
            mood = self.player_observed_moods.get(i)
            if mood:
                mood_lines.append(f"  - {p.name} 看起来: {mood}")
        if not mood_lines:
            observed_moods_str = "暂未观察到对手的明显情绪。"
        else:
            observed_moods_str = "\n".join(mood_lines)
        min_raise_increment = st.config.min_raise
        dealer_name = self.players[start_player_id].name
        multiplier = 2 if ps.looked else 1
        return "\n".join(
            state_summary_lines), my_hand, available_actions_str, available_actions_tuples, next_player_name, opponent_reflections_str, min_raise_increment, dealer_name, observed_moods_str, multiplier, call_cost

    def _parse_action_json(self, game: ZhajinhuaGame, action_json: dict, player_id: int,
                           available_actions: list) -> (Action, str):
        # (此函数无修改)
        action_name = action_json.get("action", "FOLD").upper()
        selected_action_tuple = None
        for (name, cost) in available_actions:
            if name == action_name:
                selected_action_tuple = (name, cost)
                break
        if selected_action_tuple is None:
            error_msg = f"警告: {self.players[player_id].name} 选择了无效动作 '{action_name}'，强制弃牌。"
            return Action(player=player_id, type=ActionType.FOLD), error_msg
        action_type = ActionType[action_name]
        amount = None
        target = None
        if action_type == ActionType.RAISE:
            min_inc = game.state.config.min_raise
            try:
                amount_increment_str = action_json.get("amount")
                amount = int(amount_increment_str)
                if amount < min_inc:
                    error_msg = f"警告: {self.players[player_id].name} 试图加注 {amount}，小于最小增量 {min_inc}。强制弃牌。"
                    return Action(player=player_id, type=ActionType.FOLD), error_msg
            except (ValueError, TypeError):
                error_msg = f"警告: {self.players[player_id].name} RAISE 动作未提供有效的 'amount'。强制弃牌。"
                return Action(player=player_id, type=ActionType.FOLD), error_msg
        if action_type == ActionType.COMPARE:
            target_name = action_json.get("target_name")
            if target_name is None:
                error_msg = f"警告: {self.players[player_id].name} 选择比牌但未指定 target_name，强制弃牌。"
                return Action(player=player_id, type=ActionType.FOLD), error_msg
            found = False
            for i, p in enumerate(self.players):
                if p.name.strip() == target_name.strip():
                    if game.state.players[i].alive:
                        target = i
                        found = True
                        break
                    else:
                        error_msg = f"警告: {self.players[player_id].name} 试图与已弃牌的 {target_name} 比牌，强制弃牌。"
                        return Action(player=player_id, type=ActionType.FOLD), error_msg
            if not found:
                error_msg = f"警告: {self.players[player_id].name} 指定了无效的比牌目标 '{target_name}'，强制弃牌。"
                return Action(player=player_id, type=ActionType.FOLD), error_msg
        return Action(player=player_id, type=action_type, amount=amount, target=target), ""

    async def run_round(self, start_player_id: int):
        """(新) 接收 start_player_id"""
        config = GameConfig(num_players=self.num_players)
        game = ZhajinhuaGame(config, self.persistent_chips, start_player_id)
        self.player_observed_moods.clear()
        await self.god_panel_update(self._build_panel_data(game, start_player_id))
        for i, p in enumerate(game.state.players):
            if self.persistent_chips[i] <= 0:
                p.alive = False
            if not p.alive and self.persistent_chips[i] > 0:
                await self.god_print(
                    f"玩家 {self.players[i].name} 筹码 ({self.persistent_chips[i]}) 不足支付底注 ({config.base_bet})，本手自动弃牌。",
                    0.5)
        await self.god_print("--- 初始发牌 (上帝视角已在看板) ---", 1)
        while not game.state.finished:
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
             next_player_name, opponent_reflections_str,
             min_raise_increment, dealer_name,
             observed_moods_str, multiplier, call_cost) = self._build_llm_prompt(game, current_player_idx,
                                                                                 start_player_id)

            try:
                action_json = await current_player_obj.decide_action(
                    state_summary, my_hand, actions_str, next_player_name,
                    opponent_reflections_str,
                    min_raise_increment,
                    dealer_name,
                    observed_moods_str,
                    multiplier,
                    call_cost,
                    stream_start_cb=self.god_stream_start,
                    stream_chunk_cb=self.god_stream_chunk
                )
            except Exception as e:
                await self.god_print(f"!! 玩家 {current_player_obj.name} 决策失败: {e}。强制弃牌。", 0)
                action_json = {"action": "FOLD", "reason": "决策系统崩溃", "target_name": None, "mood": "崩溃"}
            player_mood = action_json.get("mood", "未知")
            MOOD_LEAK_CHANCE = 0.33
            if random.random() < MOOD_LEAK_CHANCE:
                self.player_observed_moods[current_player_idx] = player_mood
                await self.god_print(f"【上帝视角】: {current_player_obj.name} 似乎泄露了一丝情绪: {player_mood}", 0.5)
            else:
                self.player_observed_moods.pop(current_player_idx, None)
            action_obj, error_msg = self._parse_action_json(game, action_json, current_player_idx, actions_list)
            if error_msg: await self.god_print(error_msg, 0.5)
            action_desc = f"{action_obj.type.name}"
            if action_obj.amount: action_desc += f" (加注 {action_obj.amount})"
            if action_obj.target is not None:
                action_desc += f" (目标 {self.players[action_obj.target].name})"
            await self.god_print(f"[{current_player_obj.name} 动作]: {action_desc}", 1.5)
            try:
                game.step(action_obj)
                await self.god_panel_update(self._build_panel_data(game, start_player_id))
            except Exception as e:
                await self.god_print(f"!! 动作执行失败: {e}。强制玩家 {current_player_obj.name} 弃牌。", 0)
                if not game.state.finished:
                    game.step(Action(player=current_player_idx, type=ActionType.FOLD))
                    await self.god_panel_update(self._build_panel_data(game, start_player_id))
            if action_obj.type == ActionType.LOOK and not game.state.finished:
                await self.god_print(f"{current_player_obj.name} 刚刚看了牌，现在轮到他/她再次行动...", 1)
                continue
            await asyncio.sleep(1)
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
        await self.god_print("--- LLM 人设发言开始 ---", 1)
        final_state_data = game.export_state(view_player=None)
        round_history_json = json.dumps(final_state_data['history'], indent=2, ensure_ascii=False)
        round_result_str = f"赢家是 {winner_name}"
        for i, player in enumerate(self.players):
            if self.persistent_chips[i] > 0 and player.alive:
                reflection_text = await player.reflect(
                    round_history_json,
                    round_result_str,
                    stream_start_cb=self.god_stream_start,
                    stream_chunk_cb=self.god_stream_chunk
                )
                self.player_reflections[i] = reflection_text
                await asyncio.sleep(0.5)
