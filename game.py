"""
 ClassName game
 Description: 核心牌型与数据结构
 (已修改：实现花色优先级)
"""
import random
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional

# 牌面点数，从小到大
RANKS = ["2", "3", "4", "5", "6", "7", "8",
         "9", "10", "J", "Q", "K", "A"]
SUITS = ["♠", "♥", "♣", "♦"]  #

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
    rank: int  # 使用 RANK_TO_INT 的值
    suit: int  # 0..3


@dataclass(frozen=True)
class HandRank:
    hand_type: HandType
    key: tuple[int, ...]  # 比较大小时用的元组，越大越强


def make_card(rank_str: str, suit_str: str) -> Card:
    return Card(
        rank=RANK_TO_INT[rank_str],
        suit=SUITS.index(suit_str)
    )


# (新) 帮助函数：获取花色优先级
def get_suit_priority(suit_index: int) -> int:
    """
    根据 rule_base.txt: ♠ > ♥ > ♣ > ♦
    SUITS 列表索引: ♠(0), ♥(1), ♣(2), ♦(3)
    返回一个更高的数字代表更好的花色。
    """
    return 3 - suit_index


def evaluate_hand(cards: list[Card]) -> HandRank:
    if len(cards) != 3:
        raise ValueError("Zhajinhua hand must have 3 cards")

    # --- (新) 核心修改：按 (点数, 花色) 优先级排序 ---
    # 1. 创建一个按 (点数, 花色) 排序的卡牌列表，用于最终 tie-break
    sorted_cards_for_tiebreak = sorted(
        cards,
        key=lambda c: (c.rank, get_suit_priority(c.suit)),
        reverse=True
    )
    # 2. 创建一个 (R1, SP1, R2, SP2, R3, SP3) 格式的元组
    #    示例: (12, 3, 11, 2, 10, 1) for A♠ K♥ Q♣
    full_key = tuple(val for c in sorted_cards_for_tiebreak for val in (c.rank, get_suit_priority(c.suit)))
    # ---------------------------------------------

    # --- (旧) 逻辑，用于快速检测牌型 ---
    ranks_sorted_by_rank_only = sorted((c.rank for c in cards), reverse=True)
    suits_unsorted = [c.suit for c in cards]
    rank_set = set(ranks_sorted_by_rank_only)

    is_flush = len(set(suits_unsorted)) == 1
    unique_ranks_count = len(rank_set)
    # ------------------------------------

    # --- 规则：判断顺子 (包含 A-2-3) ---
    is_straight = False
    straight_key_rank = tuple()  # 仅用于存储顺子的 (主) 级别

    # 特殊顺子 A-2-3 (A=12, 2=0, 3=1)
    a23_ranks = {RANK_TO_INT["A"], RANK_TO_INT["2"], RANK_TO_INT["3"]}
    if rank_set == a23_ranks:
        is_straight = True
        straight_key_rank = (RANK_TO_INT["3"],)  # (A23最小)

    # 普通顺子 QKA (A=12) 或 234
    elif ranks_sorted_by_rank_only[0] - ranks_sorted_by_rank_only[2] == 2 and unique_ranks_count == 3:
        is_straight = True
        straight_key_rank = (max(ranks_sorted_by_rank_only),)  # 顺子比最大牌
    # ------------------------------------

    # --- 规则：特殊 235 (非同花) ---
    special_235_ranks = {RANK_TO_INT["2"], RANK_TO_INT["3"], RANK_TO_INT["5"]}
    if rank_set == special_235_ranks and not is_flush:
        # (新) 使用 full_key 进行 235 内部的花色比较
        return HandRank(HandType.SPECIAL_235, full_key)  #
    # --------------------------------

    from collections import Counter
    counter = Counter(ranks_sorted_by_rank_only)
    counts = sorted(counter.items(), key=lambda x: (-x[1], -x[0]))

    # 豹子
    if unique_ranks_count == 1:
        # (新) 使用 full_key 进行花色比较 (例如 A♠A♥A♦ > A♠A♥A♣)
        return HandRank(HandType.TRIPS, full_key)  #

    # 顺金 (QKA 最大, A23 最小)
    if is_flush and is_straight:
        # (新) 键 = (顺子级别, 花色)
        suit_prio = get_suit_priority(suits_unsorted[0])  # 都是同花
        final_straight_key = straight_key_rank + (suit_prio,)
        # (12, 3) for QKA♠ > (12, 2) for QKA♥
        return HandRank(HandType.STRAIGHT_FLUSH, final_straight_key)  #

    # 金花 (JKA 最大, 235 最小)
    if is_flush:
        # (新) 使用 full_key 比较 (A♠K♠9♠ > A♥K♥9♥)
        return HandRank(HandType.FLUSH, full_key)  #

    # 顺子 (QKA 最大, A23 最小)
    if is_straight:
        # (新) 键 = (顺子级别) + (full_key tie-break)
        # (12, ...) for QKA > (11, ...) for JQK
        # (12, R1,SP1...) > (12, R1,SP1'...)
        final_key = straight_key_rank + full_key
        return HandRank(HandType.STRAIGHT, final_key)  #

    # 对子 (AAK 最大, 223 最小)
    if unique_ranks_count == 2:
        pair_rank = counts[0][0]
        single_rank = counts[1][0]
        # (新) 键 = (对子级别, 踢卡级别) + (full_key tie-break)
        primary_key = (pair_rank, single_rank)
        final_key = primary_key + full_key
        return HandRank(HandType.PAIR, final_key)  #

    # 单张 (JKA 最大, 234 最小)
    # (新) 使用 full_key 比较 (A♠K♦... > A♥K♣...)
    return HandRank(HandType.HIGH_CARD, full_key)  #


def compare_hands(cards_a: list[Card], cards_b: list[Card]) -> int:
    """
    返回: 1 (A>B), -1 (A<B), 0 (A=B)
    """
    rank_a = evaluate_hand(cards_a)
    rank_b = evaluate_hand(cards_b)

    # 235 > 豹子 的规则已通过 HandType 枚举值 (7 > 6) 自动实现

    if rank_a.hand_type > rank_b.hand_type:
        return 1
    if rank_a.hand_type < rank_b.hand_type:
        return -1

    # 牌型相同，比较 key (key 现在已包含花色信息)
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


@dataclass
class Action:
    player: int
    type: ActionType
    amount: int | None = None  # RAISE 时: "暗注"的"增量"
    target: int | None = None  # COMPARE 时: 目标玩家


@dataclass
class GameConfig:
    num_players: int = 3
    initial_chips: int = 1000
    base_bet: int = 10  # 底注 (暗注)
    min_raise: int = 10  # 最小加注 (暗注)
    compare_cost_multiplier: int = 2  # 规则: 比牌费用是"当前单注"的两倍
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
    current_bet: int = 0  # 永远表示"暗注"的当前额度
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
