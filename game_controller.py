import time
import json
import asyncio
import random
from pathlib import Path
from typing import List, Dict, Callable, Awaitable, Tuple, Optional, Set

from zhajinhua import ZhajinhuaGame, GameConfig, Action
from game_rules import ActionType, INT_TO_RANK, SUITS, GameConfig, evaluate_hand, Card, RANK_TO_INT, HandType, \
    PlayerState
from player import Player

BASE_DIR = Path(__file__).parent.resolve()
ITEM_STORE_PATH = BASE_DIR / "items_store.json"
AUCTION_PROMPT_PATH = BASE_DIR / "prompt/auction_bid_prompt.txt"
USED_PERSONA_PATH = BASE_DIR / "used_personas.json"  # <-- 📌 新增人设记录路径


class SystemVault:
    """金库逻辑：(新) 根据经验和手牌强度评估贷款请求。"""

    def __init__(self, base_interest_rate: float = 0.16):
        self.base_interest_rate = base_interest_rate

    def _calculate_hand_strength_bonus(self, hand: list[Card], has_looked: bool) -> int:
        """ (新) 根据手牌类型计算额外贷款额度 """
        if not has_looked or not hand:
            # 没看牌，或者没手牌，不能以手牌为抵押
            return 0

        try:
            hand_type = evaluate_hand(hand).hand_type
        except Exception:
            return 0

        # (新) 牌型奖金 (数值可按需调整)
        if hand_type == HandType.TRIPS:  # 豹子
            return 3000
        if hand_type == HandType.STRAIGHT_FLUSH:  # 顺金
            return 2500
        if hand_type == HandType.FLUSH:  # 金花
            return 1200
        if hand_type == HandType.STRAIGHT:  # 顺子
            return 800
        if hand_type == HandType.PAIR:  # 对子
            return 400

        # 单张 (High Card) 或 235 不提供额外奖金
        return 0

    def get_max_loan(self, experience: float, hand: list[Card], has_looked: bool) -> int:
        # (新) 修改了函数签名

        # 1. 基础额度 (来自经验值)
        baseline = 400
        experience_bonus = int(min(max(experience, 0.0) * 25, 3000))
        base_loan = baseline + experience_bonus

        # 2. 手牌强度奖金
        hand_bonus = self._calculate_hand_strength_bonus(hand, has_looked)

        return base_loan + hand_bonus

    def assess_loan_request(self, player: Player, amount: int, turns: int,
                            # (新) 评估时需要游戏状态
                            game: ZhajinhuaGame) -> Dict[str, object]:

        if player.loan_data:
            return {"approved": False, "reason": "你仍有未清贷款，必须先归还。"}

        if amount <= 0:
            return {"approved": False, "reason": "贷款金额必须大于 0。"}

        # (新) 获取手牌状态
        player_id = self._find_player_by_name(player.name)  # (需要辅助函数，假设 player.name 是唯一的)
        if player_id is None:
            player_id = self._find_player_id_by_obj(player)  # (需要辅助函数)

        # (安全回退)
        current_hand = []
        has_looked = False
        if player_id is not None and game and game.state:
            ps = game.state.players[player_id]
            current_hand = ps.hand
            has_looked = ps.looked

        max_amount = self.get_max_loan(player.experience, current_hand, has_looked)

        if amount > max_amount:
            return {
                "approved": False,
                "reason": f"额度不足。以你当前的经验和手牌，最高可贷 {max_amount}。"
            }

        approved_turns = max(2, min(6, int(turns or 0)))
        if turns is None or turns <= 0:
            approved_turns = 3

        if approved_turns < 2:
            return {"approved": False, "reason": "贷款最少需要 2 手牌后归还。"}

        interest_rate = self.base_interest_rate + max(0.0, (0.35 - min(player.experience, 120.0) / 400.0))
        interest_rate = min(0.45, interest_rate)

        # (新) 手牌越好，利率越低
        hand_bonus = self._calculate_hand_strength_bonus(current_hand, has_looked)
        interest_rate -= (hand_bonus / 3000.0) * 0.15  # (好牌最高可降低 15% 利率)
        interest_rate = max(0.05, interest_rate)  # (最低 5% 利率)

        due_amount = int(amount * (1 + interest_rate))

        return {
            "approved": True,
            "amount": amount,
            "due_amount": due_amount,
            "due_in_hands": approved_turns,
            "interest_rate": round(interest_rate, 3),
            "reason": (
                f"批准贷款 {amount}，利率 {interest_rate:.2%} (已计入手牌强度)，"
                f"请在 {approved_turns} 手内归还共 {due_amount} 筹码。"
            )
        }

    # (新) 辅助函数，用于 assess_loan_request
    def _find_player_by_name(self, name: str) -> Optional[int]:
        # (这个函数已在 GameController 中，我们假设 Vault 稍后会通过 Controller 访问)
        # (为了独立，我们暂时假设它无法访问 self.players)
        return None

    def _find_player_id_by_obj(self, player: Player) -> Optional[int]:
        # (同上)
        return None


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
        self.global_alert_level: float = 0.0
        self.CHEAT_ALERT_INCREASE = 25.0  # (新) 每次抓获增加 25 点
        self.CHEAT_ALERT_DECAY_PER_HAND = 3.0  # (新) 每手牌降低 3 点
        self.auction_min_raise_floor = 100  # (新) 拍卖中最小的加注底限 (例如 20)
        # --- [修复 19.1 (修改版)] 泄密机制 *基础* 概率 ---
        # (最终概率将受经验和警戒值影响)
        self.LEAK_SECRET_MESSAGE_BASE = 0.20  # 密信基础泄露率
        self.LEAK_CHEAT_MOVE_BASE = 0.25  # 作弊基础泄露率
        self.LEAK_BRIBE_MOVE_BASE = 0.40  # (新) 贿赂基础泄露率 (更高)

        # (↓↓ 新增此 5 行 ↓↓)
        self.LEAK_FALSIFY_POT_BASE = 0.20
        self.LEAK_COUNTERFEIT_CHIPS_BASE = 0.25
        self.LEAK_GIFT_CHIPS_BASE = 0.35
        self.LEAK_DEALER_FAVOR_BASE = 0.40
        self.LEAK_BRIBE_SWAP_BASE = 0.40

        try:
            with ITEM_STORE_PATH.open("r", encoding="utf-8") as fp:
                self.item_catalog: Dict[str, Dict[str, object]] = json.load(fp)
        except FileNotFoundError:
            self.item_catalog = {}
            print(f"【上帝(警告)】: 未找到 {ITEM_STORE_PATH.name}，拍卖行暂不可用。")
        except json.JSONDecodeError as exc:
            self.item_catalog = {}
            print(f"【上帝(错误)】: 解析 {ITEM_STORE_PATH.name} 失败: {exc}。")

            # --- [代码一致性修复]：集中加载所有 Prompt 模板 ---
        self.prompt_templates = {}
        prompt_paths = {
            "auction": AUCTION_PROMPT_PATH,
            "create_persona": BASE_DIR / "prompt/create_persona_prompt.txt",
            "decide_action": BASE_DIR / "prompt/decide_action_prompt.txt",
            "defend": BASE_DIR / "prompt/defend_prompt.txt",
            "reflect": BASE_DIR / "prompt/reflect_prompt_template.txt",
            "vote": BASE_DIR / "prompt/vote_prompt.txt",
            "bribe": BASE_DIR / "prompt/bribe_prompt.txt",  # <-- [新功能] 增加贿赂 Prompt

        }
        for name, path in prompt_paths.items():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self.prompt_templates[name] = f.read().strip()
            except Exception as e:
                self.prompt_templates[name] = ""  # 存入空字符串以防 KeyError
                print(f"【上帝(严重警告)】: 加载 Prompt 模板 {path.name} 失败: {e}")
        # --- [修复结束] ---

        self.vault = SystemVault()
        self.active_effects: List[Dict[str, object]] = []

        default_chips = GameConfig.initial_chips
        self.persistent_chips: List[int] = [default_chips] * self.num_players

        # --- [人设记录] 加载已使用的代号 (现在是完整的文本) ---
        self.used_personas: Set[str] = set()
        try:
            if USED_PERSONA_PATH.exists():
                with USED_PERSONA_PATH.open("r", encoding="utf-8") as fp:
                    content = fp.read().strip()
                    if content:
                        data = json.loads(content)
                        # 📌 关键：从简化的 [{"text": ...}, ...] 格式中提取完整的文本
                        self.used_personas.update(p.get("text") for p in data if p.get("text"))
        except Exception as exc:
            # 这里的 exc 可能是 json.JSONDecodeError 或其他，均视为加载失败
            print(f"【上帝(警告)】: 加载人设记录失败: {exc}。将从空白开始。")
        # --- [修复结束] ---

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

    def _get_player_max_bid_allowed(self, player_id: int) -> int:
        """计算单个玩家在拍卖中的实际可出价上限"""
        current_chips = self.persistent_chips[player_id]

        # 1. 计算当前底注成本
        _base, distribution, _total = self._build_ante_distribution()
        ante_cost = distribution[player_id]

        # 2. 计算安全缓冲 (例如 3 倍底注，最低 350)
        safety_buffer = max(ante_cost * 3, 350)

        # 3. 实际可出价上限 = 总筹码 - 安全缓冲
        max_bid_allowed = max(0, current_chips - safety_buffer)
        return max_bid_allowed

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
                        # sorted_hand = sorted(p_state.hand, key=lambda c: c.rank, reverse=True)  # (正确)
                        # --- (修复结束) ---
                        hand_str = ' '.join([INT_TO_RANK[c.rank] + SUITS[c.suit] for c in p_state.hand])
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
            "global_alert_level": round(self.global_alert_level, 1),
            "players": players_data,
            # (↓ 新增此行 ↓)
            "current_player": game.state.current_player if game and game.state else -1
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
            # [健壮性修复]：改为不区分大小写的比较
            if player.name.strip().lower() == (name or "").strip().lower():
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

    async def _apply_start_of_hand_effects(self, game: ZhajinhuaGame) -> None:
        for idx, ps in enumerate(game.state.players):
            if not ps.alive:
                continue
                # (↓↓ 新增此块 ↓↓)
                # 检查是否有“荷官的偏爱”
            if self._consume_effect(idx, "dealer_favor"):
                await self.god_print(
                    f"【千术】: {self.players[idx].name} 之前贿赂了荷官，荷官的偏爱正在生效...", 0.5
                )
                self._apply_luck_boost(game, idx)  # 复用幸运币的换牌逻辑
                # (注意：如果幸运币也在，会触发两次，这没问题)
            # (↑↑ 新增结束 ↑↑)
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

        # if self._consume_effect(final_loser, "compare_second_chance"):
        #     self._queue_message(
        #         f"【道具生效】{self.players[final_loser].name} 的免死金牌发动，逃过此次比牌淘汰。",
        #         0.5
        #     )
        #     return {"action": "draw"}

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

    async def _settle_bribe_debts(self, game: ZhajinhuaGame) -> List[tuple[str, float]]:
        """(新) 结算所有贿赂欠款"""
        messages: List[tuple[str, float]] = []

        for effect in list(self.active_effects):
            if effect.get("effect_id") != "bribe_debt":
                continue

            player_id = effect.get("target_id")
            if player_id is None:
                self.active_effects.remove(effect)
                continue

            # 只结算本手牌的债务
            if effect.get("hand_id") != self.hand_count:
                continue

            debt_amount = int(effect.get("amount", 0))
            if debt_amount <= 0:
                self.active_effects.remove(effect)
                continue

            player_state = game.state.players[player_id]
            player_name = self.players[player_id].name

            if player_state.chips >= debt_amount:
                # 玩家赢了，并且奖金足够支付
                player_state.chips -= debt_amount
                messages.append(
                    (f"【金库结算】: {player_name} 成功偿还了 {debt_amount} 筹码的贿赂欠款。", 0.5)
                )
            elif player_state.chips > 0:
                # 玩家赢了，但奖金不够支付（例如赢了边池）
                messages.append(
                    (f"【金库结算】: {player_name} 赢了 {player_state.chips}，不足以偿还 {debt_amount} 欠款。筹码被清零！",
                     0.5)
                )
                player_state.chips = 0
            else:
                # 玩家输了（chips=0），债务自动勾销（因为他们被淘汰了）
                messages.append(
                    (f"【金库结算】: {player_name} 在本局输光，贿赂欠款 {debt_amount} 自动勾销。", 0.3)
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

        # ... (省略拍卖行公告) ...
        item_name = item_info.get('name', item_id)
        item_effect_desc = item_info.get('description', '效果未知')
        announcement_text = (
            f"--- 🔔【系统拍卖行】🔔 ---\n"
            f"  即将竞拍: 【 {item_name} ({item_id}) 】\n"
            f"  道具效果: {item_effect_desc}\n"
            f"--------------------------"
        )
        await self.god_print(announcement_text, 0.6)

        # --- [修复 12.1] 多轮拍卖核心逻辑 (无跟注, 实时最小加注) ---
        current_highest_bid = 1  # 起拍价
        current_highest_bidder_id: Optional[int] = None
        active_bidders = set(eligible_players)

        last_raise_amount = 1
        is_first_bid_placed = False

        max_auction_rounds = 4
        round_count = 0

        # 📌 [效率优化] 计算全局有效出价上限
        global_max_effective_bid = 0
        if eligible_players:
            # 找到所有符合竞拍资格玩家中的最高出价上限
            max_bid_caps = [self._get_player_max_bid_allowed(i) for i in eligible_players]
            if max_bid_caps:
                global_max_effective_bid = max(max_bid_caps)

        while round_count < max_auction_rounds and len(active_bidders) > 1:

            # 📌 [效率优化] 检查是否已达到有效上限
            if is_first_bid_placed and current_highest_bid >= global_max_effective_bid:
                await self.god_print(
                    f"【系统拍卖行】: 当前出价 ({current_highest_bid}) 已达场上最高可出价上限 ({global_max_effective_bid})，拍卖提前结束。",
                    0.8
                )
                break  # 提前结束循环

            round_count += 1
            await self.god_print(f"--- 拍卖第 {round_count}/{max_auction_rounds} 轮 ---", 0.5)

            leader_name = self.players[
                current_highest_bidder_id].name if current_highest_bidder_id is not None else '无人'
            await self.god_print(f"当前最高价: {current_highest_bid} (来自: {leader_name})", 0.5)

            players_to_ask = list(active_bidders)
            players_who_folded = set()
            new_raise_made_this_round = False

            # (新) 标记本轮是否是首个行动者 (用于处理首轮平价)
            is_first_actor_this_round = True

            for player_id in players_to_ask:
                # --- [修复 12.1 (关键)] ---
                # (新) 在玩家行动前，实时计算最小加注额
                if not is_first_bid_placed:
                    required_increment = 1  # 首位出价者
                else:
                    required_increment = max(self.auction_min_raise_floor, int(last_raise_amount * 0.5))

                min_next_bid_to_raise = current_highest_bid + required_increment

                # (新) 如果是本轮第一个行动者，且已有人出价，必须加注
                if is_first_actor_this_round and is_first_bid_placed:
                    await self.god_print(f"(本轮必须出价 >= {min_next_bid_to_raise} 才能继续)", 0.3)
                elif not is_first_bid_placed:
                    await self.god_print(f"(等待首位出价... 最小出价: {min_next_bid_to_raise})", 0.3)
                # --- [修复 12.1 结束] ---

                is_first_actor_this_round = False  # 不再是首个行动者

                try:
                    stream_prefix = f"【系统拍卖行】[{self.players[player_id].name}] (等待出价...): "
                    result = await self._get_player_bid(
                        player_id, item_id, item_info, eligible_players, stream_prefix,
                        current_highest_bid,
                        min_next_bid_to_raise
                    )
                except Exception:
                    result = {"bid": 0}

                secret_message = result.get("secret_message")
                if secret_message:
                    await self._handle_secret_message(None, player_id, secret_message)

                bid_amount = int(result.get("bid", 0))

                if bid_amount >= min_next_bid_to_raise:
                    # 这是一个有效的加注
                    last_raise_amount = bid_amount - current_highest_bid

                    current_highest_bid = bid_amount
                    current_highest_bidder_id = player_id
                    new_raise_made_this_round = True
                    is_first_bid_placed = True

                    await self.god_print(
                        f"【拍卖行】{self.players[player_id].name} 加注到 {bid_amount}！", 0.5
                    )

                else:
                    # 出价 < 最小加注要求 (或 0)，视为放弃
                    if bid_amount > 0:
                        await self.god_print(
                            f"【拍卖行】{self.players[player_id].name} 出价 {bid_amount}，"
                            f"未达到最小加注额 {min_next_bid_to_raise}，视为放弃。", 0.4
                        )
                    players_who_folded.add(player_id)

            active_bidders.difference_update(players_who_folded)

            if len(active_bidders) == 1:
                current_highest_bidder_id = list(active_bidders)[0]
                await self.god_print(f"其他玩家均已放弃。", 0.5)
                break

            if not new_raise_made_this_round and is_first_bid_placed:
                await self.god_print(f"一轮无人加注，拍卖结束。", 0.5)
                break

            if round_count >= max_auction_rounds:
                await self.god_print(f"达到 {max_auction_rounds} 轮硬上限，拍卖结束。", 0.5)
                break

            await asyncio.sleep(0.5)

        # --- 拍卖结束，结算 ---
        if current_highest_bidder_id is None or not is_first_bid_placed:
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
                              current_highest_bid: int = 0,
                              min_next_bid_to_raise: int = 0) -> Dict[str, object]:
        player = self.players[player_id]
        # try: # <-- [修复] 移除
        #     template = AUCTION_PROMPT_PATH.read_text(encoding="utf-8") # <-- [修复] 移除
        # except FileNotFoundError: # <-- [修复] 移除
        #     return {"player_id": player_id, "bid": 0} # <-- [修复] 移除

        template = self.prompt_templates.get("auction", "")  # <-- [修复] 使用加载的模板
        if not template:  # <-- [修复] 添加检查
            return {"player_id": player_id, "bid": 0}

        # ( ... 省略 inventory_str 和 other_status 的构建 ...)
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
            other_player = self.players[other_id]
            other_chips = self.persistent_chips[other_id]
            loan_info = other_player.loan_data
            loan_str = "有债务" if loan_info else "无债务"
            # --- [修复 18.1] 拍卖时显示对手道具详情 ---
            inventory_names = [self.item_catalog.get(item_id, {}).get("name", item_id) for item_id in
                               other_player.inventory]
            inventory_str = "无" if not inventory_names else ", ".join(inventory_names)
            other_lines.append(
                f"  - {other_player.name}: 筹码 {other_chips}, 道具=[{inventory_str}], {loan_str}"
            )
            # --- [修复 18.1 结束] ---
        other_status = "\n".join(other_lines) if other_lines else "暂无竞争对手。"

        # ( ... 省略 my_assets_str 和 item_value 的构建 ...)
        current_chips = self.persistent_chips[player_id]

        # 📌 [代码简化] 使用辅助函数计算上限
        max_bid_allowed = self._get_player_max_bid_allowed(player_id)
        _base, distribution, _total = self._build_ante_distribution()
        ante_cost = distribution[player_id]
        safety_buffer = max(ante_cost * 3, 350)  # 重新计算 buffer 用于显示

        my_assets_str = f"""- 你的总筹码: {current_chips}
            - 你的背包: {inventory_str}
            - 【!! 重要警告 !!】: 你必须为下局保留 {safety_buffer} 筹码 (约 3 倍底注) 用于上桌。
            - 【!! 你的实际可出价上限是: {max_bid_allowed} !!】"""
        item_value = "1 (请自行根据描述评估)"

        # --- [修复 11.2] 更新拍卖上下文 (无跟注) ---
        auction_context_str = f"""- 当前最高价: {current_highest_bid}
    - 你的出价必须 >= {min_next_bid_to_raise} 才能继续
    - (出价低于 {min_next_bid_to_raise} 将视为放弃)"""
        # --- [修复 11.2 结束] ---

        prompt = template.format(
            item_name=item_info.get("name", item_id),
            item_description=item_info.get("description", ""),
            item_value=item_value,
            my_assets_str=my_assets_str,
            other_bidders_status=other_status,
            auction_context=auction_context_str,
            current_highest_bid=current_highest_bid,
            min_next_bid_to_raise=min_next_bid_to_raise
        )

        messages = [{"role": "user", "content": prompt}]

        # ( ... 省略 stream_callback 和 LLM 调用 ...)
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

        # --- [修复 11.3] 出价验证 (无跟注) ---

        if bid_value > 0 and bid_value < min_next_bid_to_raise:
            # AI 出价低于最小加注额
            await _stream(
                f"\n【系统提示】: 出价 {bid_value} 低于最小加注额 {min_next_bid_to_raise}，视为放弃。"
            )
            bid_value = 0  # 强制视为放弃

        elif bid_value >= min_next_bid_to_raise:
            # AI 试图加注，检查安全上限
            final_bid = max(0, min(bid_value, max_bid_allowed))

            if final_bid < bid_value:
                # AI 试图出价过高，被系统强制修正
                await _stream(
                    f"\n【系统修正】: AI 出价 {bid_value} 过高，"
                    f"已强制修正为 {final_bid} (保留 {safety_buffer} 筹码)。"
                )
                bid_value = final_bid

            # (新) 再次检查：如果修正后的价格不再高于最小加注额
            if bid_value < min_next_bid_to_raise:
                await _stream(
                    f"\n【系统提示】: 修正后的出价 {bid_value} 已无力加注，视为【放弃】。"
                )
                bid_value = 0

        # (bid_value == 0 自动视为放弃)
        # --- [修复 11.3 结束] ---

        return {
            "player_id": player_id,
            "bid": bid_value,
            "reason": parsed.get("reason"),
            "mood": parsed.get("mood"),
            "cheat_move": None,
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
            card_old_str = self._format_card(old_card)
            card_new_str = self._format_card(new_card)
            self._append_system_message(
                player_id,
                f"换牌卡替换了 {card_old_str} -> {card_new_str}。"
            )
            # (新) 将详情添加到上帝日志
            await self.god_print(f"【道具生效】{player.name} 使用换牌卡：【{card_old_str}】 替换为 【{card_new_str}】", 0.5)

            # (↓↓ 新增此行 ↓↓)
            result_flags["panel_refresh"] = True

            result_flags["re_decide_action"] = True  # <-- 📌 新增：强制重新决策
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
            # (新) 将 card_str 添加到上帝日志
            await self.god_print(
                f"【道具生效】{player.name} 使用窥牌镜，窥视到 {self.players[target_id].name} 的一张牌：【{card_str}】",
                0.5)
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
            # (新) 告知玩家这是被动道具
            await self.god_print(
                f"【系统提示】{player.name} 试图主动使用免死金牌(ITM_005)。此道具为【被动】效果，无需主动使用。", 0.5)
            # (新) AI 浪费了一次行动，但不消耗道具
            # consume_item() # (注释掉)
            return None  # 阻止行动

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
            # (新) 将 card_str 添加到上帝日志
            await self.god_print(
                f"【道具生效】{player.name} 使用偷看卡，偷看到 {self.players[target_id].name} 的一张牌：【{card_str}】", 0.5)
            return result_flags

        if item_id == "ITM_007":  # 调牌符
            if not game.state.deck:
                await self.god_print("【系统提示】牌堆耗尽，无法重新洗牌。", 0.5)
                return None
            consume_item()
            game.state.deck.extend(player_state.hand)
            random.shuffle(game.state.deck)
            game.state.deck.extend(player_state.hand)
            random.shuffle(game.state.deck)
            player_state.hand = [game.state.deck.pop() for _ in range(3)]
            # (新) 获取新手牌详情
            new_hand_str = " ".join(self._format_card(card) for card in player_state.hand)
            await self.god_print(f"【道具生效】{player.name} 使用调牌符，新手牌为：【{new_hand_str}】", 0.5)

            # (↓↓ 新增此行 ↓↓)
            result_flags["panel_refresh"] = True

            result_flags["re_decide_action"] = True  # <-- 📌 新增：强制重新决策
            return result_flags

        if item_id == "ITM_008":  # 顺手换牌
            target_name = item_payload.get("target_name")
            target_id = self._find_player_by_name(target_name) if target_name else None
            if target_id is None or not game.state.players[target_id].alive:
                await self.god_print("【系统提示】顺手换牌需要指定一名仍在牌局中的目标。", 0.5)
                return None

            # --- [修复 20.1] 阻止 AI 将自己作为目标 ---
            if target_id == player_id:
                await self.god_print(f"【系统提示】{player.name} 试图使用“顺手换牌”与自己换牌，操作无效。", 0.5)
                return None  # 阻止行动，不消耗道具
            # --- [修复 20.1 结束] ---

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
            player_card_str = self._format_card(player_card)
            target_card_str = self._format_card(target_card)
            target_name = self.players[target_id].name
            player_state.hand[my_index], target_state.hand[target_index] = target_card, player_card
            # (新) 将详情添加到上帝日志
            await self.god_print(
                f"【道具生效】{player.name} (交出 {player_card_str}) 与 {target_name} (交出 {target_card_str}) 交换了手牌。",
                0.5
            )

            # (↓↓ 新增此行 ↓↓)
            result_flags["panel_refresh"] = True

            result_flags["re_decide_action"] = True  # <-- 📌 新增：强制重新决策
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

        # (新) 将 game 对象传入评估
        assessment = self.vault.assess_loan_request(self.players[player_id], amount, turns, game)
        if not assessment.get("approved"):
            await self.god_print(f"【系统金库】{self.players[player_id].name} 的贷款被拒绝: {assessment.get('reason')}",
                                 0.5)
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
                # player.alive = False
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

        final_personas_data = []  # 收集本轮所有玩家的人设数据 (需要保留在循环外定义)

        for i, player in enumerate(self.players):
            if self.persistent_chips[i] <= 0 and player.alive:
                self.player_personas[i] = f"我是 {player.name} (已淘汰)"
                continue

            await self.god_stream_start(f"【上帝(赛前介绍)】: [{player.name}]: ")

            # 📌 这里的 player.create_persona 逻辑被修改以适应新的返回格式
            intro_text, alias = await player.create_persona(
                self.prompt_templates.get("create_persona", ""),
                list(self.used_personas),
                stream_chunk_cb=self.god_stream_chunk
            )

            if "(创建人设时出错:" in intro_text:
                await self.god_stream_chunk(f" {intro_text}")
            else:
                # 📌 简化记录逻辑，只记录完整的文本
                if intro_text:
                    self.used_personas.add(intro_text)
                    final_personas_data.append({"text": intro_text})  # 只需要 text 字段

            await self.god_stream_chunk("\n")

            self.player_personas[i] = intro_text
            self.players[i].register_persona(intro_text)
            await asyncio.sleep(0.5)

        await self.god_print(f"--- 牌桌介绍结束 ---", 2)

        # --- [人设记录] 写入文件：立即执行 ---
        try:
            # 1. 找到所有现存的人设文本
            all_saved_persona_texts = set()
            if USED_PERSONA_PATH.exists():
                with USED_PERSONA_PATH.open("r", encoding="utf-8") as fp:
                    content = fp.read().strip()
                    if content:
                        data = json.loads(content)
                        all_saved_persona_texts.update(p.get("text") for p in data if p.get("text"))

            # 2. 合并当前轮新生成的人设
            all_saved_persona_texts.update(player.persona_text for player in self.players if player.persona_text)

            # 3. 转换为最终的简化列表格式 [{"text": persona_text}, ...]
            final_list = [{"text": text} for text in sorted(list(all_saved_persona_texts))]

            with USED_PERSONA_PATH.open("w", encoding="utf-8") as fp:
                json.dump(final_list, fp, ensure_ascii=False, indent=2)

        except Exception as exc:
            print(f"【上帝(警告)】: 写入人设记录失败: {exc}")
        # --- [修复结束] ---

        await asyncio.sleep(3)

        while self.get_alive_player_count() > 1:
            self.hand_count += 1

            # --- [起始玩家修复]：确保第一手牌从 P0 (索引 0) 开始 ---
            if self.hand_count == 1:
                start_player_id = 0
                self.last_winner_id = self.num_players - 1  # 确保下一轮开始时 (self.last_winner_id + 1) % N = 0
            else:
                start_player_id = (self.last_winner_id + 1) % self.num_players
            # --- [修复结束] ---

            start_attempts = 0
            while self.persistent_chips[start_player_id] <= 0:
                start_player_id = (start_player_id + 1) % self.num_players
                start_attempts += 1
                if start_attempts > self.num_players:
                    # 极端情况下所有玩家都淘汰时，回退到 0
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

        # (↓↓ 新增逻辑 ↓↓)
        # 1. 获取真实的底池
        real_pot = st.pot
        display_pot = real_pot  # 默认显示真实底池

        # 2. 检查是否有伪造底池的效果
        falsify_effect = next((e for e in self.active_effects if e.get("effect_id") == "falsified_pot"), None)

        if falsify_effect:
            source_id = falsify_effect.get("source_id")
            # 3. 如果查看者不是施法者，就显示假底池
            if source_id != player_id:
                display_pot = falsify_effect.get("fake_pot", real_pot)
        # (↑↑ 新增结束 ↑↑)

        state_summary_lines = [
            f"当前是 {self.players[st.current_player].name} 的回合。",
            f"底池 (Pot): {display_pot}",  # (← 修改此行)
            f"当前暗注 (Base Bet): {st.current_bet}",
            f"最后加注者: {self.players[st.last_raiser].name if st.last_raiser is not None else 'N/A'}"
        ]

        state_summary_lines.append("\n玩家信息:")
        player_status_list: list[str] = []

        # (↓) 检查是否有伪造筹码的效果 (↓)
        counterfeit_effect = next((e for e in self.active_effects if e.get("effect_id") == "counterfeit_chips"), None)
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
            # (↓) 修改此逻辑块 (↓)
            visible_chips = self._get_visible_chips(player_id, i, p.chips)

            # 如果查看者(player_id)不是施法者，并且目标(i)是施法者，则显示假筹码
            if (counterfeit_effect and
                    player_id != counterfeit_effect.get("source_id") and
                    i == counterfeit_effect.get("source_id")):

                # 确保我们不会看到 ??? (隐形符)
                if visible_chips != "???":
                    visible_chips = counterfeit_effect.get("display_chips", p.chips)

            status_line = f"  - {p_name}: 筹码={visible_chips}, 状态={status}"
            # (↑) 修改结束 (↑)

            state_summary_lines.append(status_line)
            player_status_list.append(status)

        my_hand = "你还未看牌。"
        if ps.looked:
            # --- [修复 15.1] 修复手牌索引问题 ---
            # (旧) sorted_hand = sorted(ps.hand, key=lambda c: c.rank, reverse=True)
            # (旧) hand_str_list = [INT_TO_RANK[c.rank] + SUITS[c.suit] for c in sorted_hand]

            # (新) 必须按 1-based 索引显示原始手牌，AI 才能正确执行 cheat_move
            hand_str_list = []
            for i, card in enumerate(ps.hand):
                card_index = i + 1  # 转换为 1-based 索引
                card_str = INT_TO_RANK[card.rank] + SUITS[card.suit]
                hand_str_list.append(f"  - (索引 {card_index}): {card_str}")

            try:
                hand_rank_obj = evaluate_hand(ps.hand)
                hand_list_str = "\n".join(hand_str_list)
                my_hand = f"你的手牌是 (牌型: {hand_rank_obj.hand_type.name}):\n{hand_list_str}"
            except Exception:
                my_hand = f"你的手牌是:\n" + "\n".join(hand_str_list)
            # --- [修复 15.1 结束] ---

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

        # (↓↓ 新增此块 ↓↓)
        # 检查是否有待处理的贿赂换牌要约
        for effect in self.active_effects:
            if (effect.get("effect_id") == "bribe_swap_pending" and
                    effect.get("target_id") == player_id):
                source_name = self.players[effect['source_id']].name
                payment = effect['payment']
                secret_message_lines.append(
                    f"  - 【!! 秘密要约 !!】: {source_name} 提出支付你 {payment} 筹码，"
                    f"以换取你们双方的*全部手牌*。"
                    f"请在JSON中使用 'accept_bribe_swap' 键回应。"
                )
        # (↑↑ 新增结束 ↑↑)

        received_secret_messages_str = "\n".join(secret_message_lines) if secret_message_lines else "你没有收到任何秘密消息。"

        min_raise_increment = st.config.min_raise
        dealer_name = self.players[start_player_id].name
        multiplier = 2 if ps.looked else 1

        # --- [修复 18.2] 构建全场道具情报 ---
        field_item_intel_lines = []
        for i, p in enumerate(self.players):
            if i == player_id or not p.inventory:  # 跳过自己和空背包
                continue
            inventory_names = [self.item_catalog.get(item_id, {}).get("name", item_id) for item_id in p.inventory]
            inventory_str = ", ".join(inventory_names)
            field_item_intel_lines.append(f"  - {p.name} 持有: [{inventory_str}]")

        field_item_intel_str = "\n".join(field_item_intel_lines) if field_item_intel_lines else "场上暂无其他道具。"
        # --- [修复 18.2 结束] ---

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
        else:
            # --- [修复 17.1 (修正版)] ---

            # (新) 获取当前手牌状态
            ps_loan = game.state.players[player_id]
            current_hand = ps_loan.hand
            has_looked = ps_loan.looked

            # (新) get_max_loan 内部会检查 has_looked，
            # 如果未看牌，max_loan 只会包含基础额度。
            max_loan = self.vault.get_max_loan(player_obj.experience, current_hand, has_looked)

            my_persona_str += (
                f"\n【系统金库】你信誉良好。你的最高可贷额度为: {max_loan} 筹码。"
            )

            # (新) 计算基础额度
            base_loan_calc = (400 + int(min(max(player_obj.experience, 0.0) * 25, 3000)))

            if has_looked and max_loan > base_loan_calc:
                # 玩家已看牌，且额度高于基础额度，提示他们
                my_persona_str += f" (已包含你当前手牌的额外额度)"

            elif not has_looked:
                # [修复] 修正错字 (my_nota_str -> my_persona_str)
                # [修复] 移除信息泄露 (不再暗示手牌“不错”)
                my_persona_str += f" (如果你看牌，手牌强度也可能会提高额度)"

        # --- [修复 17.1 (修正版) 结束] ---

        # --- [修复 21.1] 向 AI 背包添加道具描述 ---
        inventory_display = []
        for item_id in player_obj.inventory:
            item_info = self.item_catalog.get(item_id, {})

            item_name = item_info.get('name', item_id)
            # (新) 从 items_store.json 获取描述
            item_desc = item_info.get('description', '效果未知')

            # (新) 将描述添加到提示中
            inventory_display.append(f"  - {item_name} ({item_id}): {item_desc}")

        inventory_str = "空" if not inventory_display else "\n".join(inventory_display)
        # --- [修复 21.1 结束] ---

        return (
            "\n".join(state_summary_lines), my_hand, available_actions_str, available_actions_tuples,
            next_player_name, my_persona_str, opponent_personas_str, opponent_reflections_str,
            opponent_private_impressions_str, observed_speech_str,
            received_secret_messages_str, inventory_str,
            field_item_intel_str,  # (新) 在 inventory_str 之后添加
            min_raise_increment, dealer_name, observed_moods_str, multiplier, call_cost,
            table_seating_str, opponent_reference_str
        )

    def _parse_action_json(self, game: ZhajinhuaGame, action_json: dict, player_id: int,
                           available_actions: list) -> (Action, str):
        self._parse_warnings.clear()
        action_name = action_json.get("action", "FOLD").upper()

        def find_target_id(target_name_key: str) -> (int | None, str):
            target_name = action_json.get(target_name_key)
            if not target_name:
                return None, f"未提供 {target_name_key} (比牌或指控时必须明确指定目标)"
            for i, p in enumerate(self.players):
                if p.name.strip() == target_name.strip():
                    # (已修改) 修复：确保目标是 game.state.players 中的 alive
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
            # (此部分是旧的降级逻辑，用于 AI 选择 RAISE 但 RAISE 不在可用列表时)
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
                # (已修改) 修复：此处应为 <=
                if total_cost is not None and chips < total_cost:
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
            # --- [修复 2.1]：智能降级 ---
            can_all_in = any(name == "ALL_IN_SHOWDOWN" for name, _ in available_actions)

            # 如果 AI 试图 Call, Raise 或 Compare 但筹码不足，且 All-In 是唯一出路
            if can_all_in and action_name in {"CALL", "RAISE", "COMPARE"}:
                error_msg = f"警告: {self.players[player_id].name} 试图 {action_name} 但筹码不足，自动降级为 ALL_IN_SHOWDOWN。"
                self._parse_warnings.append(error_msg)  # (使用 _parse_warnings 打印)
                return Action(player=player_id, type=ActionType.ALL_IN_SHOWDOWN), ""  # (返回空错误)

            # 否则，按原逻辑强制弃牌
            error_msg = f"警告: {self.players[player_id].name} S 选择了无效动作 '{action_name}' (可能筹码不足)。强制弃牌。"
            return Action(player=player_id, type=ActionType.FOLD), error_msg
            # --- [修复 2.1 结束] ---

        amount = None
        target = None
        target2 = None

        # --- [修复 8.1 (替换)]：集成 RAISE 成本验证 ---
        if action_type == ActionType.RAISE:
            min_inc = game.state.config.min_raise
            try:
                amount_increment_str = action_json.get("amount")
                amount = int(amount_increment_str)
                if amount < min_inc:
                    # AI 请求的加注额无效 (太小)
                    self._parse_warnings.append(
                        f"警告: {self.players[player_id].name} 试图加注 {amount} (小于最小增量 {min_inc})。")
                    return Action(player=player_id, type=ActionType.FOLD), f"加注金额 {amount} 小于最小增量 {min_inc}。"

            except (ValueError, TypeError):
                # AI 请求 RAISE 但未提供 amount
                self._parse_warnings.append(f"警告: {self.players[player_id].name} RAISE 动作未提供有效的 'amount'。")
                return Action(player=player_id, type=ActionType.FOLD), "RAISE 动作未提供有效的 'amount'。"

            # 在 _parse_action_json 中执行 RAISE 筹码验证
            ps = game.state.players[player_id]
            call_cost = game.get_call_cost(player_id)
            multiplier = 2 if ps.looked else 1
            total_raise_cost = call_cost + (amount * multiplier)

            if ps.chips < total_raise_cost:
                # 筹码不足以支付这个 RAISE！
                self._parse_warnings.append(
                    f"警告: {self.players[player_id].name} 试图 RAISE (成本 {total_raise_cost})，但只有 {ps.chips} 筹码。"
                )

                # !! 【BUG #F2 修复】 !!
                # 检查 AI 是否 *同时* 提交了作弊请求。
                # 如果 AI 正在作弊，它的意图是 All-In，我们绝不能将其降级为 CALL。
                is_cheating_this_turn = bool(action_json.get("cheat_move"))

                # 检查是否能降级为 CALL
                # (我们必须从 available_actions 列表中确认 CALL 是否可用)
                can_call = any(
                    name == "CALL" for name, cost in available_actions if name == "CALL" and ps.chips >= cost)

                # 只有在 AI (1)能跟注 且 (2)没有作弊 的情况下，才降级为 CALL
                if can_call and not is_cheating_this_turn:
                    # 筹码足够 Call，降级为 Call
                    self._parse_warnings.append("动作已自动降级为 CALL。")
                    action_type = ActionType.CALL
                    amount = None  # CALL 没有 amount
                else:
                    # (情况1：AI 正在作弊，RAISE 无效 -> 修正为 ALL_IN)
                    # (情况2：AI 没作弊，RAISE 无效，连 CALL 都不够 -> 降级为 ALL_IN)

                    # 检查是否能 All In
                    can_all_in = any(name == "ALL_IN_SHOWDOWN" for name, _ in available_actions)

                    if can_all_in:
                        if is_cheating_this_turn:
                            self._parse_warnings.append("作弊警告：RAISE 金额无效，已自动修正为 ALL_IN_SHOWDOWN。")
                        else:
                            self._parse_warnings.append("动作已自动降级为 ALL_IN_SHOWDOWN。")

                        action_type = ActionType.ALL_IN_SHOWDOWN
                        amount = None
                    else:
                        # 连 All In 都不行 (不应发生)，强制 Fold
                        self._parse_warnings.append("动作已自动降级为 FOLD。")
                        action_type = ActionType.FOLD
                        amount = None
        # --- [修复 8.1 结束] ---

        elif action_type == ActionType.COMPARE:
            target_id, err = find_target_id("target_name")
            if err:
                return Action(player=player_id,
                              type=ActionType.FOLD), f"警告: {self.players[player_id].name} COMPARE 失败: {err}。强制弃牌。"
            # (已修改) 修复：应为 target_id
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

    async def _leak_information(self, game: ZhajinhuaGame, leak_message: str, base_probability: float,
                                # (新) 必须传入“行动者”的 ID
                                actor_id: int,
                                *exclude_player_ids: int):
        """
        (新) 泄密辅助函数。
        (已修改：泄露概率受“行动者经验”和“全局警戒值”动态影响)
        """

        # --- [修复 20.1] 动态计算泄露概率 ---
        try:
            actor = self.players[actor_id]
            actor_experience = actor.experience
        except IndexError:
            actor_experience = 0.0

        # 1. 经验修正 (经验越高，越不容易泄露)
        # (例如：经验值 100 时，降低 15% 的泄露概率)
        experience_mitigation = min(0.15, (actor_experience / 100.0) * 0.15)

        # 2. 警戒值修正 (警戒值越高，越容易泄露)
        # (例如：警戒值 100 时，增加 30% 的泄露概率)
        alert_penalty = min(0.30, (self.global_alert_level / 100.0) * 0.30)

        # 3. 最终概率
        final_leak_prob = base_probability - experience_mitigation + alert_penalty
        final_leak_prob = max(0.05, min(0.80, final_leak_prob))  # 确保概率在 5% 到 80% 之间

        if random.random() >= final_leak_prob:
            return  # 本次未触发泄密
        # --- [修复 20.1 结束] ---

        # 找出所有“目击者”(活着的，且不是作弊者或密谋参与者)
        witnesses = [
            i for i, p in enumerate(game.state.players)
            if p.alive and not p.all_in and i not in exclude_player_ids
        ]

        if not witnesses:
            return  # 没有目击者

        witness_id = random.choice(witnesses)
        witness_name = self.players[witness_id].name

        self._append_system_message(witness_id, f"【!! 绝密情报 !!】{leak_message}")

        await self.god_print(f"【上帝(泄密)】: 一条情报 (P={final_leak_prob:.1%}) 已秘密泄露给 {witness_name}。", 0.5)

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

        # --- [修复 19.2 (修改版)] 秘密消息泄露 ---
        leak_msg = f"你截获了一条密信：{sender_name} 悄悄告诉 {target_name}：'{message}'"
        if game:
            await self._leak_information(
                game,
                leak_msg,
                self.LEAK_SECRET_MESSAGE_BASE,  # (新) 使用基础概率
                sender_id,  # (新) 传入行动者 ID
                sender_id, target_id
            )
        # --- [修复 19.2 结束] ---

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
        # 5. 【!! 新规则：绝境加成 (代替低筹码惩罚) !!】
        desperation_modifier = 0.0
        if chips < 300:
            # (新) 筹码越低，作弊成功率越高 (被发现概率降低)
            # (在 299 筹码时，提供 -15% 的概率；在 0 筹码时，提供 -35% 的概率)
            desperation_bonus = 0.15 + min(0.20, (300 - max(chips, 0)) / 300.0 * 0.20)
            desperation_modifier = -desperation_bonus  # (这是一个负值，即 成功率Buff)

        # 6. [您的要求 1] 次数惩罚 (新 V3)
        # (player_obj.cheat_attempts 是作弊总次数)
        # 每次尝试 +1.5% 概率, 封顶 +20%
        frequency_penalty = min(player_obj.cheat_attempts * 0.015, 0.20)

        # 7. (新) 全局警戒值惩罚
        # (每100点警戒值，增加 40% 的基础被抓率)
        global_alert_penalty = min(0.40, self.global_alert_level / 100.0)

        # 最终概率 = 基础 + 经验修正 + 压力 + 低筹码 + 次数惩罚 + 全局警戒
        probability = base + experience_modifier + pressure_penalty + desperation_modifier + frequency_penalty + global_alert_penalty

        return max(0.05, min(0.95, probability))

    def _calculate_bribe_details(self, player_id: int, ps: PlayerState) -> tuple[bool, int, float]:
        """(新) 计算贿赂成本和成功率"""
        player_obj = self.players[player_id]

        # 1. 成本：当前筹码的 70%，最低 400
        bribe_cost = max(400, int(ps.chips * 0.7))

        # 2. 成功率：基础 60%
        base_chance = 0.60

        # 3. 惩罚/奖励
        # 全局警戒值越高，贿赂越难 (最高 -30%)
        alert_penalty = (self.global_alert_level / 100.0) * 0.30
        # 经验越高，贿赂越容易 (最高 +20%)
        experience_bonus = (player_obj.experience / 100.0) * 0.20

        # [用户需求]: 筹码越低，贿赂越容易 (绝境加成)
        desperation_bonus = 0.0
        if ps.chips < 300:
            # 筹码为 300 时 bonus=0, 筹码为 0 时 bonus=0.25 (即最高提升 25% 成功率)
            desperation_bonus = ((300 - max(ps.chips, 0)) / 300.0) * 0.25

        # 4. 最终概率
        final_chance = base_chance - alert_penalty + experience_bonus + desperation_bonus
        # (提高下限和上限，以匹配绝境加成)
        final_chance = max(0.15, min(0.95, final_chance))  # 限制在 15% ~ 95%

        # 5. 可负担性
        # [IOU 修复] 玩家不再需要立即支付，但他们必须拥有 "有价值的" 筹码量（至少 100）
        # 才能让荷官认为这笔 "欠款" 有意义。
        can_afford = ps.chips >= 100

        return can_afford, bribe_cost, final_chance

    async def _handle_cheat_move(self, game: ZhajinhuaGame, player_id: int, cheat_move: Optional[dict]) -> Dict[
        str, object]:
        """(新) 处理换花色/点数作弊。"""
        result = {"attempted": False, "success": False, "type": None, "detected": False, "cards": []}
        if not cheat_move or not isinstance(cheat_move, dict):
            return result

        player_obj = self.players[player_id]
        player_name = player_obj.name

        # (↓ 新增的检查 ↓)
        ps = game.state.players[player_id]
        if not ps.looked:
            await self.god_print(f"【安保锁定】: {player_name} 试图在未看牌的情况下作弊（盲换），作弊被自动阻止。")
            log_payload = {"success": False, "error": "严禁盲换 (未看牌)", "raw": cheat_move}
            self.cheat_action_log.append((self.hand_count, player_id, cheat_move.get("type", "UNKNOWN"), log_payload))
            player_obj.update_experience_from_cheat(False, cheat_move.get("type", "UNKNOWN"), log_payload)
            result["attempted"] = True
            result["type"] = cheat_move.get("type", "UNKNOWN")
            return result
        # (↑ 新增检查结束 ↑)

        # --- [修复 5.4]：全局警戒值 100 检查 ---
        if self.global_alert_level >= 100.0 and player_obj.experience < 100.0:
            await self.god_print(
                f"【安保锁定】: 全局警戒值 100！{player_name} (经验 {player_obj.experience:.1f}) 经验不足，作弊被自动阻止。",
                0.5)
            log_payload = {"success": False, "error": "全局警戒值100，经验不足", "raw": cheat_move}
            self.cheat_action_log.append((self.hand_count, player_id, cheat_move.get("type", "UNKNOWN"), log_payload))
            player_obj.update_experience_from_cheat(False, cheat_move.get("type", "UNKNOWN"), log_payload)
            result["attempted"] = True
            result["type"] = cheat_move.get("type", "UNKNOWN")
            return result
        # --- [修复 5.4 结束] ---

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
                    await self.god_print(f"【上帝(警告)】: {player_name} 提供的目标花色无效: {entry.get('new_suit')}。",
                                         0.5)
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
                    await self.god_print(f"【上帝(警告)】: {player_name} 提供的目标点数无效: {entry.get('new_rank')}。",
                                         0.5)
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

        # (新) 提前构建
        if cheat_type_raw == "SWAP_SUIT":
            changes_desc = ", ".join(
                f"第 {m['card_index_display']} 张 {m['from']}→{m['to']}" for m in modifications
            )
        else:
            changes_desc = ", ".join(
                f"第 {m['card_index_display']} 张 {m['from']}→{m['to']}" for m in modifications
            )

        detected = random.random() < detection_probability
        if detected:
            await self.god_print(
                f"【上帝(抓现行)】: {player_name} 偷换牌被巡逻荷官发现！({len(modifications)} 张, 类型: {cheat_type_raw})",
                0.5
            )

            ps = game.state.players[player_id]
            penalty_chips_at_stake = ps.chips

            # --- [新功能：混合贿赂系统 (D20版)] ---
            # (↓) 修复了 Bug 1 (变量解包)
            can_afford_bribe, bribe_cost, success_chance = self._calculate_bribe_details(player_id, ps)
            payment_type = "IOU" if can_afford_bribe else "UPFRONT"  # (推断 payment_type)

            bribe_successful = False
            bribe_attempted = False
            is_critical_success = False

            if ps.chips < 100:
                await self.god_print(f"【上帝(贿赂失败)】: {player_name} 筹码不足 100，荷官拒绝提供贿赂选项。", 0.5)
            else:
                await self.god_print(f"【上帝(密谈)】: 荷官将 {player_name} 拉到一边... 提供了贿赂选项。")
                bribe_template = self.prompt_templates.get("bribe", "")

                if not bribe_template:
                    await self.god_print(f"【上帝(系统错误)】: 贿赂模板未加载，自动跳过。", 0.5)
                else:
                    if payment_type == "UPFRONT":
                        payment_method_string = f"“如果你现在**立即支付 {bribe_cost} 筹码** 作为‘封口费’，我可以当作什么都没看见。”"
                        consequence_string = (
                            "**如果贿赂成功 (常规检定)**：\n"
                            f"    * 你**立即支付** {bribe_cost} 筹码。\n"
                            "    * 你*不会*被淘汰，可以（用剩余筹码）继续游戏。"
                        )
                    else:  # payment_type == "IOU"
                        payment_method_string = f"“你现在付不起... 这样吧，你**同意签署一份 {bribe_cost} 筹码的‘贿赂欠款’ (IOU)**。如果你同意并贿赂成功，你将背负这笔债务继续游戏。”"
                        consequence_string = (
                            "**如果贿赂成功 (常规检定)**：\n"
                            "    * 你**不会**被立即淘汰，你的主要动作 (如 ALL_IN) 将正常执行。\n"
                            f"    * 你将背负 **{bribe_cost} 筹码的欠款**。\n"
                            "    * **【!! 债务结算 !!】**：在本手牌结束时，如果你赢得了底池，系统将**自动从你的奖金中扣除**这 {bribe_cost} 筹码。"
                        )

                    bribe_decision_json = await player_obj.decide_bribe(
                        bribe_template,
                        bribe_cost,
                        success_chance,
                        penalty_chips_at_stake,
                        payment_method_string,
                        consequence_string,
                        self.god_stream_start,
                        self.god_stream_chunk
                    )

                    wants_to_bribe = bribe_decision_json.get("bribe", False)

                    if not wants_to_bribe:
                        await self.god_print(f"【上帝(贿赂失败)】: {player_name} 拒绝了荷官的提议。", 0.5)
                    else:
                        bribe_attempted = True
                        d20_roll = random.randint(1, 20)
                        await self.god_print(f"【上帝(命运)】: {player_name} 试图说服荷官... D20 掷骰结果: {d20_roll}",
                                             0.5)
                        await asyncio.sleep(1)

                        if d20_roll == 1:
                            bribe_successful = False
                            await self.god_print(
                                f"【上帝(大失败)】: {player_name} (掷骰 1)... 荷官勃然大怒：“你在侮辱我吗？！滚出去！”", 0.5)
                            if payment_type == "UPFRONT":
                                ps.chips -= bribe_cost
                                self.persistent_chips[player_id] -= bribe_cost
                                await self.god_print(f"【上帝(惩罚)】: 荷官没收了 {bribe_cost} 筹码（贿赂金不退）。", 0.5)

                        elif d20_roll == 20:
                            bribe_successful = True
                            is_critical_success = True
                            await self.god_print(
                                f"【上帝(大成功)】: {player_name} (掷骰 20)... 荷官拍了拍他的肩膀：“都是哥们，钱不要了。我就当没看见。”",
                                0.5
                            )
                            leak_msg = f"你注意到 {player_name} (玩家 {player_id}) 作弊被抓，但他们和荷官聊了几句，荷官大笑着放过了他们，连钱都没要！"
                            await self._leak_information(
                                game, leak_msg, self.LEAK_BRIBE_MOVE_BASE, player_id, player_id
                            )

                        else:
                            await self.god_print(
                                f"【上帝(常规检定)】: (掷骰 {d20_roll}) ...荷官正在权衡利弊 (检定成功率: {success_chance:.0%})",
                                0.5)
                            await asyncio.sleep(1)

                            if random.random() < success_chance:
                                bribe_successful = True
                                if payment_type == "UPFRONT":
                                    ps.chips -= bribe_cost
                                    self.persistent_chips[player_id] -= bribe_cost
                                    await self.god_print(f"【上帝(贿赂成功)】: 荷官收下了钱 ({bribe_cost})，假装无事发生。",
                                                         0.5)
                                else:  # IOU
                                    self.active_effects.append({
                                        "effect_id": "bribe_debt",
                                        "effect_name": "贿赂欠款",
                                        "source_id": player_id,
                                        "target_id": player_id,
                                        "turns_left": 1,
                                        "hand_id": self.hand_count,
                                        "category": "debt",
                                        "amount": bribe_cost
                                    })
                                    await self.god_print(
                                        f"【上帝(贿赂成功)】: 荷官接受了欠款协议。{player_name} 负债 {bribe_cost} 继续游戏。",
                                        0.5)

                                leak_msg = f"你注意到 {player_name} (玩家 {player_id}) 作弊被抓，但他们似乎私下与荷官达成了某种交易（贿赂？），荷官随后放过了他们。"
                                await self._leak_information(
                                    game, leak_msg, self.LEAK_BRIBE_MOVE_BASE, player_id, player_id
                                )

                            else:
                                bribe_successful = False
                                if payment_type == "UPFRONT":
                                    ps.chips -= bribe_cost
                                    self.persistent_chips[player_id] -= bribe_cost
                                    await self.god_print(
                                        f"【上帝(贿赂失败)】: 荷官拒绝了贿赂... (贿赂金 {bribe_cost} 不退)", 0.5)
                                else:
                                    await self.god_print(f"【上帝(贿赂失败)】: 荷官拒绝了欠款协议！“你没有资格！”", 0.5)

            # --- [D20 修复] 警戒值增加 (移到贿赂逻辑之后) ---
            if not is_critical_success:
                old_alert = self.global_alert_level
                self.global_alert_level = min(100.0, self.global_alert_level + self.CHEAT_ALERT_INCREASE)
                await self.god_print(
                    f"【安保提示】: 全局警戒值上升！ {old_alert:.1f} -> {self.global_alert_level:.1f}", 0.5
                )
            else:
                await self.god_print(f"【安保提示】: (大成功) {player_name} 的贿赂未引起警戒值上升。", 0.5)

            # --- [!! 核心逻辑修复 (替换) !!] ---

            if not bribe_successful:
                # --- 1. 贿赂失败 = 作弊失败 (淘汰) ---
                ps.chips = 0
                ps.alive = False
                game.state.pot += penalty_chips_at_stake
                self.persistent_chips[player_id] = 0
                result["penalty_elimination"] = True
                await self.god_print(f"【作弊惩罚】: {player_name} 被当场抓获，筹码清零并淘汰出局！", 0.5)

                log_payload = {
                    "success": False,
                    "detected": True,
                    "error": "被当场抓住，贿赂失败",
                    "raw": cheat_move,
                    "cards": [
                        {"card_index": m["card_index_display"], "from": m.get("from"), "to": m.get("to"), }
                        for m in modifications
                    ],
                    "probability": round(detection_probability, 3),
                    "bribe_attempted": bribe_attempted,
                    "bribe_success": bribe_successful,
                    "bribe_cost": bribe_cost if bribe_attempted else 0
                }
                self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
                player_obj.update_experience_from_cheat(False, cheat_type_raw, log_payload)
                result["detected"] = True
                return result

            else:
                # --- 2. 贿赂成功 = 作弊成功 (换牌) ---
                result["bribe_successful"] = True
                result["penalty_elimination"] = False

                # (↓) 按你的要求：执行换牌
                for m in modifications:
                    ps.hand[m["index"]] = m["new"]

                await self.god_panel_update(self._build_panel_data(game, -1))

                cover_story = cheat_move.get("cover_story")

                # (↓) 按你的要求：记录“成功”
                log_payload = {
                    "success": True,
                    "detected": True,  # (仍然是被发现了)
                    "bribe_success": True,
                    "cards": [
                        {"card_index": m["card_index_display"], "from": m.get("from"), "to": m.get("to"), }
                        for m in modifications
                    ],
                    "cover_story": cover_story,
                    "probability": round(detection_probability, 3),
                    "bribe_cost": bribe_cost if bribe_attempted else 0,
                    "d20_roll": d20_roll if bribe_attempted else None
                }
                self.cheat_action_log.append((self.hand_count, player_id, cheat_type_raw, log_payload))
                player_obj.update_experience_from_cheat(True, cheat_type_raw, log_payload)  # (经验: 成功)

                await self.god_print(
                    f"【上帝(作弊日志)】: {player_name} 贿赂成功，作弊被强行执行 ({changes_desc})。", 0.5
                )

                leak_msg = f"你注意到 {player_name} (玩家 {player_id}) 作弊被抓，但他们似乎私下与荷官达成了某种交易（贿赂？），荷官随后放过了他们。"
                await self._leak_information(
                    game, leak_msg, self.LEAK_BRIBE_MOVE_BASE, player_id, player_id
                )

                result["success"] = True
                result["cards"] = log_payload["cards"]
                return result
            # --- [!! 核心逻辑修复 (结束) !!] ---

        # --- (此块不变) 未被发现 = 作弊成功 (换牌) ---
        for m in modifications:
            ps.hand[m["index"]] = m["new"]

        # (↓↓ 新增此行，立即刷新面板 ↓↓)
        await self.god_panel_update(self._build_panel_data(game, -1))

        cover_story = cheat_move.get("cover_story")
        log_payload = {
            "success": True,
            "detected": False,  # (未被发现)
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

        await self.god_print(
            f"【上帝(作弊日志)】: {player_name} 偷偷修改了 {len(modifications)} 张牌 ({changes_desc})。",
            0.5
        )

        leak_msg = f"你注意到 {player_name} (玩家 {player_id}) 的动作非常可疑... 似乎在荷官不注意时调换了手牌。"
        await self._leak_information(
            game,
            leak_msg,
            self.LEAK_CHEAT_MOVE_BASE,
            player_id,
            player_id
        )

        result["success"] = True
        result["cards"] = log_payload["cards"]
        return result

    async def _handle_falsify_pot(self, game: ZhajinhuaGame, player_id: int, payload: dict):
        """处理伪造底池的千术"""
        COST = 250  # 固定的手续费
        player_state = game.state.players[player_id]
        player_name = self.players[player_id].name

        fake_amount = payload.get("fake_pot_amount")
        try:
            fake_amount = int(fake_amount)
        except (TypeError, ValueError):
            await self.god_print(f"【千术失败】: {player_name} 试图伪造底池，但未提供有效的金额。", 0.5)
            return

        if player_state.chips < COST:
            await self.god_print(f"【千术失败】: {player_name} 筹码不足 {COST} 来支付伪造底池的费用。", 0.5)
            return

        player_state.chips -= COST
        self.persistent_chips[player_id] -= COST

        # 移除旧效果（防止叠加）
        for effect in list(self.active_effects):
            if effect.get("effect_id") == "falsified_pot" and effect.get("source_id") == player_id:
                self.active_effects.remove(effect)

        self.active_effects.append({
            "effect_id": "falsified_pot",
            "effect_name": "伪造底池",
            "source_id": player_id,
            "fake_pot": fake_amount,
            "turns_left": 2
        })

        await self.god_print(
            f"【千术】: {player_name} 支付 {COST} 筹码，将底池伪造成 {fake_amount}！", 0.5
        )

        leak_msg = f"你感觉底池的数目看起来不太对劲... {player_name} 似乎在暗中动了手脚。"
        await self._leak_information(
            game, leak_msg,
            self.LEAK_FALSIFY_POT_BASE,
            player_id, player_id
        )
        await self.god_panel_update(self._build_panel_data(game, -1))

    async def _handle_counterfeit_chips(self, game: ZhajinhuaGame, player_id: int, payload: dict):
        """处理伪造筹码的千术"""
        COST = 150
        player_state = game.state.players[player_id]
        player_name = self.players[player_id].name

        fake_amount = payload.get("fake_amount")
        try:
            fake_amount = int(fake_amount)
        except (TypeError, ValueError):
            await self.god_print(f"【千术失败】: {player_name} 试图伪造筹码，但未提供有效的金额。", 0.5)
            return

        if player_state.chips < COST:
            await self.god_print(f"【千术失败】: {player_name} 筹码不足 {COST} 来支付伪造筹码的费用。", 0.5)
            return

        player_state.chips -= COST
        self.persistent_chips[player_id] -= COST

        # 移除旧效果
        for effect in list(self.active_effects):
            if effect.get("effect_id") == "counterfeit_chips" and effect.get("source_id") == player_id:
                self.active_effects.remove(effect)

        self.active_effects.append({
            "effect_id": "counterfeit_chips",
            "effect_name": "伪造筹码",
            "source_id": player_id,
            "display_chips": fake_amount,
            "turns_left": 2
        })

        await self.god_print(
            f"【千术】: {player_name} 支付 {COST} 筹码，将自己的筹码伪造成 {fake_amount}！", 0.5
        )

        leak_msg = f"你注意到 {player_name} 的筹码堆看起来有点不对劲，似乎比他/她应有的要多..."
        await self._leak_information(
            game, leak_msg,
            self.LEAK_COUNTERFEIT_CHIPS_BASE,
            player_id, player_id
        )
        await self.god_panel_update(self._build_panel_data(game, -1))

    async def _handle_gift_chips(self, game: ZhajinhuaGame, player_id: int, payload: dict):
        """处理赠送筹码的千术"""
        player_state = game.state.players[player_id]
        player_name = self.players[player_id].name

        target_name = payload.get("target_name")
        target_id = self._find_player_by_name(target_name)

        try:
            amount = int(payload.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0

        if target_id is None or not self.players[target_id].alive or not game.state.players[target_id].alive:
            await self.god_print(f"【千术失败】: {player_name} 试图赠送筹码给无效或已淘汰的目标: {target_name}", 0.5)
            return

        if amount <= 0:
            await self.god_print(f"【千术失败】: {player_name} 试图赠送无效的筹码金额。", 0.5)
            return

        if player_state.chips < amount:
            await self.god_print(f"【千术失败】: {player_name} 筹码不足 {amount} 来赠送。", 0.5)
            return

        # 执行转移
        player_state.chips -= amount
        self.persistent_chips[player_id] -= amount
        game.state.players[target_id].chips += amount
        self.persistent_chips[target_id] += amount
        target_name = self.players[target_id].name

        await self.god_print(
            f"【秘密交易】: {player_name} 偷偷赠送了 {amount} 筹码给 {target_name}！", 0.5
        )

        self._append_system_message(player_id, f"你成功赠送了 {amount} 筹码给 {target_name}。")
        self._append_system_message(target_id, f"【!! 秘密收入 !!】: {player_name} 刚刚赠送了你 {amount} 筹码！")

        leak_msg = f"你似乎看到 {player_name} 和 {target_name} 之间有筹码在桌下传递..."
        await self._leak_information(
            game, leak_msg,
            self.LEAK_GIFT_CHIPS_BASE,
            player_id, player_id, target_id
        )
        await self.god_panel_update(self._build_panel_data(game, -1))

    async def _handle_dealer_favor(self, game: ZhajinhuaGame, player_id: int):
        """处理贿赂荷官以求偏爱"""
        COST = 400
        player_state = game.state.players[player_id]
        player_name = self.players[player_id].name

        if player_state.chips < COST:
            await self.god_print(f"【千术失败】: {player_name} 筹码不足 {COST} 来贿赂荷官。", 0.5)
            return

        # 检查是否已有此效果，防止重复购买
        if self._player_has_effect(player_id, "dealer_favor"):
            await self.god_print(f"【千术失败】: {player_name} 已经购买过荷官的偏爱了。", 0.5)
            return

        player_state.chips -= COST
        self.persistent_chips[player_id] -= COST

        self.active_effects.append({
            "effect_id": "dealer_favor",
            "effect_name": "荷官的偏爱",
            "target_id": player_id,
            "turns_left": 1  # 仅在下一手牌开始时生效
        })

        await self.god_print(
            f"【千术】: {player_name} 支付 {COST} 筹码贿赂了荷官，以求在*下一手牌*获得好运！", 0.5
        )

        leak_msg = f"你注意到 {player_name} 趁荷官发牌时，往荷官手里塞了些筹码..."
        await self._leak_information(
            game, leak_msg,
            self.LEAK_DEALER_FAVOR_BASE,
            player_id, player_id
        )
        await self.god_panel_update(self._build_panel_data(game, -1))

    async def _handle_propose_bribe_swap(self, game: ZhajinhuaGame, player_id: int, payload: dict):
        """处理发起贿赂换牌要约"""
        player_state = game.state.players[player_id]
        player_name = self.players[player_id].name

        target_name = payload.get("target_name")
        target_id = self._find_player_by_name(target_name)
        payment = int(payload.get("payment", 0))

        if target_id is None or not self.players[target_id].alive or not game.state.players[target_id].alive:
            await self.god_print(f"【千术失败】: {player_name} 试图贿赂无效的目标: {target_name}", 0.5)
            return

        if payment <= 0:
            await self.god_print(f"【千术失败】: {player_name} 试图用 0 筹码贿赂，要约无效。", 0.5)
            return

        if player_state.chips < payment:
            await self.god_print(f"【千术失败】: {player_name} 筹码不足 {payment} 来支付贿赂。", 0.5)
            return

        # 移除旧的待处理要约 (防止刷屏)
        for effect in list(self.active_effects):
            if effect.get("effect_id") == "bribe_swap_pending" and effect.get("source_id") == player_id:
                self.active_effects.remove(effect)

        self.active_effects.append({
            "effect_id": "bribe_swap_pending",
            "source_id": player_id,
            "target_id": target_id,
            "action": "SWAP_HANDS",
            "payment": payment,
            "turns_left": 1  # 只在对方的下一个回合有效
        })

        await self.god_print(f"【千术】: {player_name} 正在向 {target_name} 提出 {payment} 筹码的“换牌贿赂”...", 0.5)

        leak_msg = f"你似乎看到 {player_name} 鬼鬼祟祟地向 {target_name} 递了张纸条..."
        await self._leak_information(
            game, leak_msg,
            self.LEAK_BRIBE_SWAP_BASE,
            player_id, player_id, target_id
        )

    async def _handle_accept_bribe_swap(self, game: ZhajinhuaGame, player_id: int, payload: dict) -> dict | None:
        """处理接受或拒绝贿赂换牌要约"""
        player_state = game.state.players[player_id]  # 接受者 (B)
        player_name = self.players[player_id].name

        source_name = payload.get("source_name")
        source_id = self._find_player_by_name(source_name)
        accept = payload.get("accept", False)

        offer_effect = None
        for effect in self.active_effects:
            if (effect.get("effect_id") == "bribe_swap_pending" and
                    effect.get("target_id") == player_id and
                    effect.get("source_id") == source_id):
                offer_effect = effect
                break

        if offer_effect is None:
            await self.god_print(f"【千术失败】: {player_name} 试图回应一个不存在或已过期的贿赂要约。", 0.5)
            return None

        self.active_effects.remove(offer_effect)

        if not accept:
            await self.god_print(f"【千术】: {player_name} 拒绝了 {source_name} 的换牌贿赂。", 0.5)
            self._append_system_message(source_id, f"【!! 要约被拒 !!】: {player_name} 拒绝了你的换牌要约。")
            return None

        # --- 接受贿赂 ---
        payment = offer_effect['payment']
        action = offer_effect['action']  # 总是 "SWAP_HANDS"

        if source_id is None or not self.players[source_id].alive or not game.state.players[source_id].alive:
            await self.god_print(f"【千术失败】: {player_name} 接受了贿赂，但 {source_name} 已不在场！", 0.5)
            return None

        source_state = game.state.players[source_id]  # 付款人 (A)

        if source_state.chips < payment:
            await self.god_print(
                f"【千术失败】: {player_name} 接受了贿赂，但 {source_name} 已经没有足够的筹码 ({payment}) 支付！", 0.5)
            self._append_system_message(source_id,
                                        f"【!! 支付失败 !!】: {player_name} 接受了你的要约，但你已无力支付 {payment}！")
            return None

        # 1. 转移筹码
        source_state.chips -= payment
        self.persistent_chips[source_id] -= payment
        player_state.chips += payment
        self.persistent_chips[player_id] += payment

        await self.god_print(
            f"【贿赂成功】: {player_name} 接受了 {source_name} 的 {payment} 筹码！", 0.5
        )
        self._append_system_message(source_id, f"{player_name} 接受了你的 {payment} 筹码。")
        self._append_system_message(player_id, f"你收到了 {source_name} 的 {payment} 筹码。")

        # 2. 执行换牌 (背叛的开始)
        p_hand = player_state.hand
        a_hand = source_state.hand
        player_state.hand = a_hand  # B 拿到了 A 的牌
        source_state.hand = p_hand  # A 拿到了 B 的牌

        p_hand_str = " ".join(self._format_card(c) for c in a_hand)
        a_hand_str = " ".join(self._format_card(c) for c in p_hand)

        await self.god_print(
            f"【千术】: {player_name} 与 {source_name} 秘密交换了手牌！", 0.5
        )
        self._append_system_message(player_id, f"交换成功。你的新手牌 (来自 {source_name}): {p_hand_str}")
        self._append_system_message(source_id, f"交换成功。你的新手牌 (来自 {player_name}): {a_hand_str}")

        leak_msg = f"你注意到 {player_name} 和 {source_name} 之间达成了某种交易，他们交换了手牌！"
        await self._leak_information(
            game, leak_msg,
            self.LEAK_GIFT_CHIPS_BASE,
            player_id, player_id, source_id
        )

        # 强制 B 重新决策（现在 B 拿着 A 的牌）
        return {"panel_refresh": True, "re_decide_action": True}

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
            await self.god_print(f"{accuser_name} 筹码不足 ({accuser_state.chips}) 支付指控成本 ({cost})。指控自动失败。",
                                 1)
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
            self.prompt_templates.get("defend", ""),  # <-- [修复] 传入模板
            accuser_name, target_name_2, evidence_log_str,
            self.god_stream_start, self.god_stream_chunk
        )
        await asyncio.sleep(1)

        defense_speech_2 = await self.players[target_id_2].defend(
            self.prompt_templates.get("defend", ""),  # <-- [修复] 传入模板
            accuser_name, target_name_1, evidence_log_str,
            self.god_stream_start, self.god_stream_chunk
        )
        await asyncio.sleep(2)

        await self.god_print(f"--- 审判阶段 3: 陪审团投票 ---", 1)

        vote_tasks = []
        for jury_id in jury_list:
            vote_tasks.append(
                self.players[jury_id].vote(
                    self.prompt_templates.get("vote", ""),  # <-- [修复] 传入模板
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
            # self.players[target_id_1].alive = False
            # self.players[target_id_2].alive = False

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
            # self.players[accuser_id].alive = False

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
        # (新) 警戒值随时间衰减
        if self.global_alert_level > 0:
            decay = min(self.global_alert_level, self.CHEAT_ALERT_DECAY_PER_HAND)
            self.global_alert_level = max(0.0, self.global_alert_level - decay)
            if decay > 0:
                await self.god_print(f"【安保提示】: 警戒值降低 {decay:.1f}，当前: {self.global_alert_level:.1f}", 0.2)

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
        await self._apply_start_of_hand_effects(game)  # <-- 在此添加 await

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
                await self.god_print(
                    f"跳过 {current_player_obj.name} (状态: {'All-In' if p_state.all_in else '已弃牌'})", 0.5)
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
             field_item_intel_str,  # (新) 接收新变量
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
                    field_item_intel_str,  # (新) 传入新变量
                    min_raise_increment,
                    dealer_name,
                    observed_moods_str,
                    multiplier,
                    call_cost,
                    table_seating_str,
                    opponent_reference_str,
                    self.prompt_templates.get("decide_action", ""),  # <-- [修复] 传入模板
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
                    await self.god_print(
                        f"【上帝(错误详情)】: [{current_player_obj.name}] 决策失败并强制弃牌，原因: {error_reason}", 0.5)
                # --- 调试块结束 ---

            cheat_context = await self._handle_cheat_move(game, current_player_idx, action_json.get("cheat_move"))

            # --- [修改点 1.2 (修正版)]：如果玩家因作弊被淘汰，则跳过本轮后续动作 ---
            if cheat_context.get("penalty_elimination"):
                # (我们不再需要在这里调用 _handle_next_turn())
                # (循环顶部的 'if not p_state.alive' 会自动处理)
                await self.god_panel_update(self._build_panel_data(game, start_player_id))
                continue  # 结束当前玩家的循环
            # --- [修改点 1.2 (修正版) 结束] ---

            secret_message_json = action_json.get("secret_message")
            if secret_message_json:
                await self._handle_secret_message(game, current_player_idx, secret_message_json)

            # (↓↓ 新增此处的 6 个处理器 ↓↓)
            # 1. (必须最先) 处理“接受贿赂”
            accept_bribe_payload = action_json.get("accept_bribe_swap")
            if accept_bribe_payload:
                bribe_result = await self._handle_accept_bribe_swap(game, current_player_idx, accept_bribe_payload)
                if bribe_result and bribe_result.get("re_decide_action"):
                    await self.god_panel_update(self._build_panel_data(game, start_player_id))
                    await self.god_print(
                        f"【系统提示】: {current_player_obj.name} 接受了贿赂并交换了手牌，请重新决策...", 0.5)
                    continue  # 强制重新决策

            # 2. (必须在 accept 之后) 处理“发起贿赂”
            propose_bribe_payload = action_json.get("propose_bribe_swap")
            if propose_bribe_payload:
                await self._handle_propose_bribe_swap(game, current_player_idx, propose_bribe_payload)

            # 3. 处理“赠送筹码”
            gift_payload = action_json.get("gift_chips")
            if gift_payload:
                await self._handle_gift_chips(game, current_player_idx, gift_payload)

            # 4. 处理“伪造底池”
            falsify_payload = action_json.get("falsify_pot")
            if falsify_payload:
                await self._handle_falsify_pot(game, current_player_idx, falsify_payload)

            # 5. 处理“伪造筹码”
            counterfeit_payload = action_json.get("counterfeit_chips")
            if counterfeit_payload:
                await self._handle_counterfeit_chips(game, current_player_idx, counterfeit_payload)

            # 6. 处理“荷官的偏爱”
            favor_payload = action_json.get("request_favor")
            if favor_payload:
                # 检查是否为布尔值true
                if isinstance(favor_payload, bool) and favor_payload:
                    await self._handle_dealer_favor(game, current_player_idx)

            # (↑↑ 新增结束 ↑↑)

            item_to_use = action_json.get("use_item")
            re_decide = False  # <-- 📌 新增：定义 re_decide 标志
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
                        # <-- 📌 新增：如果触发了重新决策，则设置标志
                    if item_result.get("re_decide_action"):
                        re_decide = True

            await self._flush_queued_messages()

            loan_request = action_json.get("loan_request")
            if loan_request:
                await self._handle_loan_request(game, current_player_idx, loan_request)
                # (新) 如果处理了贷款，立即刷新面板以显示新筹码
                await self.god_panel_update(self._build_panel_data(game, start_player_id))

            # --- [修复 22.1] 修复贷款导致动作验证失败的Bug ---
            # (旧的 'actions_list' 在贷款/道具使用后已“陈旧”)
            # (我们必须在解析前，根据*当前*的筹码量重新生成动作列表)
            fresh_raw_actions = game.available_actions(current_player_idx, player_debuffs or set())
            fresh_actions_list = [(act_type.name, display_cost) for act_type, display_cost in fresh_raw_actions]
            # --- [修复 22.1 结束] ---

            if re_decide:
                # 不执行动作，不调用 game.step()，不调用 _handle_next_turn()
                await self.god_print(f"【系统提示】: {current_player_obj.name} 使用了手牌调整道具，请重新决策动作...", 0.5)
                continue  # 跳到下一个循环，再次询问当前玩家

            # (新) 使用“新鲜”的列表进行解析
            action_obj, error_msg = self._parse_action_json(game, action_json, current_player_idx, fresh_actions_list)
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

        # --- [IOU 修复] 结算贿赂欠款 ---
        # (必须在 _apply_post_hand_effects 之后，在最终淘汰检查之前)
        for text, delay in await self._settle_bribe_debts(game):
            await self.god_print(text, delay)
        # --- [修复结束] ---
        winner_name = "N/A"
        if winner_id is not None:
            winner_name = self.players[winner_id].name
            await self.god_print(f"赢家是 {winner_name}!", 1)
            self.last_winner_id = winner_id
        else:
            await self.god_print("没有赢家 (流局)。", 1)

        await self.god_print("--- 最终亮牌 (上帝视角已在看板) ---", 1)
        await self.god_panel_update(self._build_panel_data(game, start_player_id))
        await self.god_print("--- 本手筹码结算 (并检查淘汰/复活) ---", 1)

        # (新) 在循环外获取 'game' 对象，因为 'game' 在此作用域内 100% 可用。
        current_game_state = game.state

        for i, p_state in enumerate(game.state.players):
            old_chips = self.persistent_chips[i]
            new_chips = p_state.chips

            # (新) 检查是否在本轮死亡
            if new_chips <= 0:
                p = self.players[i]
                if p.alive:  # 仅当他们 *之前* 还活着时，才处理淘汰/复活
                    # --- 检查 ITM_005 复活 ---
                    if "ITM_005" in p.inventory:
                        try:
                            p.inventory.remove("ITM_005")
                        except ValueError:
                            pass

                        revive_chips = 300
                        new_chips = revive_chips  # (新) 将新筹码设为复活筹码
                        p.alive = True  # 保持控制器存活

                        # (新) 更新游戏状态机
                        current_game_state.players[i].chips = revive_chips
                        current_game_state.players[i].alive = True
                        current_game_state.players[i].all_in = False

                        await self.god_print(f"  {self.players[i].name}: {old_chips} -> 0", 0.3)
                        await self.god_print(f"!!! 玩家 {p.name} 筹码输光...但免死金牌(ITM_005)发动！", 0.5)
                        await self.god_print(f"【道具生效】: {p.name} 消耗道具并以 {revive_chips} 筹码复活！", 1)

                    else:
                        # --- 没有复活道具，玩家被淘汰 ---
                        await self.god_print(f"  {self.players[i].name}: {old_chips} -> {new_chips}", 0.3)
                        await self.god_print(f"!!! 玩家 {p.name} 筹码输光，已被淘汰 !!!", 1)
                        p.alive = False  # (新) 在控制器中标记为淘汰

                else:  # (如果 p.alive 已经是 False，说明是之前淘汰的)
                    await self.god_print(f"  {self.players[i].name}: {old_chips} -> {new_chips} (已淘汰)", 0.3)

            else:
                # 筹码 > 0
                await self.god_print(f"  {self.players[i].name}: {old_chips} -> {new_chips}", 0.3)

            # (新) 最终更新 persistent_chips
            self.persistent_chips[i] = new_chips

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

        # --- [AI 脆弱性修复] ---
        # 预处理历史记录，将 玩家ID 和 目标ID 替换为 玩家名字
        # 极大降低 LLM 在 reflect 阶段解析历史的认知负担
        processed_history = []
        raw_history_list = final_state_data.get('history', [])

        for action_dict in raw_history_list:
            processed_action = action_dict.copy()

            # 替换 'player' ID
            if 'player' in processed_action:
                player_id = processed_action['player']
                if 0 <= player_id < len(self.players):
                    # 使用玩家名字
                    processed_action['player_name'] = self.players[player_id].name
                else:
                    processed_action['player_name'] = f"未知 (ID:{player_id})"
                del processed_action['player']  # 移除旧的 ID 键

            # 替换 'target' ID (用于 COMPARE, ACCUSE 等)
            if 'target' in processed_action and processed_action['target'] is not None:
                target_id = processed_action['target']
                if 0 <= target_id < len(self.players):
                    # 使用目标名字
                    processed_action['target_name'] = self.players[target_id].name
                else:
                    processed_action['target_name'] = f"未知 (ID:{target_id})"
                del processed_action['target']  # 移除旧的 ID 键

            processed_history.append(processed_action)

        # 使用处理后的人类可读历史
        round_history_json = json.dumps(processed_history, indent=2, ensure_ascii=False)
        # --- [修复结束] ---

        round_result_str = f"赢家是 {winner_name}"

        new_impressions_map = {}

        for i, player in enumerate(self.players):
            if self.persistent_chips[i] > 0 and self.players[i].alive:

                current_player_impressions = self.player_private_impressions.get(i, {})

                # --- [策略优化]：只将存活对手的笔记信息传回 AI ---
                opponent_impressions_data = {}
                for opponent_id, impression_text in current_player_impressions.items():
                    # 检查：1. 不是自己； 2. 对手必须存活
                    if opponent_id != i and self.players[opponent_id].alive:
                        opponent_name = self.players[opponent_id].name
                        opponent_impressions_data[opponent_name] = impression_text

                current_impressions_json_str = json.dumps(opponent_impressions_data, indent=2, ensure_ascii=False)
                # --- [优化结束] ---

                # --- [修复 13.1] 构建玩家 ID-名字索引 (只包含存活对手) ---
                player_self_details_str = f"  - {player.name} (Player {i})"
                opponent_name_list_lines = []
                for opp_id, opp_player in enumerate(self.players):
                    # 检查：1. 不是自己； 2. 对手必须存活
                    if opp_id == i or not opp_player.alive:
                        continue
                    opponent_name_list_lines.append(f"  - {opp_player.name} (Player {opp_id})")
                opponent_name_list_str = "\n".join(opponent_name_list_lines)
                # --- [修复 13.1 结束] ---

                (reflection_text, private_impressions_dict) = await player.reflect(
                    self.prompt_templates.get("reflect", ""),  # <-- [修复] 传入模板
                    round_history_json,
                    round_result_str,
                    current_impressions_json_str,
                    # (新) 传入索引
                    player_self_details_str,
                    opponent_name_list_str,
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
