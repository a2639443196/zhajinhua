#!/usr/bin/env python3
"""
测试游戏结束和信息重置功能的脚本

测试内容：
1. 游戏日志保存功能
2. AI信息重置功能
3. 游戏状态清理功能
4. 完整的游戏结束流程
"""

import asyncio
import sys
import os
import json
from pathlib import Path

# 添加当前目录到 Python 路径
sys.path.append(str(Path(__file__).parent))

from game_controller import GameController
from server import GameLogCollector


class MockCallbacks:
    """模拟回调函数用于测试"""

    def __init__(self):
        self.printed_messages = []
        self.stream_chunks = []
        self.panel_data = []
        self.event_logs = []

    async def god_print(self, message: str, delay: float = 1.0):
        """模拟上帝消息输出"""
        self.printed_messages.append(message)
        print(f"[GOD_PRINT] {message}")
        await asyncio.sleep(0.1)  # 简化延迟

    async def god_stream_start(self, message: str):
        """模拟流输出开始"""
        print(f"[STREAM_START] {message}")

    async def god_stream_chunk(self, chunk: str):
        """模拟流输出片段"""
        self.stream_chunks.append(chunk)
        print(f"[STREAM_CHUNK] {chunk}")

    async def god_panel_update(self, data: dict):
        """模拟面板更新"""
        self.panel_data.append(data)
        print(f"[PANEL_UPDATE] {len(data)} fields")

    async def god_event_log_update(self, event: dict):
        """模拟事件日志更新"""
        self.event_logs.append(event)
        print(f"[EVENT_LOG] {event.get('type', 'Unknown')}")


