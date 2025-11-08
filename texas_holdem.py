"""Texas Hold'em game implementation compatible with the existing
`GameController` infrastructure.

The goal of this module is to offer an API that mirrors the behaviour of
`zhajinhua.ZhajinhuaGame` so that the higher level systems (items, loans,
logging, etc.) can operate without changes.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Dict, List, Optional

from game_rules import (
    Action,
    ActionType,
    Card,
    GameConfig,
    GameState,
    PlayerState,
    create_deck,
    INT_TO_RANK,
    SUITS,
)


class TexasHandType(IntEnum):
    HIGH_CARD = 1
    PAIR = 2
    TWO_PAIR = 3
    THREE_OF_A_KIND = 4
    STRAIGHT = 5
    FLUSH = 6
    FULL_HOUSE = 7
    FOUR_OF_A_KIND = 8
    STRAIGHT_FLUSH = 9


@dataclass(frozen=True)
class TexasHandRank:
    hand_type: TexasHandType
    key: tuple[int, ...]


def _normalize_straight(ranks: List[int]) -> Optional[int]:
    unique = sorted(set(ranks), reverse=True)
    if len(unique) < 5:
        return None
    # Handle wheel straight (A2345)
    if set([12, 0, 1, 2, 3]).issubset(set(ranks)):
        return 3  # Five high straight
    for i in range(len(unique) - 4):
        window = unique[i : i + 5]
        if window[0] - window[4] == 4:
            return window[0]
    return None


def evaluate_best_hand(hole_cards: List[Card], community_cards: List[Card]) -> TexasHandRank:
    """Evaluate the strongest 5-card hand from hole + community cards."""

    cards = list(hole_cards) + list(community_cards)
    if len(cards) < 5:
        raise ValueError("Texas Hold'em evaluation requires at least 5 cards")

    best_rank: Optional[TexasHandRank] = None
    for combo in itertools.combinations(cards, 5):
        ranks = sorted((card.rank for card in combo), reverse=True)
        suits = [card.suit for card in combo]

        is_flush = len(set(suits)) == 1
        straight_high = _normalize_straight(ranks)
        counts: Dict[int, int] = {}
        for r in ranks:
            counts[r] = counts.get(r, 0) + 1
        count_items = sorted(counts.items(), key=lambda x: (-x[1], -x[0]))

        if straight_high is not None and is_flush:
            rank_obj = TexasHandRank(
                TexasHandType.STRAIGHT_FLUSH,
                (straight_high,) + tuple(sorted(ranks, reverse=True))
            )
        elif count_items[0][1] == 4:
            four_rank = count_items[0][0]
            kicker = max(r for r in ranks if r != four_rank)
            rank_obj = TexasHandRank(TexasHandType.FOUR_OF_A_KIND, (four_rank, kicker))
        elif count_items[0][1] == 3 and count_items[1][1] == 2:
            rank_obj = TexasHandRank(
                TexasHandType.FULL_HOUSE,
                (count_items[0][0], count_items[1][0])
            )
        elif is_flush:
            rank_obj = TexasHandRank(TexasHandType.FLUSH, tuple(ranks))
        elif straight_high is not None:
            rank_obj = TexasHandRank(TexasHandType.STRAIGHT, (straight_high,))
        elif count_items[0][1] == 3:
            kickers = [r for r in ranks if r != count_items[0][0]]
            rank_obj = TexasHandRank(
                TexasHandType.THREE_OF_A_KIND,
                (count_items[0][0],) + tuple(kickers)
            )
        elif count_items[0][1] == 2 and count_items[1][1] == 2:
            pair_ranks = sorted([count_items[0][0], count_items[1][0]], reverse=True)
            kicker = max(r for r in ranks if r not in pair_ranks)
            rank_obj = TexasHandRank(
                TexasHandType.TWO_PAIR,
                tuple(pair_ranks) + (kicker,)
            )
        elif count_items[0][1] == 2:
            kickers = [r for r in ranks if r != count_items[0][0]]
            rank_obj = TexasHandRank(
                TexasHandType.PAIR,
                (count_items[0][0],) + tuple(kickers)
            )
        else:
            rank_obj = TexasHandRank(TexasHandType.HIGH_CARD, tuple(ranks))

        if best_rank is None or rank_obj.hand_type > best_rank.hand_type:
            best_rank = rank_obj
        elif best_rank and rank_obj.hand_type == best_rank.hand_type and rank_obj.key > best_rank.key:
            best_rank = rank_obj

    assert best_rank is not None
    return best_rank


class TexasHoldemGame:
    """Simplified Texas Hold'em implementation compatible with GameController."""

    def __init__(
        self,
        config: GameConfig = GameConfig(),
        initial_chips_list: Optional[List[int]] = None,
        start_player_id: int = 0,
        event_listeners: Optional[Dict[str, Callable[..., Optional[dict]]]] = None,
    ) -> None:
        self.config = config
        self._event_listeners = event_listeners or {}
        if initial_chips_list is None:
            initial_chips_list = [self.config.initial_chips] * self.config.num_players
        self.state = self._init_game(initial_chips_list, start_player_id)

    def _init_game(self, current_chips: List[int], start_player_id: int) -> GameState:
        deck = create_deck()
        players = []
        pot = 0
        base_distribution = self.config.base_bet_distribution
        if base_distribution is not None:
            if len(base_distribution) != self.config.num_players:
                raise ValueError("base_bet_distribution length must match num_players")
            ante_distribution = list(base_distribution)
        else:
            ante_distribution = [self.config.base_bet] * self.config.num_players

        for idx in range(self.config.num_players):
            ps = PlayerState(chips=current_chips[idx])
            ante = ante_distribution[idx]
            if ante > 0 and ps.chips > 0:
                pay = min(ps.chips, ante)
                ps.chips -= pay
                pot += pay
            if ps.chips <= 0 and ante > 0:
                ps.alive = False
                ps.all_in = True
            ps.hand = [deck.pop() for _ in range(2)] if ps.alive else []
            ps.looked = True  # 玩家始终看到底牌
            players.append(ps)

        state = GameState(
            config=self.config,
            deck=deck,
            players=players,
            pot=pot,
            current_bet=0,
            current_player=self._first_to_act(start_player_id, players),
            last_raiser=None,
        )
        state.round_bets = [0] * self.config.num_players
        state.stage = "preflop"
        state.community_cards: List[Card] = []
        state.stage_start_player = self._first_to_act(start_player_id, players)
        state.dealer = start_player_id
        state.pot_at_showdown = pot
        return state

    def _first_to_act(self, dealer: int, players: List[PlayerState]) -> int:
        idx = (dealer + 1) % len(players)
        rotations = 0
        while rotations < len(players) and (not players[idx].alive or players[idx].all_in):
            idx = (idx + 1) % len(players)
            rotations += 1
        return idx

    def set_event_listener(self, event_name: str, callback: Callable[..., Optional[dict]]) -> None:
        self._event_listeners[event_name] = callback

    def _dispatch_event(self, event_name: str, **kwargs) -> Optional[dict]:
        callback = self._event_listeners.get(event_name)
        if not callback:
            return None
        return callback(**kwargs)

    def alive_players(self) -> List[int]:
        return [i for i, p in enumerate(self.state.players) if p.alive]

    def next_player(self, start_from: Optional[int] = None) -> int:
        if start_from is None:
            start_from = self.state.current_player
        n = len(self.state.players)
        i = (start_from + 1) % n
        rotations = 0
        while rotations < n:
            ps = self.state.players[i]
            if ps.alive and not ps.all_in:
                return i
            i = (i + 1) % n
            rotations += 1
        return start_from

    def get_call_cost(self, player_id: int) -> int:
        st = self.state
        ps = st.players[player_id]
        if not ps.alive or ps.all_in:
            return 0
        target = st.current_bet
        current = st.round_bets[player_id]
        return max(0, target - current)

    def get_compare_cost(self, player_id: int) -> int:
        return self.get_call_cost(player_id)

    def get_accuse_cost(self, player_id: int) -> int:
        base = self.get_call_cost(player_id)
        if base == 0:
            base = self.state.config.base_bet or 10
        return base * self.config.accuse_cost_multiplier

    def available_actions(self, player: int, active_debuffs: Optional[set] = None) -> List[tuple[ActionType, int]]:
        st = self.state
        ps = st.players[player]
        if not ps.alive or st.finished or ps.all_in:
            return []

        actions: List[tuple[ActionType, int]] = []
        actions.append((ActionType.FOLD, 0))

        call_cost = self.get_call_cost(player)
        accuse_cost = self.get_accuse_cost(player)

        can_call = ps.chips >= call_cost
        forced_double = active_debuffs and "force_double_raise" in active_debuffs

        if can_call:
            actions.append((ActionType.CALL, call_cost))

        min_raise = self.config.min_raise
        raise_cost = call_cost + min_raise
        if forced_double:
            raise_cost = max(raise_cost, call_cost * 2)

        if ps.chips > raise_cost and (not active_debuffs or "lock_raise" not in active_debuffs):
            actions.append((ActionType.RAISE, raise_cost))
        elif ps.chips > call_cost:
            actions.append((ActionType.ALL_IN_SHOWDOWN, ps.chips))

        alive_targets = [
            i for i in self.alive_players() if i != player and not self.state.players[i].all_in
        ]
        if ps.chips >= accuse_cost and len(alive_targets) >= 2:
            actions.append((ActionType.ACCUSE, accuse_cost))

        if ps.chips < call_cost:
            actions.append((ActionType.ALL_IN_SHOWDOWN, ps.chips))

        return actions

    def _advance_stage(self) -> None:
        st = self.state
        if st.stage == "preflop":
            st.community_cards.extend([st.deck.pop(), st.deck.pop(), st.deck.pop()])
            st.stage = "flop"
        elif st.stage == "flop":
            st.community_cards.append(st.deck.pop())
            st.stage = "turn"
        elif st.stage == "turn":
            st.community_cards.append(st.deck.pop())
            st.stage = "river"
        else:
            self._force_showdown()
            return

        st.round_bets = [0] * len(st.players)
        st.current_bet = 0
        st.last_raiser = None
        st.current_player = self._first_to_act(st.dealer, st.players)
        st.stage_start_player = st.current_player

    def _handle_next_turn(self) -> None:
        st = self.state
        active = [i for i in self.alive_players() if not st.players[i].all_in]
        if len(active) <= 1:
            self._force_showdown()
            return
        st.current_player = self.next_player()

    def _check_round_completion(self) -> None:
        st = self.state
        if st.finished:
            return
        outstanding = [
            i for i in self.alive_players()
            if not st.players[i].all_in and self.get_call_cost(i) > 0
        ]
        if not outstanding:
            if st.stage == "river":
                self._force_showdown()
            else:
                self._advance_stage()

    def step(self, action: Action) -> None:
        st = self.state
        if st.finished:
            raise RuntimeError("Game already finished")
        if action.player != st.current_player:
            raise ValueError("Not this player's turn")

        ps = st.players[action.player]
        if not ps.alive:
            raise ValueError("Player already folded")
        if ps.all_in:
            self._handle_next_turn()
            return

        st.history.append(action)
        st.round_count += 1
        if st.round_count > st.config.max_rounds:
            self._force_showdown()
            return

        if action.type == ActionType.FOLD:
            ps.alive = False
            alive = self.alive_players()
            if len(alive) <= 1:
                st.finished = True
                st.winner = alive[0] if alive else None
                self._payout()
            else:
                self._handle_next_turn()
            return

        if action.type == ActionType.CALL:
            pay = min(ps.chips, self.get_call_cost(action.player))
            st.round_bets[action.player] += pay
            ps.chips -= pay
            st.pot += pay
            if ps.chips == 0:
                ps.all_in = True
            self._handle_next_turn()
            self._check_round_completion()
            return

        if action.type == ActionType.RAISE:
            raise_inc = action.amount or 0
            min_raise = st.config.min_raise
            if raise_inc < min_raise:
                raise ValueError(f"Raise increment must be at least {min_raise}")

            call_cost = self.get_call_cost(action.player)
            total_pay = call_cost + raise_inc
            if ps.chips < total_pay:
                raise ValueError("Not enough chips to raise")

            st.current_bet = st.round_bets[action.player] + total_pay
            st.round_bets[action.player] += total_pay
            ps.chips -= total_pay
            st.pot += total_pay
            if ps.chips == 0:
                ps.all_in = True
            st.last_raiser = action.player
            self._handle_next_turn()
            self._check_round_completion()
            return

        if action.type == ActionType.ALL_IN_SHOWDOWN:
            pay = ps.chips
            st.round_bets[action.player] += pay
            ps.chips -= pay
            st.pot += pay
            ps.all_in = True
            st.current_bet = max(st.current_bet, st.round_bets[action.player])
            self._handle_next_turn()
            self._check_round_completion()
            return

        if action.type == ActionType.ACCUSE:
            cost = self.get_accuse_cost(action.player)
            if ps.chips < cost:
                raise ValueError("Not enough chips to accuse")
            ps.chips -= cost
            st.pot += cost
            # 具体审判逻辑由控制器处理
            return

        raise ValueError(f"Unsupported action type: {action.type}")

    def _force_showdown(self) -> None:
        st = self.state
        if st.finished:
            return
        alive_indices = self.alive_players()
        if not alive_indices:
            st.finished = True
            st.winner = None
            return
        if len(alive_indices) == 1:
            st.finished = True
            st.winner = alive_indices[0]
            self._payout()
            return

        board = getattr(st, "community_cards", [])
        best_player = alive_indices[0]
        best_rank = evaluate_best_hand(st.players[best_player].hand, board)

        for idx in alive_indices[1:]:
            current_rank = evaluate_best_hand(st.players[idx].hand, board)
            if current_rank.hand_type > best_rank.hand_type:
                best_player, best_rank = idx, current_rank
            elif current_rank.hand_type == best_rank.hand_type and current_rank.key > best_rank.key:
                best_player, best_rank = idx, current_rank

        st.finished = True
        st.winner = best_player
        self._payout()

    def _payout(self) -> None:
        st = self.state
        st.pot_at_showdown = st.pot
        if st.winner is not None:
            st.players[st.winner].chips += st.pot
            st.pot = 0

    def export_state(self, view_player: Optional[int] = 0) -> dict:
        st = self.state
        def _card_to_str(card: Card) -> str:
            return f"{INT_TO_RANK[card.rank]}{SUITS[card.suit]}"

        players_info = []
        for i, ps in enumerate(st.players):
            hand = ["🂠"] * len(ps.hand)
            if st.finished and ps.alive:
                hand = [_card_to_str(card) for card in ps.hand]
            elif view_player is not None and i == view_player:
                hand = [_card_to_str(card) for card in ps.hand]
            players_info.append(
                {
                    "id": i,
                    "chips": ps.chips,
                    "alive": ps.alive,
                    "looked": ps.looked,
                    "hand": hand,
                }
            )

        available_actions = []
        if view_player is not None and view_player == st.current_player and not st.finished:
            raw_actions = self.available_actions(view_player)
            for act_type, display_cost in raw_actions:
                available_actions.append((act_type.name, display_cost))

        community = [_card_to_str(card) for card in getattr(st, "community_cards", [])]

        return {
            "finished": st.finished,
            "winner": st.winner,
            "pot": st.pot,
            "current_bet": st.current_bet,
            "current_player": st.current_player,
            "players": players_info,
            "community_cards": community,
            "stage": getattr(st, "stage", "preflop"),
            "available_actions": available_actions,
            "history": [
                {
                    "player": a.player,
                    "type": a.type.name,
                    "amount": a.amount,
                    "target": a.target,
                }
                for a in st.history
                if isinstance(a, Action)
            ],
        }

