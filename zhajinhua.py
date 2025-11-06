from game_rules import *
import random
from typing import List, Tuple


class ZhajinhuaGame:
    def __init__(self, config: GameConfig = GameConfig(),
                 initial_chips_list: List[int] | None = None,
                 start_player_id: int = 0):  # <-- 1. 接受 start_player_id
        """
        (已修改) 接受一个可选的筹码列表和起始玩家ID。
        """
        self.config = config

        if initial_chips_list is None:
            initial_chips_list = [self.config.initial_chips] * self.config.num_players

        self.state = self._init_game(initial_chips_list, start_player_id)  # <-- 传入 ID

    def _init_game(self, current_chips: List[int], start_player_id: int) -> GameState:  # <-- 1. 接受 start_player_id
        """
        (已修改) 使用传入的筹码列表和起始玩家ID初始化状态。
        """
        deck = create_deck()
        players = []

        for i in range(self.config.num_players):
            players.append(PlayerState(chips=current_chips[i]))

        pot = 0
        for p in players:
            if p.chips >= self.config.base_bet:
                p.chips -= self.config.base_bet
                pot += self.config.base_bet
            else:
                p.alive = True
                p.all_in = True
                pot += p.chips
                p.chips = 0

        for _ in range(3):
            for p in players:
                p.hand.append(deck.pop())

        return GameState(
            config=self.config,
            deck=deck,
            players=players,
            pot=pot,
            current_bet=self.config.base_bet,
            current_player=start_player_id,  # <-- 1. 使用传入的ID
            last_raiser=start_player_id,  # <-- 1. 底注视为起始玩家 "raise"
        )

    # (alive_players, next_player, get_call_cost, get_compare_cost - 无修改)
    def alive_players(self) -> List[int]:
        return [i for i, p in enumerate(self.state.players) if p.alive]

    def next_player(self, start_from: int | None = None) -> int:
        if start_from is None:
            start_from = self.state.current_player
        n = len(self.state.players)
        i = (start_from + 1) % n
        count = 0
        while (not self.state.players[i].alive or self.state.players[i].all_in):
            i = (i + 1) % n
            count += 1
            if count > (n * 2):
                return start_from
        return i

    def get_call_cost(self, player_id: int) -> int:
        st = self.state
        ps = st.players[player_id]
        return st.current_bet * 2 if ps.looked else st.current_bet

    def get_compare_cost(self, player_id: int) -> int:
        call_cost = self.get_call_cost(player_id)
        return call_cost * self.config.compare_cost_multiplier

    # (available_actions - 无修改)
    def available_actions(self, player: int) -> List[Tuple[ActionType, int]]:
        st = self.state
        ps = st.players[player]
        if not ps.alive or st.finished or ps.all_in:
            return []
        actions = []
        if not ps.looked:
            actions.append((ActionType.LOOK, 0))
        actions.append((ActionType.FOLD, 0))
        call_cost = self.get_call_cost(player)
        if ps.chips <= call_cost:
            actions.append((ActionType.CALL, ps.chips))  # Call (All-in)
        else:
            actions.append((ActionType.CALL, call_cost))
            min_raise_an_increment = st.config.min_raise
            min_raise_cost = min_raise_an_increment * 2 if ps.looked else min_raise_an_increment
            if ps.chips > call_cost + min_raise_cost:
                actions.append((ActionType.RAISE, call_cost + min_raise_cost))
            compare_cost = self.get_compare_cost(player)
            if ps.chips >= compare_cost and len(self.alive_players()) >= 2:
                actions.append((ActionType.COMPARE, compare_cost))
        return actions

    def _handle_next_turn(self):
        """(已修改) 处理回合结束，切换到下一个玩家。"""
        st = self.state

        active_players = [i for i in self.alive_players() if not st.players[i].all_in]
        if len(active_players) <= 1:
            self._force_showdown()
            return

        st.current_player = self.next_player()

        # --- (修改点 2) ---
        # 移除了 "if st.current_player == st.last_raiser:" 逻辑。
        # 对局将无限继续，直到有人 FOLD 或 COMPARE。
        # ---------------------

    def step(self, action: Action):
        # (step 的内部逻辑, FOLD, CALL, RAISE - 无修改)
        st = self.state
        if st.finished: raise RuntimeError("Game already finished")
        if action.player != st.current_player: raise ValueError("Not this player's turn")
        ps = st.players[action.player]
        if not ps.alive: raise ValueError("Player already folded")
        if ps.all_in:
            self._handle_next_turn()
            return

        st.history.append(action)
        st.round_count += 1
        if st.round_count > st.config.max_rounds:
            self._force_showdown()
            return
        if action.type == ActionType.LOOK:
            ps.looked = True
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
            pay = self.get_call_cost(action.player)
            if ps.chips <= pay:
                pay = ps.chips
                ps.all_in = True
            ps.chips -= pay
            st.pot += pay
            self._handle_next_turn()
            return
        if action.type == ActionType.RAISE:
            raise_an_increment = action.amount
            if raise_an_increment is None or raise_an_increment < st.config.min_raise:
                raise ValueError(f"Raise increment must be at least {st.config.min_raise}")
            call_cost = self.get_call_cost(action.player)
            raise_cost = raise_an_increment * 2 if ps.looked else raise_an_increment
            pay = call_cost + raise_cost
            if ps.chips < pay: raise ValueError("Not enough chips to raise")
            if ps.chips == pay:
                ps.all_in = True
            ps.chips -= pay
            st.pot += pay
            st.current_bet += raise_an_increment
            st.last_raiser = action.player
            self._handle_next_turn()
            return

        # (step 的 COMPARE 逻辑 - 无修改)
        if action.type == ActionType.COMPARE:
            if action.target is None or not self.state.players[action.target].alive:
                raise ValueError("Invalid target for comparison")
            pay = self.get_compare_cost(action.player)
            if ps.chips < pay: raise ValueError("Not enough chips to compare")
            if ps.chips == pay:
                ps.all_in = True
            ps.chips -= pay
            st.pot += pay
            self._do_compare(action.player, action.target)
            return

    def _do_compare(self, p1: int, p2: int):
        """(已修改) 执行比牌"""
        st = self.state
        res = compare_hands(st.players[p1].hand, st.players[p2].hand)

        if res == 0:
            loser = p1
        else:
            loser = p2 if res > 0 else p1

        st.players[loser].alive = False

        alive = self.alive_players()
        if len(alive) <= 1:
            st.finished = True
            st.winner = alive[0]
            self._payout()
        else:
            # 回合从比牌者(p1)之后继续
            st.current_player = self.next_player(start_from=p1)
            # --- (修改点 3) ---
            # 移除了 "if st.current_player == st.last_raiser:" 逻辑。
            # ---------------------

    # (_force_showdown, _payout, export_state - 无修改)
    def _force_showdown(self):
        st = self.state
        if st.finished: return
        alive_indices = self.alive_players()
        if not alive_indices:
            st.winner = None
        else:
            winner = alive_indices[0]
            if st.players[winner].all_in and st.players[winner].chips == 0:
                for p_idx in alive_indices[1:]:
                    if not st.players[p_idx].all_in:
                        winner = p_idx
                        break
            for player_idx in alive_indices[1:]:
                if compare_hands(st.players[player_idx].hand, st.players[winner].hand) > 0:
                    winner = player_idx
            st.winner = winner
        st.finished = True
        self._payout()

    def _payout(self):
        if self.state.winner is not None:
            self.state.players[self.state.winner].chips += self.state.pot
            self.state.pot = 0

    def export_state(self, view_player: int | None = 0) -> dict:
        st = self.state
        players_info = []
        for i, ps in enumerate(st.players):
            hand = ["🂠"] * 3
            if st.finished and ps.alive:
                hand = [INT_TO_RANK[c.rank] + SUITS[c.suit] for c in ps.hand]
            elif view_player is not None and i == view_player and ps.looked:
                hand = [INT_TO_RANK[c.rank] + SUITS[c.suit] for c in ps.hand]
            players_info.append({
                "id": i, "chips": ps.chips, "alive": ps.alive,
                "looked": ps.looked, "hand": hand,
            })
        available_actions = []
        if view_player is not None and view_player == st.current_player and not st.finished:
            raw_actions = self.available_actions(view_player)
            for act_type, display_cost in raw_actions:
                send_amount = None
                if act_type == ActionType.RAISE:
                    send_amount = st.config.min_raise
                available_actions.append((act_type.name, display_cost, send_amount))
        return {
            "finished": st.finished, "winner": st.winner, "pot": st.pot,
            "current_bet": st.current_bet, "current_player": st.current_player,
            "players": players_info,
            "available_actions": available_actions,
            "history": [{"player": a.player, "type": a.type.name, "amount": a.amount, "target": a.target}
                        for a in st.history if isinstance(a, Action)],
        }
