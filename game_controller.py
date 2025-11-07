import time
import json
import asyncio
import random
from pathlib import Path
from typing import List, Dict, Callable, Awaitable, Tuple, Optional

from zhajinhua import ZhajinhuaGame, GameConfig, Action
from game_rules import ActionType, INT_TO_RANK, SUITS, GameConfig, evaluate_hand, Card, RANK_TO_INT, HandType
from player import Player

BASE_DIR = Path(__file__).parent.resolve()
ITEM_STORE_PATH = BASE_DIR / "items_store.json"
AUCTION_PROMPT_PATH = BASE_DIR / "prompt/auction_bid_prompt.txt"


class SystemVault:
    """金库逻辑：根据经验和信誉评估贷款请求。"""

    def __init__(self, base_interest_rate: float = 0.16):
        self.base_interest_rate = base_interest_rate

    def get_max_loan(self, experience: float) -> int:
        baseline = 400
        experience_bonus = int(min(max(experience, 0.0) * 25, 3000))
        return baseline + experience_bonus

    def assess_loan_request(self, player: Player, amount: int, turns: int) -> Dict[str, object]:
        if player.loan_data:
            return {"approved": False, "reason": "你仍有未清贷款，必须先归还。"}

        if amount <= 0:
            return {"approved": False, "reason": "贷款金额必须大于 0。"}

        max_amount = self.get_max_loan(player.experience)
        if amount > max_amount:
            return {
                "approved": False,
                "reason": f"额度不足。以你当前的经验值，最高可贷 {max_amount}。"
            }

        approved_turns = max(2, min(6, int(turns or 0)))
        if turns is None or turns <= 0:
            approved_turns = 3

        if approved_turns < 2:
            return {"approved": False, "reason": "贷款最少需要 2 手牌后归还。"}

        interest_rate = self.base_interest_rate + max(0.0, (0.35 - min(player.experience, 120.0) / 400.0))
        interest_rate = min(0.45, interest_rate)
        due_amount = int(amount * (1 + interest_rate))

        return {
            "approved": True,
            "amount": amount,
            "due_amount": due_amount,
            "due_in_hands": approved_turns,
            "interest_rate": round(interest_rate, 3),
            "reason": (
                f"批准贷款 {amount}，利率 {interest_rate:.2%}，"
                f"请在 {approved_turns} 手内归还共 {due_amount} 筹码。"
            )
        }


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

        try:
            with ITEM_STORE_PATH.open("r", encoding="utf-8") as fp:
                self.item_catalog: Dict[str, Dict[str, object]] = json.load(fp)
        except FileNotFoundError:
            self.item_catalog = {}
            print(f"【上帝(警告)】: 未找到 {ITEM_STORE_PATH.name}，拍卖行暂不可用。")
        except json.JSONDecodeError as exc:
            self.item_catalog = {}
            print(f"【上帝(错误)】: 解析 {ITEM_STORE_PATH.name} 失败: {exc}。")

        self.vault = SystemVault()
        self.active_effects: List[Dict[str, object]] = []

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

        self.player_system_messages: Dict[int, List[str]] = {i: [] for i in range(self.num_players)}
        self._hand_starting_chips: List[int] = [default_chips] * self.num_players
        self._hand_start_persistent: List[int] = list(self.persistent_chips)
        self._current_ante_distribution: List[int] = [0] * self.num_players
        self._redeal_requested: bool = False
        self._queued_messages: List[tuple[str, float]] = []

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
            inventory_names: list[str] = []
            for owned_id in p.inventory:
                owned_info = self.item_catalog.get(owned_id, {})
                display_name = owned_info.get("name", owned_id)
                inventory_names.append(f"{display_name} ({owned_id})")
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
                "pressure_state": p.get_pressure_descriptor(),
                "inventory": inventory_names,
                "inventory_count": len(inventory_names)
            })
        return {
            "hand_count": self.hand_count,
            "current_pot": game.state.pot if game and game.state else 0,
            "players": players_data
        }

    def _select_item_for_auction(self) -> tuple[str, Dict[str, object]]:
        if not self.item_catalog:
            raise ValueError("item catalog empty")
        items = list(self.item_catalog.items())
        weights = [max(1, int(info.get("auction_weight", 1))) for _, info in items]
        index = random.choices(range(len(items)), weights=weights, k=1)[0]
        return items[index]

    def _find_player_by_name(self, name: str) -> Optional[int]:
        for idx, player in enumerate(self.players):
            if player.name.strip() == (name or "").strip():
                return idx
        return None

    def _get_effects_for_player(self, player_id: int) -> List[Dict[str, object]]:
        return [effect for effect in self.active_effects if effect.get("target_id") == player_id]

    def _clear_system_messages(self) -> None:
        for msg_list in self.player_system_messages.values():
            msg_list.clear()

    def _append_system_message(self, player_id: int, message: str) -> None:
        if player_id not in self.player_system_messages:
            self.player_system_messages[player_id] = []
        self.player_system_messages[player_id].append(message)

    def _queue_message(self, text: str, delay: float = 0.5) -> None:
        self._queued_messages.append((text, delay))

    async def _flush_queued_messages(self) -> None:
        while self._queued_messages:
            text, delay = self._queued_messages.pop(0)
            await self.god_print(text, delay)

    def _find_effect(self, player_id: int, effect_id: str) -> Optional[Dict[str, object]]:
        for effect in self.active_effects:
            if effect.get("target_id") == player_id and effect.get("effect_id") == effect_id:
                return effect
        return None

    def _consume_effect(self, player_id: int, effect_id: str) -> Optional[Dict[str, object]]:
        effect = self._find_effect(player_id, effect_id)
        if effect:
            try:
                self.active_effects.remove(effect)
            except ValueError:
                pass
        return effect

    def _player_has_effect(self, player_id: int, effect_id: str) -> bool:
        return self._find_effect(player_id, effect_id) is not None

    def _get_visible_chips(self, viewer_id: int, subject_id: int, actual_chips: int) -> str:
        if viewer_id != subject_id and self._player_has_effect(subject_id, "chip_invisible"):
            return "???"
        return str(actual_chips)

    def _format_card(self, card: Card) -> str:
        return INT_TO_RANK[card.rank] + SUITS[card.suit]

    def _get_next_active_player(self, game: ZhajinhuaGame, start_idx: int) -> Optional[int]:
        st = game.state
        candidate = start_idx
        for _ in range(self.num_players):
            candidate = (candidate + 1) % self.num_players
            player_state = st.players[candidate]
            if player_state.alive and not player_state.all_in:
                return candidate
        return None

    def _check_peek_blockers(self, attacker_id: int, target_id: int) -> tuple[bool, Optional[str]]:
        attacker_name = self.players[attacker_id].name
        target_name = self.players[target_id].name

        reflect_effect = self._consume_effect(target_id, "peek_reflect")
        if reflect_effect:
            self._append_system_message(target_id, f"{attacker_name} 试图窥探你，但被反窥镜识破。")
            self._queue_message(
                f"【安保反制】{target_name} 的反窥镜反弹了 {attacker_name} 的窥探，并暴露了对方身份。",
                0.5
            )
            return True, f"反窥镜反弹，{attacker_name} 行动失败"

        if self._player_has_effect(target_id, "anti_peek_once"):
            return True, f"{target_name} 被反侦测烟雾笼罩，窥探失败。"

        if self._player_has_effect(target_id, "peek_shield"):
            return True, f"{target_name} 处于屏蔽状态，窥探失败。"

        return False, None

    def _record_hand_start_state(self, game: ZhajinhuaGame) -> None:
        self._hand_starting_chips = [ps.chips for ps in game.state.players]

    def _apply_luck_boost(self, game: ZhajinhuaGame, player_id: int) -> None:
        effect = self._consume_effect(player_id, "luck_boost")
        if not effect:
            return

        player_state = game.state.players[player_id]
        if not player_state.hand or not game.state.deck:
            return

        hand_rank = evaluate_hand(player_state.hand)
        if hand_rank.hand_type >= HandType.PAIR:
            # 已经不错了，不再调整
            return

        lowest_index = min(range(len(player_state.hand)), key=lambda idx: player_state.hand[idx].rank)
        deck = game.state.deck
        high_card_index = None
        for idx, card in enumerate(deck):
            if card.rank >= RANK_TO_INT["J"]:
                high_card_index = idx
                break
        if high_card_index is None and deck:
            high_card_index = 0
        if high_card_index is None:
            return

        new_card = deck.pop(high_card_index)
        old_card = player_state.hand[lowest_index]
        player_state.hand[lowest_index] = new_card
        deck.append(old_card)
        random.shuffle(deck)

        self._append_system_message(
            player_id,
            f"幸运币发挥作用，将 {self._format_card(old_card)} 替换成了 {self._format_card(new_card)}。"
        )
        self._queue_message(
            f"【道具生效】{self.players[player_id].name} 的幸运币闪耀，手牌被系统重新调整。",
            0.5
        )

    def _apply_bad_luck_guard(self, game: ZhajinhuaGame, player_id: int) -> None:
        effect = self._find_effect(player_id, "bad_luck_guard")
        if not effect:
            return

        data = effect.setdefault("data", {})
        streak = int(data.get("streak", 0))
        player_state = game.state.players[player_id]
        hand_rank = evaluate_hand(player_state.hand)

        def is_bad_hand() -> bool:
            if hand_rank.hand_type == HandType.HIGH_CARD:
                highest_rank = max(card.rank for card in player_state.hand)
                return highest_rank < RANK_TO_INT["Q"]
            return False

        if is_bad_hand():
            streak += 1
            if streak >= 3:
                deck = game.state.deck
                if len(deck) >= 3:
                    deck.extend(player_state.hand)
                    random.shuffle(deck)
                    player_state.hand = [deck.pop() for _ in range(3)]
                    new_rank = evaluate_hand(player_state.hand)
                    self._append_system_message(
                        player_id,
                        "护运珠触发，系统重新发给你一手新牌。"
                    )
                    self._queue_message(
                        f"【道具生效】护运珠阻止了第 3 次烂牌，{self.players[player_id].name} 获得了新手牌 (牌型: {new_rank.hand_type.name})。",
                        0.5
                    )
                    streak = 0
            data["streak"] = streak
        else:
            data["streak"] = 0

    def _apply_start_of_hand_effects(self, game: ZhajinhuaGame) -> None:
        for idx, ps in enumerate(game.state.players):
            if not ps.alive:
                continue
            self._apply_luck_boost(game, idx)
            self._apply_bad_luck_guard(game, idx)

    def _handle_compare_resolution(self, game: ZhajinhuaGame, attacker: int, defender: int,
                                   result: int, loser: int) -> dict:
        attacker_name = self.players[attacker].name
        defender_name = self.players[defender].name

        decline_effect = self._consume_effect(defender, "compare_decline")
        if decline_effect:
            self._append_system_message(defender, "免比符触发，本次比牌已拒绝。")
            self._queue_message(
                f"【道具生效】{defender_name} 启动了免比符，拒绝与 {attacker_name} 比牌。",
                0.5
            )
            return {"action": "cancel"}

        reverse_owner: Optional[int] = None
        reverse_effect = self._consume_effect(attacker, "compare_reverse")
        if not reverse_effect:
            reverse_effect = self._consume_effect(defender, "compare_reverse")
            if reverse_effect:
                reverse_owner = defender
        else:
            reverse_owner = attacker

        final_loser = loser
        if reverse_owner is not None:
            final_loser = attacker if loser == defender else defender
            owner_name = self.players[reverse_owner].name
            self._queue_message(
                f"【道具生效】{owner_name} 使用了反转卡，当前比牌结果被颠倒。",
                0.5
            )

        if result == 0:
            return {"loser": None}

        if final_loser is None:
            return {}

        if self._consume_effect(final_loser, "compare_draw"):
            self._queue_message(
                f"【道具生效】{self.players[final_loser].name} 的护牌罩触发，本次比牌改判为平局。",
                0.5
            )
            return {"action": "draw"}

        if self._consume_effect(final_loser, "compare_second_chance"):
            self._queue_message(
                f"【道具生效】{self.players[final_loser].name} 的免死金牌发动，逃过此次比牌淘汰。",
                0.5
            )
            return {"action": "draw"}

        return {"loser": final_loser}

    def _apply_post_hand_effects(self, game: ZhajinhuaGame, winner_id: Optional[int],
                                 final_pot_size: int) -> List[tuple[str, float]]:
        messages: List[tuple[str, float]] = []

        if winner_id is not None and final_pot_size > 0:
            if self._consume_effect(winner_id, "double_win"):
                game.state.players[winner_id].chips += final_pot_size
                messages.append(
                    (f"【道具结算】{self.players[winner_id].name} 的双倍卡生效，额外赢得 {final_pot_size} 筹码。", 0.5)
                )

            bonus_effect = self._find_effect(winner_id, "win_bonus")
            if bonus_effect:
                ratio = bonus_effect.get("bonus_ratio", 0.25)
                bonus_amount = max(20, int(final_pot_size * ratio))
                game.state.players[winner_id].chips += bonus_amount
                messages.append(
                    (f"【道具结算】财神符赐福，{self.players[winner_id].name} 额外获得 {bonus_amount} 筹码。", 0.5)
                )

        for idx in range(self.num_players):
            effect = self._find_effect(idx, "win_streak_boost")
            if not effect:
                continue
            data = effect.setdefault("data", {})
            streak = int(data.get("streak", 0))
            if winner_id is not None and idx == winner_id:
                streak += 1
                if streak >= 3 and final_pot_size > 0:
                    game.state.players[idx].chips += final_pot_size
                    messages.append(
                        (f"【道具结算】{self.players[idx].name} 连胜三局，收益翻倍再得 {final_pot_size} 筹码。", 0.5)
                    )
                    streak = 0
                data["streak"] = streak
            else:
                data["streak"] = 0

        for effect in list(self.active_effects):
            if effect.get("effect_id") != "loss_refund":
                continue
            hand_id = effect.get("hand_id")
            player_id = effect.get("target_id")
            if hand_id != self.hand_count or player_id is None:
                continue
            refund_amount = int(effect.get("refund", 0))
            if refund_amount > 0:
                start_chips = self._hand_starting_chips[player_id]
                end_chips = game.state.players[player_id].chips
                if end_chips < start_chips:
                    game.state.players[player_id].chips += refund_amount
                    messages.append(
                        (f"【道具结算】定输免赔返还 {refund_amount} 筹码给 {self.players[player_id].name}。", 0.5)
                    )
            self.active_effects.remove(effect)

        return messages

    async def _run_auction_phase(self):
        if not self.item_catalog:
            return

        eligible_players = [
            idx for idx in range(self.num_players)
            if self.players[idx].alive and self.persistent_chips[idx] > 0
        ]
        if len(eligible_players) <= 1:
            return

        try:
            item_id, item_info = self._select_item_for_auction()
        except ValueError:
            return

        await self.god_print(
            f"【系统拍卖行】即将竞拍: {item_info.get('name', item_id)} ({item_id}) - {item_info.get('description', '')}",
            0.6
        )

        # --- 多轮拍卖核心逻辑 ---
        current_highest_bid = 1  # (新) 起拍价
        current_highest_bidder_id: Optional[int] = None
        active_bidders = set(eligible_players)

        max_auction_rounds = 5  # (新) 避免无限循环的硬上限
        round_count = 0
        last_bid_change_round = 0  # (新) 跟踪最后一次加价的回合

        while round_count < max_auction_rounds and len(active_bidders) > 1:
            round_count += 1
            await self.god_print(f"--- 拍卖第 {round_count}/{max_auction_rounds} 轮 ---", 0.5)

            leader_name = self.players[
                current_highest_bidder_id].name if current_highest_bidder_id is not None else '无人'
            await self.god_print(f"当前最高价: {current_highest_bid} (来自: {leader_name})", 0.5)

            players_to_ask = list(active_bidders)
            players_who_folded = set()
            new_bid_made_this_round = False

            for player_id in players_to_ask:
                # 已经是最高出价者，跳过
                if player_id == current_highest_bidder_id:
                    continue

                try:
                    stream_prefix = f"【系统拍卖行】[{self.players[player_id].name}] (等待出价...): "
                    result = await self._get_player_bid(
                        player_id, item_id, item_info, eligible_players, stream_prefix,
                        current_highest_bid  # (新) 传入当前最高价
                    )

                    # --- [密信修复]：处理拍卖中的密信 ---
                    secret_message = result.get("secret_message")
                    if secret_message:
                        # (新) game 传 None，因为拍卖不在牌局内
                        await self._handle_secret_message(None, player_id, secret_message)
                    # --- [修复结束] ---

                    bid_amount = int(result.get("bid", 0))

                except Exception:
                    await self.god_print(
                        f"【上帝(警告)】: {self.players[player_id].name} 的拍卖决策失败，视为弃权。", 0.5
                    )
                    result = {"bid": 0}  # 失败等于弃权

                bid_amount = int(result.get("bid", 0))

                if bid_amount > current_highest_bid:
                    # 这是一个有效的加注
                    current_highest_bid = bid_amount
                    current_highest_bidder_id = player_id
                    new_bid_made_this_round = True
                    last_bid_change_round = round_count  # (新) 重置僵局计时器
                    await self.god_print(
                        f"【拍卖行】{self.players[player_id].name} 出价 {bid_amount}！", 0.5
                    )
                else:
                    # 出价 0 或无效出价 (<= 最高价)
                    if bid_amount > 0:
                        await self.god_print(
                            f"【拍卖行】{self.players[player_id].name} 出价 {bid_amount} 低于或等于当前价格，视为放弃。", 0.4
                        )
                    players_who_folded.add(player_id)

            # 移除本轮放弃的玩家
            active_bidders.difference_update(players_who_folded)

            # (新) 检查是否只剩一人
            if len(active_bidders) == 1:
                winner_id = list(active_bidders)[0]
                # 确保获胜者是最高出价者 (如果最后一人没出价，但其他人全弃权了)
                if winner_id == current_highest_bidder_id:
                    await self.god_print(f"其他玩家均已放弃。", 0.5)
                    break
                # 如果最高出价者自己也弃权了，而其他人也弃权了，那么这个唯一的幸存者必须至少匹配价格
                if current_highest_bidder_id not in active_bidders:
                    # 这种情况很罕见，但意味着最后一人必须出价
                    pass  # 循环将再次询问他

            # (新) 僵局检查：如果一整轮无人加价 (即 last_bid_change_round 没在本轮更新)
            if not new_bid_made_this_round and round_count > 1:
                await self.god_print(f"一轮无人跟注，拍卖即将结束...", 0.5)
                # 确保最高出价者仍在
                if current_highest_bidder_id in active_bidders:
                    break  # 僵局导致拍卖结束

            # (新) 硬上限检查
            if round_count >= max_auction_rounds:
                await self.god_print(f"达到 {max_auction_rounds} 轮硬上限，拍卖结束。", 0.5)
                break

            await asyncio.sleep(0.5)  # 每轮之间稍作停顿

        # --- 拍卖结束，结算 ---
        if current_highest_bidder_id is None or current_highest_bid <= 1:
            await self.god_print("【系统拍卖行】无人出价，本次流拍。", 0.5)
            return

        winner_id = current_highest_bidder_id
        winning_bid = current_highest_bid

        self.persistent_chips[winner_id] -= winning_bid
        self.players[winner_id].inventory.append(item_id)

        await self.god_print(
            f"【系统拍卖行】{self.players[winner_id].name} 以 {winning_bid} 筹码拍得 "
            f"{item_info.get('name', item_id)} ({item_id})。",
            1
        )
        await self.god_panel_update(self._build_panel_data(None, -1))

    async def _get_player_bid(self, player_id: int, item_id: str, item_info: Dict[str, object],
                              bidder_ids: List[int], stream_prefix: Optional[str] = None,
                              current_highest_bid: int = 0) -> Dict[str, object]:  # (新) 增加 current_highest_bid
        player = self.players[player_id]
        try:
            template = AUCTION_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"player_id": player_id, "bid": 0}

        inventory_names = []
        for owned_id in player.inventory:
            owned_info = self.item_catalog.get(owned_id)
            if owned_info:
                inventory_names.append(f"{owned_info.get('name', owned_id)} ({owned_id})")
            else:
                inventory_names.append(owned_id)
        inventory_str = "空" if not inventory_names else ", ".join(inventory_names)

        other_lines = []
        for other_id in bidder_ids:
            if other_id == player_id:
                continue
            # (新) 在多轮拍卖中，对手状态应只显示仍在竞价的人
            # if other_id not in active_bidders: continue # (注意: active_bidders 不在此函数作用域)
            other_player = self.players[other_id]
            other_chips = self.persistent_chips[other_id]
            loan_info = other_player.loan_data
            loan_str = "有债务" if loan_info else "无债务"
            other_lines.append(
                f"  - {other_player.name}: 筹码 {other_chips}, 道具 {len(other_player.inventory)} 件, {loan_str}"
            )
        other_status = "\n".join(other_lines) if other_lines else "暂无竞争对手。"

        # --- [修复 1.1] (来自上一轮的修复)：计算安全筹码和真实可出价上限 ---
        current_chips = self.persistent_chips[player_id]
        _base, distribution, _total = self._build_ante_distribution()
        ante_cost = distribution[player_id]
        safety_buffer = max(ante_cost * 3, 20)
        max_bid_allowed = max(0, current_chips - safety_buffer)

        # --- [修复 1.2] (来自上一轮的修复)：将安全限制注入 Prompt ---
        my_assets_str = f"""- 你的总筹码: {current_chips}
    - 你的背包: {inventory_str}
    - 【!! 重要警告 !!】: 你必须为下局保留 {safety_buffer} 筹码 (约 3 倍底注) 用于上桌。
    - 【!! 你的实际可出价上限是: {max_bid_allowed} !!】"""

        # (新) 多轮拍卖的上下文
        auction_context_str = f"""- 当前最高出价: {current_highest_bid}
    - 你的出价必须高于此价格 (或出价 0 放弃)。"""

        # (新) 强制价值为 1 (来自上一轮的修复)
        item_value = "1 (请自行根据描述评估)"

        prompt = template.format(
            item_name=item_info.get("name", item_id),
            item_description=item_info.get("description", ""),
            item_value=item_value,
            my_assets_str=my_assets_str,
            other_bidders_status=other_status,
            auction_context=auction_context_str,  # (新)
            current_highest_bid=current_highest_bid  # (新)
        )

        messages = [{"role": "user", "content": prompt}]

        if stream_prefix:
            await self.god_stream_start(stream_prefix)

        async def _stream(chunk: str):
            if stream_prefix:
                await self.god_stream_chunk(chunk)

        try:
            response = await player.llm_client.chat_stream(messages, player.model_name, _stream)
        finally:
            if stream_prefix:
                await self.god_stream_chunk("\n")
        parsed = player._parse_first_valid_json(response) or {}

        try:
            bid_value = int(parsed.get("bid", 0))
        except (TypeError, ValueError):
            bid_value = 0

        # --- (新) 多轮拍卖的出价验证 ---

        if bid_value > 0 and bid_value <= current_highest_bid:
            # AI 试图出价，但价格无效 (太低)
            await _stream(
                f"\n【系统提示】: 出价 {bid_value} 未超过 {current_highest_bid}，视为放弃。"
            )
            bid_value = 0  # 强制视为放弃

        elif bid_value > current_highest_bid:
            # AI 试图加注，检查安全上限
            final_bid = max(0, min(bid_value, max_bid_allowed))

            if final_bid < bid_value:
                # AI 试图出价过高，被系统强制修正
                await _stream(
                    f"\n【系统修正】: AI 出价 {bid_value} 过高，"
                    f"已强制修正为 {final_bid} (保留 {safety_buffer} 筹码)。"
                )
                bid_value = final_bid

            # (新) 再次检查：如果修正后的价格不再高于当前最高价
            if bid_value <= current_highest_bid:
                await _stream(
                    f"\n【系统提示】: 修正后的出价 {bid_value} 已无力超越 {current_highest_bid}，视为放弃。"
                )
                bid_value = 0  # 强制视为放弃

        # 此时，bid_value 要么是 0 (放弃)，要么是 > current_highest_bid 且 <= max_bid_allowed

        return {
            "player_id": player_id,
            "bid": bid_value,
            "reason": parsed.get("reason"),
            "mood": parsed.get("mood"),
            "cheat_move": None,
            # (新) 启用密信: 从 AI 的 JSON 中解析
            "secret_message": parsed.get("secret_message") if isinstance(parsed.get("secret_message"), dict) else None,
            "raw": response
        }

    async def _process_turn_based_effects(self):
        if not self.active_effects:
            return

        expired: List[Dict[str, object]] = []
        for effect in self.active_effects:
            if effect.get("turns_left") is not None:
                effect["turns_left"] -= 1

        for effect in list(self.active_effects):
            if effect.get("turns_left") is not None and effect["turns_left"] <= 0:
                expired.append(effect)

        for effect in expired:
            self.active_effects.remove(effect)
            target_id = effect.get("target_id")
            if target_id is None:
                continue
            target_name = self.players[target_id].name
            effect_name = effect.get("effect_name", effect.get("effect_id", "未知效果"))
            await self.god_print(f"【道具效果结束】{target_name} 的 {effect_name} 已失效。", 0.5)

    async def _handle_item_effect(self, game: ZhajinhuaGame, player_id: int, item_payload: Dict[str, object]) -> \
            Optional[Dict[str, object]]:
        if not isinstance(item_payload, dict):
            await self.god_print(f"【系统提示】道具使用数据无效，操作被忽略。", 0.5)
            return None

        item_id = item_payload.get("item_id")
        if not item_id:
            await self.god_print(f"【系统提示】未指定要使用的道具。", 0.5)
            return None

        player = self.players[player_id]
        if item_id not in player.inventory:
            await self.god_print(f"【系统提示】{player.name} 试图使用未持有的道具 {item_id}。", 0.5)
            return None

        item_info = self.item_catalog.get(item_id, {})
        player_state = game.state.players[player_id]

        def consume_item() -> None:
            try:
                player.inventory.remove(item_id)
            except ValueError:
                pass

        result_flags: Dict[str, object] = {}

        if item_id == "ITM_001":  # 换牌卡
            if not player_state.hand or not game.state.deck:
                await self.god_print("【系统提示】牌堆不足，无法换牌。", 0.5)
                return None
            consume_item()
            try:
                card_index = int(item_payload.get("card_index", -1)) - 1
            except (TypeError, ValueError):
                card_index = -1
            if card_index not in range(len(player_state.hand)):
                card_index = random.randrange(len(player_state.hand))
            old_card = player_state.hand[card_index]
            game.state.deck.append(old_card)
            random.shuffle(game.state.deck)
            new_card = game.state.deck.pop()
            player_state.hand[card_index] = new_card
            self._append_system_message(
                player_id,
                f"换牌卡替换了 {self._format_card(old_card)} -> {self._format_card(new_card)}。"
            )
            await self.god_print(f"【道具生效】{player.name} 更换了一张手牌。", 0.5)
            return result_flags

        if item_id == "ITM_002":  # 窥牌镜
            target_name = item_payload.get("target_name")
            target_id = self._find_player_by_name(target_name) if target_name else None
            if target_id is None or not game.state.players[target_id].alive:
                await self.god_print("【系统提示】必须指定一名仍在局内的目标。", 0.5)
                return None
            consume_item()
            blocked, reason = self._check_peek_blockers(player_id, target_id)
            if blocked:
                await self.god_print(f"【道具受阻】{player.name} 的窥牌尝试失败：{reason}", 0.5)
                return result_flags
            target_hand = game.state.players[target_id].hand
            if not target_hand:
                await self.god_print("【系统提示】目标暂无可窥视的手牌。", 0.5)
                return result_flags
            try:
                card_index = int(item_payload.get("card_index", -1)) - 1
            except (TypeError, ValueError):
                card_index = -1
            if card_index not in range(len(target_hand)):
                card_index = random.randrange(len(target_hand))
            peek_card = target_hand[card_index]
            card_str = self._format_card(peek_card)
            self._append_system_message(player_id, f"窥牌镜看到 {self.players[target_id].name} 的 {card_str}。")
            await self.god_print(f"【道具生效】{player.name} 使用窥牌镜窥视了 {self.players[target_id].name} 的一张暗牌。", 0.5)
            return result_flags

        if item_id == "ITM_003":  # 锁筹卡
            target_name = item_payload.get("target_name")
            target_id = self._find_player_by_name(target_name) if target_name else None
            if target_id is None or not game.state.players[target_id].alive:
                await self.god_print("【系统提示】锁筹卡需要指定一名仍在牌局中的对手。", 0.5)
                return None
            consume_item()
            effect_payload = {
                "effect_id": "lock_raise",
                "effect_name": item_info.get("name", "锁筹卡"),
                "source_id": player_id,
                "target_id": target_id,
                "turns_left": 1,
                "category": "debuff",
                "expires_after_action": True
            }
            self.active_effects.append(effect_payload)
            await self.god_print(
                f"【道具生效】{player.name} 对 {self.players[target_id].name} 使用了锁筹卡，其下一次行动无法 RAISE。",
                0.5
            )
            return result_flags

        if item_id == "ITM_004":  # 双倍卡
            consume_item()
            self.active_effects.append({
                "effect_id": "double_win",
                "effect_name": item_info.get("name", "双倍卡"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "hand_id": self.hand_count,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 激活双倍卡，若本局获胜将额外翻倍收益。", 0.5)
            return result_flags

        if item_id == "ITM_005":  # 免死金牌
            consume_item()
            self.active_effects.append({
                "effect_id": "compare_second_chance",
                "effect_name": item_info.get("name", "免死金牌"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 装备免死金牌，下一次比牌失败时可死里逃生。", 0.5)
            return result_flags

        if item_id == "ITM_006":  # 偷看卡
            alive_targets = [i for i, ps in enumerate(game.state.players) if ps.alive and i != player_id]
            if not alive_targets:
                await self.god_print("【系统提示】暂无可偷看的对手。", 0.5)
                return None
            target_id = random.choice(alive_targets)
            consume_item()
            blocked, reason = self._check_peek_blockers(player_id, target_id)
            if blocked:
                await self.god_print(f"【道具受阻】偷看卡失效：{reason}", 0.5)
                return result_flags
            target_hand = game.state.players[target_id].hand
            if not target_hand:
                await self.god_print("【系统提示】目标暂无可偷看的手牌。", 0.5)
                return result_flags
            peek_card = random.choice(target_hand)
            card_str = self._format_card(peek_card)
            self._append_system_message(player_id, f"偷看卡窥见 {self.players[target_id].name} 的 {card_str}。")
            await self.god_print(f"【道具生效】{player.name} 偷看了 {self.players[target_id].name} 的一张暗牌。", 0.5)
            return result_flags

        if item_id == "ITM_007":  # 调牌符
            if not game.state.deck:
                await self.god_print("【系统提示】牌堆耗尽，无法重新洗牌。", 0.5)
                return None
            consume_item()
            game.state.deck.extend(player_state.hand)
            random.shuffle(game.state.deck)
            player_state.hand = [game.state.deck.pop() for _ in range(3)]
            await self.god_print(f"【道具生效】{player.name} 重新洗发了手牌。", 0.5)
            return result_flags

        if item_id == "ITM_008":  # 顺手换牌
            target_name = item_payload.get("target_name")
            target_id = self._find_player_by_name(target_name) if target_name else None
            if target_id is None or not game.state.players[target_id].alive:
                await self.god_print("【系统提示】顺手换牌需要指定一名仍在牌局中的目标。", 0.5)
                return None
            target_state = game.state.players[target_id]
            if not player_state.hand or not target_state.hand:
                await self.god_print("【系统提示】双方手牌不足，无法交换。", 0.5)
                return None
            consume_item()
            try:
                my_index = int(item_payload.get("my_index", -1)) - 1
            except (TypeError, ValueError):
                my_index = -1
            if my_index not in range(len(player_state.hand)):
                my_index = random.randrange(len(player_state.hand))
            try:
                target_index = int(item_payload.get("target_index", -1)) - 1
            except (TypeError, ValueError):
                target_index = -1
            if target_index not in range(len(target_state.hand)):
                target_index = random.randrange(len(target_state.hand))
            player_card = player_state.hand[my_index]
            target_card = target_state.hand[target_index]
            player_state.hand[my_index], target_state.hand[target_index] = target_card, player_card
            await self.god_print(
                f"【道具生效】{player.name} 与 {self.players[target_id].name} 顺手交换了各自的一张牌。",
                0.5
            )
            return result_flags

        if item_id == "ITM_009":  # 免比符
            consume_item()
            self.active_effects.append({
                "effect_id": "compare_decline",
                "effect_name": item_info.get("name", "免比符"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 持有免比符，可拒绝一次被迫比牌。", 0.5)
            return result_flags

        if item_id == "ITM_010":  # 全开卡
            consume_item()
            await self.god_print(f"【道具生效】{player.name} 启动全开卡，所有玩家必须亮牌！", 0.5)
            for idx, ps in enumerate(game.state.players):
                if not ps.alive:
                    continue
                hand_str = " ".join(self._format_card(card) for card in ps.hand)
                await self.god_print(f"  - {self.players[idx].name} 的手牌: {hand_str}", 0.5)
            return result_flags

        if item_id == "ITM_011":  # 反转卡
            consume_item()
            self.active_effects.append({
                "effect_id": "compare_reverse",
                "effect_name": item_info.get("name", "反转卡"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 准备颠倒下一次比牌的胜负。", 0.5)
            return result_flags

        if item_id == "ITM_012":  # 压注加倍符
            call_cost = game.get_call_cost(player_id)
            if call_cost > player_state.chips:
                await self.god_print("【系统提示】筹码不足，压注加倍符无法生效。", 0.5)
                return None
            consume_item()
            if call_cost > 0:
                try:
                    game.step(Action(player=player_id, type=ActionType.CALL))
                except Exception as exc:
                    await self.god_print(f"【系统提示】自动跟注失败: {exc}", 0.5)
                    return None
                result_flags["skip_action"] = True
                result_flags["panel_refresh"] = True
                await self.god_print(f"【道具生效】{player.name} 自动完成跟注。", 0.5)
            next_player = self._get_next_active_player(game, player_id)
            if next_player is not None:
                self.active_effects.append({
                    "effect_id": "force_double_raise",
                    "effect_name": item_info.get("name", "压注加倍符"),
                    "source_id": player_id,
                    "target_id": next_player,
                    "turns_left": 1,
                    "category": "debuff",
                    "expires_after_action": True
                })
                self._queue_message(
                    f"【道具生效】{self.players[next_player].name} 被迫在下一回合加倍下注。",
                    0.5
                )
            return result_flags

        if item_id == "ITM_013":  # 定输免赔
            consume_item()
            ante_paid = self._current_ante_distribution[player_id] if self._current_ante_distribution else 0
            refund_amount = max(10, ante_paid // 2) if ante_paid else 20
            self.active_effects.append({
                "effect_id": "loss_refund",
                "effect_name": item_info.get("name", "定输免赔"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "hand_id": self.hand_count,
                "refund": refund_amount,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 获得定输免赔保护，若落败可返还 {refund_amount} 筹码。", 0.5)
            return result_flags

        if item_id == "ITM_014":  # 重发令
            consume_item()
            self._redeal_requested = True
            await self.god_print(f"【道具生效】{player.name} 发布重发令，本局将立即重开。", 0.5)
            result_flags["restart_hand"] = True
            return result_flags

        if item_id == "ITM_015":  # 护身符
            consume_item()
            self.active_effects.append({
                "effect_id": "compare_immunity",
                "effect_name": item_info.get("name", "护身符"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 2,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 启动护身符，两轮内无法被点名比牌。", 0.5)
            return result_flags

        if item_id == "ITM_016":  # 反侦测烟雾
            consume_item()
            self.active_effects.append({
                "effect_id": "anti_peek_once",
                "effect_name": item_info.get("name", "反侦测烟雾"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 被烟雾笼罩，本轮窥探道具全部失效。", 0.5)
            return result_flags

        if item_id == "ITM_017":  # 屏蔽卡
            consume_item()
            self.active_effects.append({
                "effect_id": "peek_shield",
                "effect_name": item_info.get("name", "屏蔽卡"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 2,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 两轮内免疫窥探。", 0.5)
            return result_flags

        if item_id == "ITM_018":  # 隐形符
            consume_item()
            self.active_effects.append({
                "effect_id": "chip_invisible",
                "effect_name": item_info.get("name", "隐形符"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 的筹码暂时对他人隐形。", 0.5)
            return result_flags

        if item_id == "ITM_019":  # 护运珠
            consume_item()
            self.active_effects.append({
                "effect_id": "bad_luck_guard",
                "effect_name": item_info.get("name", "护运珠"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 3,
                "category": "buff",
                "data": {"streak": 0}
            })
            await self.god_print(f"【道具生效】{player.name} 受到护运珠庇护，连续烂牌将被阻断。", 0.5)
            return result_flags

        if item_id == "ITM_020":  # 护牌罩
            consume_item()
            self.active_effects.append({
                "effect_id": "compare_draw",
                "effect_name": item_info.get("name", "护牌罩"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 装备护牌罩，下一次比牌失败将改判平局。", 0.5)
            return result_flags

        if item_id == "ITM_021":  # 反窥镜
            consume_item()
            self.active_effects.append({
                "effect_id": "peek_reflect",
                "effect_name": item_info.get("name", "反窥镜"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 架起反窥镜，窥探者将原形毕露。", 0.5)
            return result_flags

        if item_id == "ITM_022":  # 幸运币
            consume_item()
            self.active_effects.append({
                "effect_id": "luck_boost",
                "effect_name": item_info.get("name", "幸运币"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 1,
                "category": "buff"
            })
            await self.god_print(f"【道具生效】{player.name} 祈愿幸运，下轮起手牌将被系统庇佑。", 0.5)
            return result_flags

        if item_id == "ITM_023":  # 财神符
            consume_item()
            self.active_effects.append({
                "effect_id": "win_bonus",
                "effect_name": item_info.get("name", "财神符"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": 3,
                "category": "buff",
                "bonus_ratio": 0.25
            })
            await self.god_print(f"【道具生效】{player.name} 获得财神庇佑，未来三局胜利将额外得利。", 0.5)
            return result_flags

        if item_id == "ITM_024":  # 连胜加成
            consume_item()
            self.active_effects.append({
                "effect_id": "win_streak_boost",
                "effect_name": item_info.get("name", "连胜加成"),
                "source_id": player_id,
                "target_id": player_id,
                "turns_left": None,
                "category": "buff",
                "data": {"streak": 0}
            })
            await self.god_print(f"【道具生效】{player.name} 开启连胜加成，三连胜将获得翻倍奖励。", 0.5)
            return result_flags

        consume_item()
        await self.god_print(
            f"【系统提示】{player.name} 使用了 {item_info.get('name', item_id)}，目前效果尚未实装 (视为装饰)。",
            0.5
        )
        return result_flags

    async def _handle_loan_request(self, game: ZhajinhuaGame, player_id: int, loan_payload: Dict[str, object]):
        if not isinstance(loan_payload, dict):
            await self.god_print("【系统金库】贷款请求格式错误，已驳回。", 0.5)
            return

        amount = loan_payload.get("amount")
        turns = loan_payload.get("turns")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 0
        try:
            turns = int(turns)
        except (TypeError, ValueError):
            turns = 0

        assessment = self.vault.assess_loan_request(self.players[player_id], amount, turns)
        if not assessment.get("approved"):
            await self.god_print(f"【系统金库】{self.players[player_id].name} 的贷款被拒绝: {assessment.get('reason')}", 0.5)
            return

        granted_amount = int(assessment.get("amount", 0))
        if granted_amount <= 0:
            await self.god_print("【系统金库】贷款金额无效，操作取消。", 0.5)
            return

        player_state = game.state.players[player_id]
        player_state.chips += granted_amount
        self.persistent_chips[player_id] += granted_amount

        self.players[player_id].loan_data = {
            "due_hand": self.hand_count + int(assessment.get("due_in_hands", 3)),
            "due_amount": int(assessment.get("due_amount", granted_amount))
        }

        await self.god_print(
            f"【系统金库】批准向 {self.players[player_id].name} 贷出 {granted_amount} 筹码。"
            f"须在第 {self.players[player_id].loan_data['due_hand']} 手牌前归还共"
            f" {self.players[player_id].loan_data['due_amount']} 筹码。",
            0.5
        )
        await self.god_panel_update(self._build_panel_data(game, -1))

    async def _check_loan_repayments(self, game: ZhajinhuaGame):
        for idx, player in enumerate(self.players):
            if not player.loan_data:
                continue

            due_hand = player.loan_data.get("due_hand", self.hand_count)
            due_amount = player.loan_data.get("due_amount", 0)
            if self.hand_count < due_hand:
                continue

            player_state = game.state.players[idx]
            if player_state.chips >= due_amount:
                player_state.chips -= due_amount
                self.persistent_chips[idx] = max(0, self.persistent_chips[idx] - due_amount)
                await self.god_print(
                    f"【系统金库】{player.name} 已偿还贷款 {due_amount} 筹码，信誉恢复正常。",
                    0.5
                )
                player.loan_data.clear()
            else:
                player_state.chips = 0
                player_state.alive = False
                self.persistent_chips[idx] = 0
                player.alive = False
                await self.god_print(
                    f"【系统金库】{player.name} 无力偿还 {due_amount} 筹码，被判定违约并淘汰出局。",
                    0.5
                )
                player.loan_data.clear()

        await self.god_panel_update(self._build_panel_data(game, -1))

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
            await self._run_auction_phase()
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

    def _build_llm_prompt(self, game: ZhajinhuaGame, player_id: int, start_player_id: int,
                          player_debuffs: Optional[set[str]] = None) -> tuple:
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
            visible_chips = self._get_visible_chips(player_id, i, p.chips)
            status_line = f"  - {p_name}: 筹码={visible_chips}, 状态={status}"
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
        raw_actions = game.available_actions(player_id, player_debuffs or set())
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
            actual_chip_val = st.players[seat_player_id].chips if seat_player_id < len(st.players) else \
                self.persistent_chips[seat_player_id]
            seat_chip_info = self._get_visible_chips(player_id, seat_player_id, actual_chip_val)
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
        for message in self.player_system_messages.get(player_id, []):
            secret_message_lines.append(f"  - [系统情报]: {message}")
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

        if player_obj.loan_data:
            due_hand = player_obj.loan_data.get("due_hand", self.hand_count)
            due_amount = player_obj.loan_data.get("due_amount", 0)
            hands_left = max(0, due_hand - self.hand_count)
            my_persona_str += (
                f"\n【!! 债务警报 !!】你欠系统金库 {due_amount} 筹码，距离强制清算还剩 {hands_left} 手。"
            )

        inventory_display = []
        for item_id in player_obj.inventory:
            item_info = self.item_catalog.get(item_id)
            if item_info:
                inventory_display.append(f"{item_info.get('name', item_id)} ({item_id})")
            else:
                inventory_display.append(item_id)
        inventory_str = "空" if not inventory_display else "\n".join(inventory_display)

        return (
            "\n".join(state_summary_lines), my_hand, available_actions_str, available_actions_tuples,
            next_player_name, my_persona_str, opponent_personas_str, opponent_reflections_str,
            opponent_private_impressions_str, observed_speech_str,
            received_secret_messages_str, inventory_str,
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
            if any(effect.get("effect_id") == "compare_immunity" for effect in self._get_effects_for_player(target_id)):
                return Action(player=player_id,
                              type=ActionType.FOLD), (
                           f"警告: {self.players[player_id].name} 试图比牌的目标受到护身符保护，操作无效。强制弃牌。"
                       )
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

    async def _handle_secret_message(self, game: Optional[ZhajinhuaGame], sender_id: int, message_json: dict):
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

        if game:
            valid_recipients = [
                i for i, st_player in enumerate(game.state.players)
                if i != sender_id and st_player.alive and self.players[i].alive
            ]
        else:
            valid_recipients = [
                i for i in range(self.num_players)
                if i != sender_id and self.players[i].alive and self.persistent_chips[i] > 0
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

        # 1. 基础概率 (换1张=16%, 2张=32%, 3张=48%)
        base = self._cheat_detection_base.get(cards_count, 0.48 + 0.18 * max(0, cards_count - 3))

        # 2. 类型惩罚 (换点数风险更高)
        if cheat_type == "SWAP_RANK":
            base += 0.08

        # 3. [V2 经验风险修正] (逻辑不变)
        # (我们使用 55.0 作为“标准线”)
        experience_modifier = 0.0
        experience_gap = player_obj.experience - self.CHEAT_SWAP_REQUIRED_EXPERIENCE

        if experience_gap < 0:
            # 经验不足：施加严厉惩罚 (最高可达 +50%)
            penalty_ratio = min(abs(experience_gap) / self.CHEAT_SWAP_REQUIRED_EXPERIENCE, 1.0)
            experience_modifier = penalty_ratio * 0.50
        else:
            # 经验充足：提供减免 (最高可达 -40%)
            mitigation_ratio = min(experience_gap / (130.0 - self.CHEAT_SWAP_REQUIRED_EXPERIENCE), 1.0)
            experience_modifier = mitigation_ratio * -0.40

        # 4. 压力惩罚 (逻辑不变)
        pressure_penalty = min(0.25, player_obj.current_pressure * 0.45)

        # 5. 低筹码惩罚 (逻辑不变)
        low_stack_penalty = 0.0
        if chips < 300:
            low_stack_penalty = 0.2 + min(0.3, (300 - max(chips, 0)) / 400.0)

        # 6. [您的要求 1] 次数惩罚 (新 V3)
        # (player_obj.cheat_attempts 是作弊总次数)
        # 每次尝试 +1.5% 概率, 封顶 +20%
        frequency_penalty = min(player_obj.cheat_attempts * 0.015, 0.20)

        # 最终概率 = 基础 + 经验修正 + 压力 + 低筹码 + 次数惩罚
        probability = base + experience_modifier + pressure_penalty + low_stack_penalty + frequency_penalty

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
        await self._process_turn_based_effects()

        config = GameConfig(num_players=self.num_players)
        per_player_base, ante_distribution, total_ante = self._build_ante_distribution()
        config.base_bet = per_player_base
        config.base_bet_distribution = ante_distribution

        self._clear_system_messages()
        self._queued_messages.clear()
        self._hand_start_persistent = list(self.persistent_chips)
        self._current_ante_distribution = ante_distribution
        self._redeal_requested = False

        alive_for_ante = sum(1 for amount in ante_distribution if amount > 0)
        if alive_for_ante > 0:
            await self.god_print(
                f"本手底注总额 {total_ante}，由 {alive_for_ante} 名玩家分摊 (基础暗注 {config.base_bet})。",
                0.5
            )

        game = ZhajinhuaGame(config, self.persistent_chips, start_player_id)
        game.set_event_listener(
            "before_compare_resolution",
            lambda **kwargs: self._handle_compare_resolution(game, **kwargs)
        )

        await self._check_loan_repayments(game)

        self._record_hand_start_state(game)
        self._apply_start_of_hand_effects(game)

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
        await self._flush_queued_messages()

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

            player_debuffs = {
                effect["effect_id"]
                for effect in self.active_effects
                if effect.get("target_id") == current_player_idx and effect.get("category") == "debuff"
            }

            (state_summary, my_hand, actions_str, actions_list,
             next_player_name, my_persona_str, opponent_personas_str, opponent_reflections_str,
             opponent_private_impressions_str, observed_speech_str,
             received_secret_messages_str, inventory_str,
             min_raise_increment, dealer_name,
             observed_moods_str, multiplier, call_cost,
             table_seating_str, opponent_reference_str) = self._build_llm_prompt(
                game, current_player_idx, start_player_id, player_debuffs
            )

            try:
                action_json = await current_player_obj.decide_action(
                    state_summary, my_hand, actions_str, next_player_name,
                    my_persona_str, opponent_personas_str, opponent_reflections_str,
                    opponent_private_impressions_str, observed_speech_str,
                    received_secret_messages_str,
                    inventory_str,
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

            item_to_use = action_json.get("use_item")
            if item_to_use:
                item_result = await self._handle_item_effect(game, current_player_idx, item_to_use)
                if item_result:
                    if item_result.get("panel_refresh"):
                        await self.god_panel_update(self._build_panel_data(game, start_player_id))
                    await self._flush_queued_messages()
                    if item_result.get("restart_hand"):
                        break
                    if item_result.get("skip_action"):
                        continue

            await self._flush_queued_messages()

            loan_request = action_json.get("loan_request")
            if loan_request:
                await self._handle_loan_request(game, current_player_idx, loan_request)

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
                await self._flush_queued_messages()
            except Exception as e:
                await self.god_print(f"!! 动作执行失败: {e}。强制玩家 {current_player_obj.name} 弃牌。", 0)
                if not game.state.finished:
                    game.step(Action(player=current_player_idx, type=ActionType.FOLD))
                    await self.god_panel_update(self._build_panel_data(game, start_player_id))
                await self._flush_queued_messages()

            current_player_obj.update_experience_after_action(
                action_json,
                cheat_context,
                call_cost,
                game.state.pot
            )

            if action_obj.type == ActionType.LOOK and not game.state.finished:
                await self.god_print(f"{current_player_obj.name} 刚刚看了牌，现在轮到他/她再次行动...", 1)
                continue

            for effect in list(self.active_effects):
                if effect.get("expires_after_action") and effect.get("target_id") == current_player_idx:
                    self.active_effects.remove(effect)
                    effect_name = effect.get("effect_name", effect.get("effect_id", "效果"))
                    await self.god_print(
                        f"【道具效果结束】{current_player_obj.name} 的 {effect_name} 已完成使命。",
                        0.5
                    )

            await asyncio.sleep(1)

        if self._redeal_requested:
            self._redeal_requested = False
            self.persistent_chips = list(self._hand_start_persistent)
            self.secret_message_log = [entry for entry in self.secret_message_log if entry[0] != self.hand_count]
            self.cheat_action_log = [entry for entry in self.cheat_action_log if entry[0] != self.hand_count]
            await self.god_print("【系统提示】重发令生效，本手作废并重新发牌。", 0.5)
            await self.god_panel_update(self._build_panel_data(None, -1))
            return await self.run_round(start_player_id)

        if not game.state.finished:
            game._force_showdown()

        final_pot_size = game.state.pot_at_showdown
        winner_id = game.state.winner
        for text, delay in self._apply_post_hand_effects(game, winner_id, final_pot_size):
            await self.god_print(text, delay)

        await self.god_print(f"--- 本手结束 ---", 1)
        winner_name = "N/A"
        if winner_id is not None:
            winner_name = self.players[winner_id].name
            await self.god_print(f"赢家是 {winner_name}!", 1)
            self.last_winner_id = winner_id
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
