"""
提示词管理器
支持动态变量替换，特别是警戒线筹码等可配置参数
"""
import os
import re
from typing import Dict, Any
import json

class PromptManager:
    def __init__(self):
        self.templates = {}
        self.variables = {
            'CHIP_WARNING_THRESHOLD': 300,  # 默认警戒线筹码
            'CHIP_CRITICAL_THRESHOLD': 150,  # 极度危险阈值
            'MIN_RAISE_PERCENTAGE': 5,  # 最小加注百分比
            'MAX_LOAN_TURNS': 6,  # 最大贷款手数
            'BASE_CHEAT_SUCCESS_RATE': 16,  # 基础作弊成功率
        }
        self.load_all_templates()

    def load_all_templates(self):
        """加载所有提示词模板"""
        prompt_dir = 'prompt'
        if not os.path.exists(prompt_dir):
            return

        for filename in os.listdir(prompt_dir):
            if filename.endswith('.txt'):
                filepath = os.path.join(prompt_dir, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.templates[filename] = content

    def set_variable(self, key: str, value: Any):
        """设置变量值"""
        self.variables[key] = value

    def set_variables(self, variables: Dict[str, Any]):
        """批量设置变量"""
        self.variables.update(variables)

    def get_prompt(self, template_name: str, **kwargs) -> str:
        """获取处理后的提示词"""
        if template_name not in self.templates:
            raise FileNotFoundError(f"Prompt template {template_name} not found")

        # 合并全局变量和传入的变量
        all_vars = self.variables.copy()
        all_vars.update(kwargs)

        # 处理模板
        content = self.templates[template_name]

        # 替换简单变量 {VAR_NAME}
        for key, value in all_vars.items():
            pattern = r'\{' + key + r'\}'
            content = re.sub(pattern, str(value), content)

        # 替换条件表达式 {if VAR > VALUE}...{endif}
        content = self._process_conditionals(content, all_vars)

        # 替换数学表达式 {math:VAR * 2}
        content = self._process_expressions(content, all_vars)

        return content

    def _process_conditionals(self, content: str, variables: Dict[str, Any]) -> str:
        """处理条件表达式"""
        # 匹配 {if condition}...{endif} 格式
        pattern = r'\{if\s+(.+?)\}(.*?)\{endif\}'

        def replace_conditional(match):
            condition = match.group(1)
            conditional_content = match.group(2)

            # 简单的条件评估
            try:
                # 替换变量名
                for key, value in variables.items():
                    condition = condition.replace(key, str(value))

                # 评估条件
                if eval(condition):
                    return conditional_content
                else:
                    return ""
            except:
                return conditional_content

        return re.sub(pattern, replace_conditional, content, flags=re.DOTALL)

    def _process_expressions(self, content: str, variables: Dict[str, Any]) -> str:
        """处理数学表达式"""
        # 匹配 {math:expression} 格式
        pattern = r'\{math:(.+?)\}'

        def replace_expression(match):
            expression = match.group(1)

            try:
                # 替换变量名
                for key, value in variables.items():
                    expression = expression.replace(key, str(value))

                # 计算表达式
                result = eval(expression)
                return str(result)
            except:
                return match.group(0)

        return re.sub(pattern, replace_expression, content)

    def reload_template(self, template_name: str):
        """重新加载指定模板"""
        filepath = os.path.join('prompt', template_name)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.templates[template_name] = content

    def reload_all_templates(self):
        """重新加载所有模板"""
        self.load_all_templates()

    def save_variables(self, filepath: str = 'config/variables.json'):
        """保存变量到文件"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.variables, f, indent=2, ensure_ascii=False)

    def load_variables(self, filepath: str = 'config/variables.json'):
        """从文件加载变量"""
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                self.variables.update(json.load(f))

# 全局提示词管理器实例
prompt_manager = PromptManager()