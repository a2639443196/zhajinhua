#!/usr/bin/env python
"""
测试提示词管理器功能
"""
import os
import sys
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from prompt_manager import prompt_manager

def test_basic_functionality():
    """测试基本功能"""
    print("=== 测试基本功能 ===")

    # 测试获取原始提示词
    print("\n1. 获取decide_action_prompt.txt:")
    prompt = prompt_manager.get_prompt('decide_action_prompt.txt')
    print(f"提示词长度: {len(prompt)}")

    # 检查变量是否被替换
    if '{CHIP_WARNING_THRESHOLD}' in prompt:
        print("❌ 错误：变量未被替换")
    else:
        print("✅ 成功：变量已被替换")

    # 测试获取特定内容
    print("\n2. 测试警戒线替换:")
    if "筹码 vs 300阈值" in prompt:
        print("✅ 默认值300已应用")
    else:
        print("❌ 默认值未应用")

    # 测试设置新变量
    print("\n3. 测试动态设置警戒线为500:")
    prompt_manager.set_variable('CHIP_WARNING_THRESHOLD', 500)
    new_prompt = prompt_manager.get_prompt('decide_action_prompt.txt')

    if "筹码 vs 500阈值" in new_prompt:
        print("✅ 动态设置成功")
    else:
        print("❌ 动态设置失败")

    # 测试数学表达式
    print("\n4. 测试数学表达式:")
    math_prompt = prompt_manager.get_prompt('decide_action_prompt.txt')
    if "筹码 < 835" in math_prompt:  # 500 * 1.67 = 835
        print("✅ 数学表达式计算成功")
    else:
        print("❌ 数学表达式计算失败")

    # 测试条件表达式
    print("\n5. 测试条件表达式:")
    test_content = "测试{if 500 > 300}显示{endif}内容"
    result = prompt_manager._process_conditionals(test_content, {'CHIP_WARNING_THRESHOLD': 500})
    if "测试显示内容" == result:
        print("✅ 条件表达式成功")
    else:
        print("❌ 条件表达式失败")

def test_api_integration():
    """测试API集成"""
    print("\n=== 测试API集成 ===")

    # 创建一个简单的测试API
    try:
        from fastapi import FastAPI
        from prompt_api import router as prompt_router

        app = FastAPI()
        app.include_router(prompt_router)
        print("✅ API路由集成成功")

        # 测试获取变量
        import asyncio
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/api/prompts/variables")
        if response.status_code == 200:
            print("✅ 获取变量API正常")
            print(f"   变量: {response.json()}")
        else:
            print("❌ 获取变量API失败")

        # 测试更新变量
        response = client.post("/api/prompts/variables",
                              json={"key": "TEST_VAR", "value": 123})
        if response.status_code == 200:
            print("✅ 更新变量API正常")
        else:
            print("❌ 更新变量API失败")

    except Exception as e:
        print(f"❌ API集成失败: {e}")

def test_file_persistence():
    """测试文件持久化"""
    print("\n=== 测试文件持久化 ===")

    # 保存变量
    test_vars = {'TEST_PERSISTENCE': 'test_value', 'CHIP_WARNING_THRESHOLD': 400}
    prompt_manager.set_variables(test_vars)
    prompt_manager.save_variables('test_variables.json')

    # 检查文件是否存在
    if os.path.exists('test_variables.json'):
        print("✅ 变量保存成功")

        # 重新加载
        new_manager = PromptManager()
        new_manager.load_variables('test_variables.json')

        if new_manager.variables.get('TEST_PERSISTENCE') == 'test_value':
            print("✅ 变量加载成功")
        else:
            print("❌ 变量加载失败")
    else:
        print("❌ 变量保存失败")

    # 清理测试文件
    if os.path.exists('test_variables.json'):
        os.remove('test_variables.json')

if __name__ == "__main__":
    print("开始测试提示词管理器...")

    test_basic_functionality()
    test_api_integration()
    test_file_persistence()

    print("\n测试完成！")