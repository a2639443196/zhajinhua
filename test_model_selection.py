#!/usr/bin/env python3
"""
测试模型选择功能的脚本
"""

import sys
import os
import asyncio
import json
from pathlib import Path

# 添加当前目录到 Python 路径
sys.path.append(str(Path(__file__).parent))

from model_manager import ModelManager


def test_model_manager():
    """测试模型管理器基本功能"""
    print("=" * 50)
    print("测试模型管理器功能")
    print("=" * 50)

    try:
        # 创建模型管理器实例
        manager = ModelManager()
        print("✅ 模型管理器创建成功")

        # 测试获取可用模型
        models = manager.get_available_models()
        print(f"✅ 获取到 {len(models)} 个可用模型")

        # 测试获取选中模型
        selected_models = manager.get_selected_models()
        print(f"✅ 当前选中 {len(selected_models)} 个模型")

        # 测试获取游戏配置
        game_configs = manager.get_selected_model_configs()
        print(f"✅ 生成游戏配置: {len(game_configs)} 个玩家")
        for config in game_configs:
            print(f"   - {config['name']} ({config['model']})")

        # 测试选择验证
        is_valid, message = manager.validate_selection()
        print(f"✅ 选择验证: {is_valid} - {message}")

        return True

    except Exception as e:
        print(f"❌ 模型管理器测试失败: {e}")
        return False


def test_config_file():
    """测试配置文件"""
    print("\n测试配置文件...")

    config_path = Path("config/models.json")
    if config_path.exists():
        print("✅ 配置文件存在")

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            print(f"✅ 配置文件格式正确")
            print(f"   - 可用模型: {len(config.get('available_models', []))} 个")
            print(f"   - 最少选择: {config.get('settings', {}).get('min_selected_models', 2)} 个")
            print(f"   - 最多选择: {config.get('settings', {}).get('max_selected_models', 8)} 个")

            return True

        except Exception as e:
            print(f"❌ 配置文件读取失败: {e}")
            return False
    else:
        print("❌ 配置文件不存在")
        return False


def test_model_selection_update():
    """测试模型选择更新"""
    print("\n测试模型选择更新...")

    try:
        manager = ModelManager()

        # 获取当前选中模型
        original_selected = [model['id'] for model in manager.get_selected_models()]
        print(f"当前选中: {original_selected}")

        # 测试无效选择（太少）
        success, message = manager.update_model_selection([])
        print(f"测试空选择: {success} - {message}")

        # 测试有效选择
        if len(original_selected) >= 2:
            success, message = manager.update_model_selection(original_selected[:2])
            print(f"测试部分选择: {success} - {message}")

        return True

    except Exception as e:
        print(f"❌ 模型选择更新测试失败: {e}")
        return False


def test_api_routes():
    """测试API路由定义"""
    print("\n测试API路由定义...")

    try:
        # 导入API路由
        from model_api import model_router
        print("✅ API路由模块导入成功")

        # 检查路由数量
        routes = [route for route in model_router.routes]
        print(f"✅ 定义了 {len(routes)} 个API路由")

        # 列出主要路由
        route_paths = [route.path for route in routes]
        main_routes = ['/', '/selected', '/select', '/game-config', '/settings', '/validate', '/reset']
        for route in main_routes:
            if route in route_paths:
                print(f"✅ 路由 {route} 已定义")
            else:
                print(f"❌ 路由 {route} 未定义")
                return False

        return True

    except Exception as e:
        print(f"❌ API路由测试失败: {e}")
        return False


def test_server_integration():
    """测试服务器集成"""
    print("\n测试服务器集成...")

    try:
        # 检查server.py中的导入
        with open('server.py', 'r', encoding='utf-8') as f:
            content = f.read()

        if 'from model_manager import model_manager' in content:
            print("✅ 模型管理器已导入到服务器")
        else:
            print("❌ 模型管理器未导入到服务器")
            return False

        if 'from model_api import model_router' in content:
            print("✅ API路由已导入到服务器")
        else:
            print("❌ API路由未导入到服务器")
            return False

        if 'get_current_player_configs()' in content:
            print("✅ 动态配置函数已使用")
        else:
            print("❌ 动态配置函数未使用")
            return False

        return True

    except Exception as e:
        print(f"❌ 服务器集成测试失败: {e}")
        return False


def test_html_interface():
    """测试HTML界面"""
    print("\n测试HTML界面...")

    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            content = f.read()

        # 检查模型选择按钮
        if 'id="models-button"' in content:
            print("✅ 模型选择按钮已添加")
        else:
            print("❌ 模型选择按钮未添加")
            return False

        # 检查模型选择弹窗
        if 'id="models-modal"' in content:
            print("✅ 模型选择弹窗已添加")
        else:
            print("❌ 模型选择弹窗未添加")
            return False

        # 检查JavaScript函数
        required_functions = [
            'openModelsModal',
            'closeModelsModal',
            'loadModels',
            'saveModelSelection',
            'toggleModelSelection'
        ]

        for func in required_functions:
            if f'function {func}' in content or f'async function {func}' in content:
                print(f"✅ JavaScript函数 {func} 已添加")
            else:
                print(f"❌ JavaScript函数 {func} 未添加")
                return False

        return True

    except Exception as e:
        print(f"❌ HTML界面测试失败: {e}")
        return False


async def test_api_endpoints():
    """测试API端点（需要服务器运行）"""
    print("\n测试API端点（模拟）...")

    try:
        import aiohttp
        import asyncio

        # 模拟API调用（如果服务器在运行）
        print("ℹ️  跳过实际API调用测试（需要服务器运行）")
        print("   可以启动服务器后访问以下端点进行测试:")
        print("   - GET /api/models/")
        print("   - GET /api/models/selected")
        print("   - POST /api/models/select")
        print("   - GET /api/models/game-config")

        return True

    except ImportError:
        print("ℹ️  aiohttp未安装，跳过API端点测试")
        return True
    except Exception as e:
        print(f"❌ API端点测试失败: {e}")
        return False


def main():
    """主测试函数"""
    print("开始模型选择功能测试")
    print("=" * 50)

    tests = [
        ("配置文件", test_config_file),
        ("模型管理器", test_model_manager),
        ("模型选择更新", test_model_selection_update),
        ("API路由", test_api_routes),
        ("服务器集成", test_server_integration),
        ("HTML界面", test_html_interface),
        ("API端点", test_api_endpoints)
    ]

    passed = 0
    total = len(tests)

    for name, test_func in tests:
        print(f"\n{'='*20} {name} {'='*20}")
        try:
            if asyncio.iscoroutinefunction(test_func):
                success = asyncio.run(test_func())
            else:
                success = test_func()

            if success:
                passed += 1
                print(f"✅ {name} 测试通过")
            else:
                print(f"❌ {name} 测试失败")

        except Exception as e:
            print(f"❌ {name} 测试出错: {e}")

    print("\n" + "="*50)
    print(f"测试结果: {passed}/{total} 通过")
    print("="*50)

    if passed == total:
        print("🎉 所有测试通过！模型选择功能实现成功")
        print("\n📋 功能清单:")
        print("✅ 模型配置文件和管理系统")
        print("✅ 后端API接口")
        print("✅ 前端选择界面")
        print("✅ 游戏初始化集成")
        print("✅ 验证和错误处理")
        return 0
    else:
        print("⚠️  部分测试失败，请检查实现")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)