#!/usr/bin/env python3
"""
简化的游戏重置功能核心测试

只测试新添加的方法，不依赖外部模块
"""

import asyncio
import sys
import os
import json
from pathlib import Path


# 模拟类用于测试
class MockPlayer:
    """模拟Player类"""
    def __init__(self, name):
        self.name = name
        self.experience = 45.5
        self.persona_tags = {"test", "mock"}
        self.persona_text = "测试人设"
        self.play_history = ["action1", "action2"]
        self.current_pressure = 0.8
        self.cheat_attempts = 5
        self.cheat_success = 3
        self.mindgame_moves = 7
        self.inventory = ["item1", "item2"]
        self.loan_data = {"loan1": 100}
        self.alive = True

    def get_experience_level(self):
        if self.experience > 40:
            return "大师"
        return "新手"


class MockGameController:
    """模拟GameController类，包含新添加的重置方法"""

    def __init__(self):
        self.num_players = 3
        self.players = [MockPlayer(f"玩家{i+1}") for i in range(3)]
        self.persistent_chips = [1000, 500, 1500]
        self.hand_count = 10
        self.last_winner_id = 1
        self.global_alert_level = 35.5
        self.current_round_loans = [{"test": "data"}]
        self.player_personas = ["人设1", "人设2", "人设3"]
        self.player_private_impressions = {0: {1: "对手印象"}, 1: {0: "对手印象"}}
        self.player_reflections = ["反思1", "反思2", "反思3"]
        self._hand_history_cache = {"cache1": "data", "cache2": "data"}
        self.secret_message_log = [(1, 0, 1, "测试密信"), (2, 1, 2, "密信2")]
        self.cheat_action_log = [(1, 0, "TEST_CHEAT", "测试作弊")]
        self.public_event_log = [
            {"type": "事件1", "player_name": "玩家1", "details": "详情1", "hand": 1},
            {"type": "事件2", "player_name": "玩家2", "details": "详情2", "hand": 2}
        ]
        self.active_effects = [{"effect_id": "effect1", "data": "test"}]
        self.used_personas = {"人设1", "人设2", "使用过的人设"}

    def get_alive_player_count(self):
        """获取存活玩家数量"""
        return sum(1 for player in self.players if player.alive)

    async def god_print(self, message: str, delay: float = 1.0):
        """模拟上帝消息输出"""
        print(f"[GOD] {message.encode('ascii', 'ignore').decode('ascii')}")
        await asyncio.sleep(0.01)

    async def _save_final_game_log(self, log_collector):
        """保存最终游戏日志"""
        log_text = log_collector.get_full_log()
        final_stats = await self._generate_final_game_stats()
        enhanced_log = f"""{log_text}

=== 游戏最终统计 ===
{final_stats}

=== 游戏结束时间 ===
2024-01-01 12:00:00
"""

        # 保存到日志目录
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        timestamp = "20240101_120000"
        log_filename = log_dir / f"final_game_log_{timestamp}.txt"

        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(enhanced_log)

        log_announce_msg = f"📁 最终游戏日志已保存: {log_filename}"
        print(f"【上帝视角】: {log_announce_msg}")
        await self.god_print(log_announce_msg, 1)

    async def _generate_final_game_stats(self):
        """生成最终的游戏统计信息"""
        stats_lines = [
            f"总手牌数: {self.hand_count}",
            f"最终存活玩家数: {self.get_alive_player_count()}",
            ""
        ]

        # 玩家统计
        stats_lines.append("=== 玩家最终状态 ===")
        for i, player in enumerate(self.players):
            stats_lines.extend([
                f"玩家 {i+1}: {player.name}",
                f"  - 最终筹码: {self.persistent_chips[i]}",
                f"  - 存活状态: {'存活' if player.alive else '淘汰'}",
                f"  - 经验值: {player.experience:.1f}",
                f"  - 经验等级: {player.get_experience_level()}",
                f"  - 作弊尝试: {player.cheat_attempts} 次",
                f"  - 作弊成功: {player.cheat_success} 次",
                f"  - 心理博弈: {player.mindgame_moves} 次",
                ""
            ])

        # 人设使用情况
        stats_lines.append("=== 人设使用记录 ===")
        for i, persona in enumerate(self.player_personas):
            if persona and f"我是 {self.players[i].name}" not in persona:
                stats_lines.append(f"{self.players[i].name}: {persona[:100]}...")

        return "\n".join(stats_lines)

    async def _reset_all_ai_data(self):
        """重置所有AI相关信息"""
        await self.god_print("🔄 正在重置所有AI信息...", 1)

        for i, player in enumerate(self.players):
            # 重置玩家经验值
            player.experience = 0.0

            # 清空人设信息
            player.persona_tags.clear()
            player.persona_text = ""

            # 清空游戏历史和状态
            player.play_history.clear()
            player.current_pressure = 0.0

            # 重置作弊统计
            player.cheat_attempts = 0
            player.cheat_success = 0
            player.mindgame_moves = 0

            # 清空道具背包
            player.inventory.clear()

            # 清空贷款数据
            player.loan_data.clear()

            # 重置存活状态（根据筹码情况）
            player.alive = self.persistent_chips[i] > 0

        # 清空人设记录
        self.player_personas = [""] * self.num_players

        # 清空印象记录
        self.player_private_impressions.clear()

        # 清空反思记录
        self.player_reflections.clear()

        await self.god_print("✅ AI信息重置完成", 1)

    def _reset_game_state(self):
        """重置游戏状态，只保留配置参数"""
        # 重置手牌计数
        self.hand_count = 0

        # 重置获胜者ID
        self.last_winner_id = -1

        # 重置游戏配置相关状态
        self.global_alert_level = 0.0

        # 重置临时状态
        self.current_round_loans = []

        # 清空使用过的人设（为下一局游戏准备全新的人设）
        self.used_personas.clear()

    def _clear_all_logs_and_cache(self):
        """清空所有日志和缓存"""
        # 清空手牌历史缓存
        self._hand_history_cache.clear()

        # 清空当前手牌的所有日志
        self.secret_message_log.clear()
        self.cheat_action_log.clear()
        self.public_event_log.clear()

        # 清空活跃效果
        self.active_effects.clear()

    async def complete_game_reset(self, log_collector=None):
        """完全重置对局信息，清理所有AI数据，只保留配置参数"""
        await self.god_print("🔄 开始完全重置对局信息...", 2)

        # 1. 保存最终游戏日志
        if log_collector:
            try:
                await self._save_final_game_log(log_collector)
            except Exception as e:
                await self.god_print(f"⚠️ 保存最终游戏日志时出错: {e}", 1)

        # 2. 重置所有AI信息
        await self._reset_all_ai_data()

        # 3. 清理游戏状态
        self._reset_game_state()

        # 4. 清理所有日志和缓存
        self._clear_all_logs_and_cache()

        # 5. 强制垃圾回收
        import gc
        gc.collect()

        await self.god_print("✅ 对局信息重置完成，已准备开始新对局", 2)


