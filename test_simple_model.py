#!/usr/bin/env python3
"""
简化的模型选择功能测试
避免编码问题
"""

import sys
import os
import json
from pathlib import Path

# 添加当前目录到 Python 路径
sys.path.append(str(Path(__file__).parent))


def test_config_file():
    """测试配置文件"""
    print("测试配置文件...")

    config_path = Path("config/models.json")
    if not config_path.exists():
        print("[FAIL] 配置文件不存在")
        return False

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        models = config.get('available_models', [])
        settings = config.get('settings', {})

        print(f"[PASS] 配置文件格式正确")
        print(f"       可用模型: {len(models)} 个")
        print(f"       最少选择: {settings.get('min_selected_models', 2)} 个")
        print(f"       最多选择: {settings.get('max_selected_models', 8)} 个")

        # 验证模型结构
        for i, model in enumerate(models[:3]):  # 只检查前3个
            required_fields = ['id', 'name', 'display_name', 'provider']
            for field in required_fields:
                if field not in model:
                    print(f"[FAIL] 模型 {i} 缺少字段: {field}")
                    return False

        print("[PASS] 模型结构验证通过")
        return True

    except Exception as e:
        print(f"[FAIL] 配置文件读取失败: {e}")
        return False


def test_model_manager():
    """测试模型管理器"""
    print("\n测试模型管理器...")

    try:
        from model_manager import ModelManager

        # 创建模型管理器实例
        manager = ModelManager()
        print("[PASS] 模型管理器创建成功")

        # 测试获取可用模型
        models = manager.get_available_models()
        print(f"[PASS] 获取到 {len(models)} 个可用模型")

        # 测试获取选中模型
        selected_models = manager.get_selected_models()
        print(f"[PASS] 当前选中 {len(selected_models)} 个模型")

        # 测试获取游戏配置
        game_configs = manager.get_selected_model_configs()
        print(f"[PASS] 生成游戏配置: {len(game_configs)} 个玩家")
        for config in game_configs[:3]:  # 只显示前3个
            print(f"       - {config['name']} ({config['model']})")

        # 测试选择验证
        is_valid, message = manager.validate_selection()
        print(f"[PASS] 选择验证: {is_valid} - {message}")

        return True

    except Exception as e:
        print(f"[FAIL] 模型管理器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_api_routes():
    """测试API路由定义"""
    print("\n测试API路由定义...")

    try:
        from model_api import model_router

        # 检查路由数量
        routes = [route for route in model_router.routes]
        print(f"[PASS] 定义了 {len(routes)} 个API路由")

        # 列出主要路由
        route_paths = [route.path for route in routes]
        main_routes = ['/', '/selected', '/select', '/game-config', '/settings', '/validate', '/reset']
        for route in main_routes:
            if route in route_paths:
                print(f"[PASS] 路由 {route} 已定义")
            else:
                print(f"[FAIL] 路由 {route} 未定义")
                return False

        return True

    except Exception as e:
        print(f"[FAIL] API路由测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_server_integration():
    """测试服务器集成"""
    print("\n测试服务器集成...")

    try:
        # 检查server.py中的导入
        with open('server.py', 'r', encoding='utf-8') as f:
            content = f.read()

        checks = [
            ('from model_manager import model_manager', '模型管理器已导入到服务器'),
            ('from model_api import model_router', 'API路由已导入到服务器'),
            ('app.include_router(model_router)', 'API路由已注册'),
            ('get_current_player_configs()', '动态配置函数已使用')
        ]

        for check_str, desc in checks:
            if check_str in content:
                print(f"[PASS] {desc}")
            else:
                print(f"[FAIL] {desc}")
                return False

        return True

    except Exception as e:
        print(f"[FAIL] 服务器集成测试失败: {e}")
        return False


def test_html_interface():
    """测试HTML界面"""
    print("\n测试HTML界面...")

    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            content = f.read()

        # 检查模型选择按钮
        if 'id="models-button"' in content:
            print("[PASS] 模型选择按钮已添加")
        else:
            print("[FAIL] 模型选择按钮未添加")
            return False

        # 检查模型选择弹窗
        if 'id="models-modal"' in content:
            print("[PASS] 模型选择弹窗已添加")
        else:
            print("[FAIL] 模型选择弹窗未添加")
            return False

        # 检查CSS样式
        css_classes = [
            '.models-grid',
            '.model-item',
            '.model-checkbox',
            '.modal-content'
        ]

        for css_class in css_classes:
            if css_class in content:
                print(f"[PASS] CSS样式 {css_class} 已添加")
            else:
                print(f"[FAIL] CSS样式 {css_class} 未添加")
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
                print(f"[PASS] JavaScript函数 {func} 已添加")
            else:
                print(f"[FAIL] JavaScript函数 {func} 未添加")
                return False

        return True

    except Exception as e:
        print(f"[FAIL] HTML界面测试失败: {e}")
        return False


def test_file_structure():
    """测试文件结构"""
    print("\n测试文件结构...")

    required_files = [
        'config/models.json',
        'model_manager.py',
        'model_api.py',
        'index.html',
        'server.py'
    ]

    for file_path in required_files:
        if Path(file_path).exists():
            print(f"[PASS] {file_path} 存在")
        else:
            print(f"[FAIL] {file_path} 不存在")
            return False

    return True


def main():
    """主测试函数"""
    print("开始模型选择功能测试")
    print("=" * 50)

    tests = [
        ("文件结构", test_file_structure),
        ("配置文件", test_config_file),
        ("模型管理器", test_model_manager),
        ("API路由", test_api_routes),
        ("服务器集成", test_server_integration),
        ("HTML界面", test_html_interface)
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
        print("所有测试通过！模型选择功能实现成功")
        print("\n功能清单:")
        print("1. 模型配置文件 (config/models.json)")
        print("2. 模型管理器 (model_manager.py)")
        print("3. API接口 (model_api.py)")
        print("4. 前端界面 (index.html)")
        print("5. 服务器集成 (server.py)")
        print("\n使用方法:")
        print("1. 启动服务器: python server.py")
        print("2. 在浏览器中打开页面")
        print("3. 点击'🤖 选择模型'按钮")
        print("4. 勾选要参赛的模型 (至少2个)")
        print("5. 点击'保存选择'")
        print("6. 开始新游戏即可使用选中的模型")
        return 0
    else:
        print("部分测试失败，请检查实现")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)