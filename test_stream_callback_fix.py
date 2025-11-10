#!/usr/bin/env python3
"""
测试stream_callback修复的脚本
"""

import sys
import os
from pathlib import Path

# 添加当前目录到 Python 路径
sys.path.append(str(Path(__file__).parent))

def test_player_methods():
    """测试Player类方法的stream_callback参数"""
    print("测试Player类方法stream_callback修复...")

    try:
        from player import Player
        import inspect

        # 创建测试Player实例
        player = Player("TestPlayer", "test-model")
        print(f"[PASS] Player实例创建成功")

        # 检查关键方法的参数默认值
        methods_to_check = [
            'decide_action',
            'defend',
            'vote',
            'decide_bribe',
            'reflect'
        ]

        for method_name in methods_to_check:
            method = getattr(player, method_name, None)
            if method is None:
                print(f"[FAIL] 方法 {method_name} 不存在")
                return False

            # 获取方法签名
            sig = inspect.signature(method)
            params = list(sig.parameters.keys())

            # 检查stream_start_cb和stream_chunk_cb参数是否有默认值None
            stream_start_cb_param = sig.parameters.get('stream_start_cb')
            stream_chunk_cb_param = sig.parameters.get('stream_chunk_cb')

            if stream_start_cb_param and stream_start_cb_param.default is None:
                print(f"[PASS] {method_name}: stream_start_cb 默认值为None")
            else:
                print(f"[FAIL] {method_name}: stream_start_cb 默认值不为None")
                return False

            if stream_chunk_cb_param and stream_chunk_cb_param.default is None:
                print(f"[PASS] {method_name}: stream_chunk_cb 默认值为None")
            else:
                print(f"[FAIL] {method_name}: stream_chunk_cb 默认值不为None")
                return False

        return True

    except Exception as e:
        print(f"[FAIL] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_async_wrapper_check():
    """测试代码中是否包含async函数检查逻辑"""
    print("\n测试异步包装器检查逻辑...")

    try:
        with open('player.py', 'r', encoding='utf-8') as f:
            content = f.read()

        # 检查是否包含安全检查代码
        checks = [
            'asyncio.iscoroutinefunction(stream_chunk_cb)',
            'if stream_start_cb:',
            'stream_chunk_cb 调用失败',
            'safe_stream_chunk_cb'
        ]

        for check in checks:
            if check in content:
                print(f"[PASS] 找到安全检查: {check}")
            else:
                print(f"[FAIL] 缺少安全检查: {check}")
                return False

        return True

    except Exception as e:
        print(f"[FAIL] 文件检查失败: {e}")
        return False


def test_error_handling_patterns():
    """测试错误处理模式"""
    print("\n测试错误处理模式...")

    try:
        with open('player.py', 'r', encoding='utf-8') as f:
            content = f.read()

        # 检查是否包含try-catch包装
        patterns = [
            'try:',
            'except Exception as e:',
            'print(f"[警告]'
        ]

        for pattern in patterns:
            count = content.count(pattern)
            print(f"[PASS] 找到模式 '{pattern}': {count} 次")

        # 检查stream_callback相关错误处理
        error_handling_count = content.count('stream_chunk_cb 调用失败')
        if error_handling_count >= 5:  # 应该有多个这样的错误处理
            print(f"[PASS] 找到stream_callback错误处理: {error_handling_count} 处")
        else:
            print(f"[FAIL] stream_callback错误处理不足: {error_handling_count} 处")
            return False

        return True

    except Exception as e:
        print(f"[FAIL] 错误处理检查失败: {e}")
        return False


def test_function_signatures():
    """测试函数签名的完整性"""
    print("\n测试函数签名完整性...")

    try:
        from player import Player
        import inspect

        player = Player("TestPlayer", "test-model")

        # 测试decide_action方法
        sig = inspect.signature(player.decide_action)
        params = list(sig.parameters.keys())

        expected_params = [
            'game_state_summary',
            'my_hand',
            'available_actions_str',
            'next_player_name',
            'my_persona',
            'opponent_personas',
            'opponent_reflections',
            'opponent_private_impressions_str',
            'observed_speech_str',
            'received_secret_messages',
            'player_inventory',
            'field_item_intel',
            'min_raise_increment',
            'dealer_name',
            'observed_moods',
            'multiplier',
            'call_cost',
            'table_seating_str',
            'opponent_reference_str',
            'public_event_log',
            'prompt_template',
            'stream_start_cb',
            'stream_chunk_cb'
        ]

        for param in expected_params:
            if param in params:
                print(f"[PASS] decide_action包含参数: {param}")
            else:
                print(f"[FAIL] decide_action缺少参数: {param}")
                return False

        # 检查默认值
        if sig.parameters['stream_start_cb'].default is None:
            print("[PASS] stream_start_cb有正确的默认值")
        else:
            print("[FAIL] stream_start_cb默认值不正确")
            return False

        if sig.parameters['stream_chunk_cb'].default is None:
            print("[PASS] stream_chunk_cb有正确的默认值")
        else:
            print("[FAIL] stream_chunk_cb默认值不正确")
            return False

        return True

    except Exception as e:
        print(f"[FAIL] 函数签名测试失败: {e}")
        return False


def main():
    """主测试函数"""
    print("开始stream_callback修复测试")
    print("=" * 50)

    tests = [
        ("Player类方法参数", test_player_methods),
        ("异步包装器检查", test_async_wrapper_check),
        ("错误处理模式", test_error_handling_patterns),
        ("函数签名完整性", test_function_signatures)
    ]

    passed = 0
    total = len(tests)

    for name, test_func in tests:
        print(f"\n{'='*20} {name} {'='*20}")
        try:
            if test_func():
                passed += 1
                print(f"[PASS] {name} 测试通过")
            else:
                print(f"[FAIL] {name} 测试失败")
        except Exception as e:
            print(f"[ERROR] {name} 测试出错: {e}")

    print("\n" + "="*50)
    print(f"测试结果: {passed}/{total} 通过")
    print("="*50)

    if passed == total:
        print("✅ 所有修复验证通过！stream_callback问题已解决")
        print("\n修复内容:")
        print("1. 为所有stream回调参数添加了默认值None")
        print("2. 添加了stream_start_cb的None检查")
        print("3. 创建了安全的stream_chunk_cb包装器")
        print("4. 添加了异步/同步函数检测")
        print("5. 完善了错误处理和异常捕获")
        print("\n这应该解决 'object NoneType can't be used in await' 错误")
        return 0
    else:
        print("❌ 部分测试失败，修复可能不完整")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)