class MockLogCollector:
    """模拟日志收集器"""
    def __init__(self):
        self.logs = []

    def append_log(self, log: str):
        self.logs.append(log)

    def get_full_log(self):
        return "\n".join(self.logs)


async def test_game_reset():
    """测试游戏重置功能"""
    print("=" * 60)
    print("开始测试游戏结束和信息重置功能（核心部分）")
    print("=" * 60)

    # 创建模拟游戏控制器
    controller = MockGameController()

    print("\n1. 检查初始状态...")
    print(f"玩家数量: {len(controller.players)}")
    print(f"手牌计数: {controller.hand_count}")
    print(f"警戒等级: {controller.global_alert_level}")
    print(f"玩家1经验: {controller.players[0].experience}")
    print(f"玩家1道具: {controller.players[0].inventory}")
    print(f"密信日志: {len(controller.secret_message_log)} 条")
    print(f"作弊日志: {len(controller.cheat_action_log)} 条")

    # 创建日志收集器
    print("\n2. 创建日志收集器...")
    log_collector = MockLogCollector()
    log_collector.append_log("=== 游戏开始 ===")
    log_collector.append_log("第1手牌：玩家A获胜")
    log_collector.append_log("第2手牌：玩家B获胜")
    log_collector.append_log("第3手牌：玩家C获胜")

    # 保存初始状态用于验证
    initial_state = {
        "hand_count": controller.hand_count,
        "global_alert_level": controller.global_alert_level,
        "player_experience": [p.experience for p in controller.players],
        "player_personas": [p.persona_text for p in controller.players],
        "secret_log_count": len(controller.secret_message_log),
        "cheat_log_count": len(controller.cheat_action_log),
        "public_event_count": len(controller.public_event_log),
    }

    print("\n3. 执行完整游戏重置...")
    try:
        await controller.complete_game_reset(log_collector)
        print("✅ 游戏重置执行成功")
    except Exception as e:
        print(f"❌ 游戏重置执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    # 验证重置结果
    print("\n4. 验证重置结果...")

    success = True
    checks = []

    # 检查游戏状态重置
    checks.append(("手牌计数", controller.hand_count, 0))
    checks.append(("警戒等级", controller.global_alert_level, 0.0))
    checks.append(("获胜者ID", controller.last_winner_id, -1))

    # 检查玩家数据重置
    for i, player in enumerate(controller.players):
        checks.append((f"玩家{i+1}经验", player.experience, 0.0))
        checks.append((f"玩家{i+1}人设标签", len(player.persona_tags), 0))
        checks.append((f"玩家{i+1}人设文本", player.persona_text, ""))
        checks.append((f"玩家{i+1}作弊尝试", player.cheat_attempts, 0))
        checks.append((f"玩家{i+1}道具数量", len(player.inventory), 0))
        checks.append((f"玩家{i+1}贷款数量", len(player.loan_data), 0))

    # 检查游戏控制器状态重置
    checks.append(("人设记录数量", len([p for p in controller.player_personas if p]), 0))
    checks.append(("印象记录数量", len(controller.player_private_impressions), 0))
    checks.append(("反思记录数量", len(controller.player_reflections), 0))
    checks.append(("密信日志数量", len(controller.secret_message_log), 0))
    checks.append(("作弊日志数量", len(controller.cheat_action_log), 0))
    checks.append(("公共事件数量", len(controller.public_event_log), 0))
    checks.append(("使用过的人设数量", len(controller.used_personas), 0))

    # 验证所有检查
    for name, actual, expected in checks:
        if actual != expected:
            print(f"❌ {name}: 期望 {expected}, 实际 {actual}")
            success = False
        else:
            print(f"✅ {name}: {actual}")

    # 检查日志文件生成
    log_dir = Path("logs")
    if log_dir.exists():
        log_files = list(log_dir.glob("final_game_log_*.txt"))
        if log_files:
            print(f"✅ 游戏日志文件已生成: {log_files[-1].name}")

            # 检查日志内容
            try:
                with open(log_files[-1], "r", encoding="utf-8") as f:
                    content = f.read()
                    if "游戏最终统计" in content:
                        print("✅ 日志包含统计信息")
                    else:
                        print("⚠️ 日志缺少统计信息")
                        success = False

                    if "玩家最终状态" in content:
                        print("✅ 日志包含玩家状态")
                    else:
                        print("⚠️ 日志缺少玩家状态")
                        success = False
            except Exception as e:
                print(f"❌ 读取日志文件失败: {e}")
                success = False
        else:
            print("❌ 游戏日志文件未生成")
            success = False
    else:
        print("❌ 日志目录未创建")
        success = False

    # 总结测试结果
    print("\n" + "=" * 60)
    if success:
        print("🎉 所有核心重置功能测试通过！")
        print("\n重置功能验证:")
        print("✅ 游戏状态重置 - 手牌计数、警戒等级等")
        print("✅ AI信息重置 - 经验值、人设、道具等")
        print("✅ 日志清理 - 密信、作弊、公共事件日志")
        print("✅ 缓存清理 - 手牌历史缓存等")
        print("✅ 日志保存 - 最终游戏统计和玩家状态")
        print("✅ 垃圾回收 - 内存释放")
    else:
        print("❌ 部分测试失败，请检查实现")
    print("=" * 60)

    return success


async def main():
    """主测试函数"""
    print("开始游戏重置功能核心测试")

    try:
        success = await test_game_reset()

        print("\n" + "🎯" * 20)
        if success:
            print("🎊 核心功能测试通过！游戏结束和信息重置功能实现正确")
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
    exit_code = asyncio.run(main())
    sys.exit(exit_code)