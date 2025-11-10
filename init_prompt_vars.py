#!/usr/bin/env python
"""
初始化提示词变量
在程序启动时运行，设置默认值并加载保存的配置
"""
import os
from pathlib import Path
from prompt_manager import prompt_manager

def init_prompt_variables():
    """初始化提示词变量"""
    # 设置默认值
    defaults = {
        'CHIP_WARNING_THRESHOLD': 300,
        'CHIP_CRITICAL_THRESHOLD': 150,
        'BASE_CHEAT_SUCCESS_RATE': 16,
        'MIN_RAISE_PERCENTAGE': 5,
        'MAX_LOAN_TURNS': 6,
    }

    # 应用默认值（仅在变量不存在时）
    for key, value in defaults.items():
        if key not in prompt_manager.variables:
            prompt_manager.set_variable(key, value)

    # 尝试从配置文件加载
    config_file = Path('config/variables.json')
    if config_file.exists():
        prompt_manager.load_variables(str(config_file))
        print(f"已加载配置文件: {config_file}")

    # 保存当前配置
    prompt_manager.save_variables()
    print(f"提示词变量初始化完成:")
    for key, value in prompt_manager.variables.items():
        print(f"  {key}: {value}")

if __name__ == "__main__":
    init_prompt_variables()