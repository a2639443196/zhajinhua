"""
 ClassName game_rules
 Description: 核心牌型与数据结构
 (已修改：实现花色优先级)
 (已修改：增加 ALL_IN_SHOWDOWN 动作)
 (已修改：增加 ACCUSE 动作)
"""
import random
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional

# 牌面点数，从小到大
RANKS = ["2", "3", "4", "5", "6", "7", "8",
         "9", "10", "J", "Q", "K", "A"]
SUITS = ["♠", "♥", "♣", "♦"]

RANK_TO_INT = {r: i for i, r in enumerate(RANKS)}
INT_TO_RANK = {i: r for r, i in RANK_TO_INT.items()}


class HandType(IntEnum):
    HIGH_CARD = 1  # 单张
    PAIR = 2  # 对子
    STRAIGHT = 3  # 顺子
    FLUSH = 4  # 金花
    STRAIGHT_FLUSH = 5  # 顺金
    TRIPS = 6  # 豹子
    SPECIAL_235 = 7  # 特殊 235


@dataclass(frozen=True)
class Card:
    rank: int
    suit: int


@dataclass(frozen=True)
class HandRank:
    hand_type: HandType
    key: tuple[int, ...]


def make_card(rank_str: str, suit_str: str) -> Card:
    return Card(
        rank=RANK_TO_INT[rank_str],
        suit=SUITS.index(suit_str)
    )


def get_suit_priority(suit_index: int) -> int:
    return 3 - suit_index


def evaluate_hand(cards: list[Card]) -> HandRank:
    if len(cards) != 3:
        raise ValueError("Zhajinhua hand must have 3 cards")
    sorted_cards_for_tiebreak = sorted(
        cards,
        key=lambda c: (c.rank, get_suit_priority(c.suit)),
        reverse=True
    )
    full_key = tuple(val for c in sorted_cards_for_tiebreak for val in (c.rank, get_suit_priority(c.suit)))
    ranks_sorted_by_rank_only = sorted((c.rank for c in cards), reverse=True)
    suits_unsorted = [c.suit for c in cards]
    rank_set = set(ranks_sorted_by_rank_only)
    is_flush = len(set(suits_unsorted)) == 1
    unique_ranks_count = len(rank_set)
    is_straight = False
    straight_key_rank = tuple()
    a23_ranks = {RANK_TO_INT["A"], RANK_TO_INT["2"], RANK_TO_INT["3"]}
    if rank_set == a23_ranks:
        is_straight = True
        straight_key_rank = (RANK_TO_INT["3"],)
    elif ranks_sorted_by_rank_only[0] - ranks_sorted_by_rank_only[2] == 2 and unique_ranks_count == 3:
        is_straight = True
        straight_key_rank = (max(ranks_sorted_by_rank_only),)
    special_235_ranks = {RANK_TO_INT["2"], RANK_TO_INT["3"], RANK_TO_INT["5"]}
    if rank_set == special_235_ranks and not is_flush:
        return HandRank(HandType.SPECIAL_235, full_key)
    from collections import Counter
    counter = Counter(ranks_sorted_by_rank_only)
    counts = sorted(counter.items(), key=lambda x: (-x[1], -x[0]))
    if unique_ranks_count == 1:
        return HandRank(HandType.TRIPS, full_key)
    if is_flush and is_straight:
        suit_prio = get_suit_priority(suits_unsorted[0])
        final_straight_key = straight_key_rank + (suit_prio,)
        return HandRank(HandType.STRAIGHT_FLUSH, final_straight_key)
    if is_flush:
        return HandRank(HandType.FLUSH, full_key)
    if is_straight:
        final_key = straight_key_rank + full_key
        return HandRank(HandType.STRAIGHT, final_key)
    if unique_ranks_count == 2:
        pair_rank = counts[0][0]
        single_rank = counts[1][0]
        primary_key = (pair_rank, single_rank)
        final_key = primary_key + full_key
        return HandRank(HandType.PAIR, final_key)
    return HandRank(HandType.HIGH_CARD, full_key)


def compare_hands(cards_a: list[Card], cards_b: list[Card]) -> int:
    """
    返回: 1 (A>B), -1 (A<B), 0 (A=B)
    """
    rank_a = evaluate_hand(cards_a)
    rank_b = evaluate_hand(cards_b)
    if rank_a.hand_type > rank_b.hand_type:
        return 1
    if rank_a.hand_type < rank_b.hand_type:
        return -1
    if rank_a.key > rank_b.key:
        return 1
    if rank_a.key < rank_b.key:
        return -1
    return 0


class ActionType(IntEnum):
    FOLD = 1
    CALL = 2
    RAISE = 3
    LOOK = 4
    COMPARE = 5
    ALL_IN_SHOWDOWN = 8
    ACCUSE = 9  # (新) 指控


@dataclass
class Action:
    player: int
    type: ActionType
    amount: int | None = None
    target: int | None = None
    # (新) target2 仅用于指控
    target2: int | None = None


@dataclass
class GameConfig:
    num_players: int = 3
    initial_chips: int = 300
    base_bet: int = 10
    min_raise: int = 10
    compare_cost_multiplier: int = 2
    # (新) 指控成本是跟注成本的 10 倍 (高风险)
    accuse_cost_multiplier: int = 10
    max_rounds: int = 100


@dataclass
class PlayerState:
    chips: int
    hand: List[Card] = field(default_factory=list)
    alive: bool = True
    looked: bool = False
    all_in: bool = False


@dataclass
class GameState:
    config: GameConfig
    deck: List[Card]
    players: List[PlayerState]
    pot: int = 0
    current_bet: int = 0
    current_player: int = 0
    last_raiser: Optional[int] = None
    round_count: int = 0
    history: List[Action] = field(default_factory=list)
    finished: bool = False
    winner: Optional[int] = None


def create_deck() -> List[Card]:
    deck = []
    for r in RANKS:
        for s in SUITS:
            deck.append(make_card(r, s))
    random.shuffle(deck)
    return deck