async def test_game_reset_functionality():
    """测试游戏重置功能"""
    print("=" * 60)
    print("开始测试游戏结束和信息重置功能")
    print("=" * 60)

    # 创建模拟回调
    callbacks = MockCallbacks()

    # 创建简化的玩家配置（减少测试时间）
    player_configs = [
        {"name": "测试玩家1", "model": "test-model"},
        {"name": "测试玩家2", "model": "test-model"},
        {"name": "测试玩家3", "model": "test-model"}
    ]

    # 创建游戏控制器
    controller = GameController(
        player_configs=player_configs,
        god_print_callback=callbacks.god_print,
        god_stream_start_callback=callbacks.god_stream_start,
        god_stream_chunk_callback=callbacks.god_stream_chunk,
        god_panel_update_callback=callbacks.god_panel_update,
        god_event_log_update_callback=callbacks.god_event_log_update,
        despair_threshold=500
    )

    print("\n1. 测试初始状态...")
    print(f"玩家数量: {len(controller.players)}")
    print(f"手牌计数: {controller.hand_count}")
    print(f"初始筹码: {controller.persistent_chips}")

    # 模拟一些游戏数据
    print("\n2. 模拟游戏数据...")
    controller.hand_count = 5
    controller.global_alert_level = 25.0

    # 模拟AI数据
    for i, player in enumerate(controller.players):
        player.experience = 45.5
        player.persona_tags.add("test")
        player.persona_text = f"测试人设{i}"
        player.cheat_attempts = 3
        player.cheat_success = 2
        player.mindgame_moves = 5
        player.inventory = ["test_item"]
        player.loan_data = {"test_loan": 100}

    controller.player_personas = ["人设1", "人设2", "人设3"]
    controller.player_private_impressions = {0: {1: "对手印象"}}
    controller.player_reflections = ["反思1", "反思2", "反思3"]
    controller.secret_message_log = [(1, 0, 1, "测试密信")]
    controller.cheat_action_log = [(1, 0, "TEST_CHEAT", "测试作弊")]
    controller.public_event_log = [{"type": "测试事件", "player_name": "玩家1", "details": "测试详情", "hand": 1}]
    controller.used_personas.add("使用过的人设")

    print(f"手牌计数: {controller.hand_count}")
    print(f"警戒等级: {controller.global_alert_level}")
    print(f"玩家1经验: {controller.players[0].experience}")
    print(f"玩家1道具: {controller.players[0].inventory}")

    # 创建日志收集器
    print("\n3. 创建日志收集器...")
    log_collector = GameLogCollector()
    log_collector.append_log("测试游戏日志1")
    log_collector.append_log("测试游戏日志2")
    log_collector.append_log("测试游戏日志3")

    # 测试完整重置功能
    print("\n4. 执行完整游戏重置...")
    try:
        await controller.complete_game_reset(log_collector)
        print("✅ 游戏重置执行成功")
    except Exception as e:
        print(f"❌ 游戏重置执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    # 验证重置结果
    print("\n5. 验证重置结果...")

    # 检查游戏状态
    success = True

    if controller.hand_count != 0:
        print(f"❌ 手牌计数未重置: {controller.hand_count}")
        success = False
    else:
        print("✅ 手牌计数已重置")

    if controller.global_alert_level != 0.0:
        print(f"❌ 警戒等级未重置: {controller.global_alert_level}")
        success = False
    else:
        print("✅ 警戒等级已重置")

    # 检查AI信息
    for i, player in enumerate(controller.players):
        if player.experience != 0.0:
            print(f"❌ 玩家{i}经验未重置: {player.experience}")
            success = False
        else:
            print(f"✅ 玩家{i}经验已重置")

        if player.persona_tags:
            print(f"❌ 玩家{i}人设标签未重置: {player.persona_tags}")
            success = False
        else:
            print(f"✅ 玩家{i}人设标签已重置")

        if player.persona_text != "":
            print(f"❌ 玩家{i}人设文本未重置: {player.persona_text}")
            success = False
        else:
            print(f"✅ 玩家{i}人设文本已重置")

        if player.cheat_attempts != 0 or player.cheat_success != 0 or player.mindgame_moves != 0:
            print(f"❌ 玩家{i}统计未重置: 作弊{player.cheat_attempts}/{player.cheat_success}, 心理{player.mindgame_moves}")
            success = False
        else:
            print(f"✅ 玩家{i}统计已重置")

        if player.inventory:
            print(f"❌ 玩家{i}道具未重置: {player.inventory}")
            success = False
        else:
            print(f"✅ 玩家{i}道具已重置")

        if player.loan_data:
            print(f"❌ 玩家{i}贷款未重置: {player.loan_data}")
            success = False
        else:
            print(f"✅ 玩家{i}贷款已重置")

    # 检查游戏控制器状态
    if any(controller.player_personas):
        print(f"❌ 人设记录未重置: {controller.player_personas}")
        success = False
    else:
        print("✅ 人设记录已重置")

    if controller.player_private_impressions:
        print(f"❌ 印象记录未重置: {controller.player_private_impressions}")
        success = False
    else:
        print("✅ 印象记录已重置")

    if controller.player_reflections:
        print(f"❌ 反思记录未重置: {controller.player_reflections}")
        success = False
    else:
        print("✅ 反思记录已重置")

    # 检查日志清理
    if controller.secret_message_log:
        print(f"❌ 密信日志未清理: {controller.secret_message_log}")
        success = False
    else:
        print("✅ 密信日志已清理")

    if controller.cheat_action_log:
        print(f"❌ 作弊日志未清理: {controller.cheat_action_log}")
        success = False
    else:
        print("✅ 作弊日志已清理")

    if controller.public_event_log:
        print(f"❌ 公共事件日志未清理: {len(controller.public_event_log)} 项")
        success = False
    else:
        print("✅ 公共事件日志已清理")

    if controller.used_personas:
        print(f"❌ 使用过的人设未清理: {controller.used_personas}")
        success = False
    else:
        print("✅ 使用过的人设已清理")

    # 检查日志文件是否生成
    log_dir = Path("logs")
    if log_dir.exists():
        log_files = list(log_dir.glob("final_game_log_*.txt"))
        if log_files:
            print(f"✅ 游戏日志文件已生成: {log_files[-1].name}")
        else:
            print("❌ 游戏日志文件未生成")
            success = False
    else:
        print("❌ 日志目录未创建")
        success = False

    # 总结测试结果
    print("\n" + "=" * 60)
    if success:
        print("🎉 所有测试通过！游戏结束和信息重置功能工作正常")
    else:
        print("❌ 部分测试失败，请检查实现")
    print("=" * 60)

    return success


async def test_game_final_summary():
    """测试游戏最终总结功能"""
    print("\n测试游戏最终总结功能...")

    from zhajinhua import ZhajinhuaGame, GameConfig

    # 创建游戏
    game = ZhajinhuaGame(
        config=GameConfig(num_players=3, initial_chips=1000),
        initial_chips_list=[500, 800, 1200]
    )

    # 模拟游戏结束状态
    game.state.finished = True
    game.state.winner = 1
    game.state.pot_at_showdown = 300
    game.state.round_count = 5

    # 获取总结
    summary = game.get_final_summary()

    expected_keys = ["game_finished", "winner", "final_pot", "total_rounds", "player_final_states"]

    for key in expected_keys:
        if key not in summary:
            print(f"❌ 总结缺少键: {key}")
            return False

    print(f"✅ 游戏总结生成成功")
    print(f"   - 获胜者: 玩家 {summary['winner']}")
    print(f"   - 最终底池: {summary['final_pot']}")
    print(f"   - 总回合数: {summary['total_rounds']}")
    print(f"   - 玩家状态数量: {len(summary['player_final_states'])}")

    return True


async def main():
    """主测试函数"""
    print("开始游戏结束和信息重置功能测试")

    try:
        # 测试1: 游戏重置功能
        test1_success = await test_game_reset_functionality()

        # 测试2: 游戏总结功能
        test2_success = await test_game_final_summary()

        # 总体结果
        print("\n" + "🎯" * 20)
        if test1_success and test2_success:
            print("🎊 所有测试通过！功能实现正确")
            return 0
        else:
            print("⚠️  部分测试失败，需要修复")
            return 1

    except Exception as e:
        print(f"❌ 测试过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    # 运行测试
    exit_code = asyncio.run(main())
    sys.exit(exit_code)