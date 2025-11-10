"""
模型管理器
负责加载、管理和提供可用的模型配置
"""

import json
import os
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import logging


class ModelManager:
    """模型管理器类"""

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化模型管理器

        Args:
            config_path: 模型配置文件路径，默认为 config/models.json
        """
        if config_path is None:
            config_path = Path(__file__).parent / "config" / "models.json"

        self.config_path = Path(config_path)
        self.available_models: List[Dict] = []
        self.settings: Dict = {}
        self.load_config()

    def load_config(self) -> None:
        """加载模型配置"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.available_models = config.get('available_models', [])
                    self.settings = config.get('settings', {})
                    logging.info(f"已加载 {len(self.available_models)} 个模型配置")
            else:
                logging.warning(f"模型配置文件不存在: {self.config_path}")
                self._create_default_config()
        except Exception as e:
            logging.error(f"加载模型配置失败: {e}")
            self._create_default_config()

    def _create_default_config(self) -> None:
        """创建默认配置"""
        self.available_models = [
            {
                "id": "doubao-seed-1-6-lite-251015",
                "name": "DouBao",
                "display_name": "豆包 (轻量版)",
                "description": "字节跳动的轻量级模型，响应快速",
                "provider": "bytedance",
                "enabled": True,
                "selected": True
            },
            {
                "id": "moonshotai/Kimi-K2-Instruct-0905",
                "name": "kimiK2",
                "display_name": "Kimi K2",
                "description": "月之暗面的Kimi K2模型，长文本处理能力强",
                "provider": "moonshot",
                "enabled": True,
                "selected": True
            },
            {
                "id": "deepseek-ai/DeepSeek-V3",
                "name": "deepseekv3",
                "display_name": "DeepSeek V3",
                "description": "深度求索的V3版本模型",
                "provider": "deepseek",
                "enabled": True,
                "selected": True
            }
        ]

        self.settings = {
            "min_selected_models": 2,
            "max_selected_models": 8,
            "default_models": [
                "doubao-seed-1-6-lite-251015",
                "moonshotai/Kimi-K2-Instruct-0905",
                "deepseek-ai/DeepSeek-V3"
            ]
        }

        self.save_config()

    def save_config(self) -> None:
        """保存模型配置"""
        try:
            # 确保目录存在
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            config = {
                "available_models": self.available_models,
                "settings": self.settings
            }

            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            logging.info("模型配置已保存")
        except Exception as e:
            logging.error(f"保存模型配置失败: {e}")

    def get_available_models(self) -> List[Dict]:
        """获取所有可用模型"""
        return self.available_models.copy()

    def get_enabled_models(self) -> List[Dict]:
        """获取所有启用的模型"""
        return [model for model in self.available_models if model.get('enabled', True)]

    def get_selected_models(self) -> List[Dict]:
        """获取当前选中的模型"""
        return [model for model in self.available_models if model.get('selected', False)]

    def get_selected_model_configs(self) -> List[Dict]:
        """获取选中模型的配置（用于游戏初始化）"""
        selected = self.get_selected_models()
        if len(selected) < self.settings.get('min_selected_models', 2):
            # 如果选中的模型太少，使用默认模型
            logging.warning("选中的模型数量不足，使用默认模型")
            default_ids = self.settings.get('default_models', [])
            selected = [model for model in self.available_models
                       if model['id'] in default_ids]

        return [
            {
                "name": model["name"],
                "model": model["id"]
            }
            for model in selected
        ]

    def update_model_selection(self, model_ids: List[str]) -> Tuple[bool, str]:
        """
        更新模型选择

        Args:
            model_ids: 选中的模型ID列表

        Returns:
            (成功状态, 错误消息)
        """
        min_count = self.settings.get('min_selected_models', 2)
        max_count = self.settings.get('max_selected_models', 8)

        if len(model_ids) < min_count:
            return False, f"至少需要选择 {min_count} 个模型"

        if len(model_ids) > max_count:
            return False, f"最多只能选择 {max_count} 个模型"

        # 验证所有模型ID都存在
        available_ids = {model['id'] for model in self.available_models}
        invalid_ids = [mid for mid in model_ids if mid not in available_ids]
        if invalid_ids:
            return False, f"无效的模型ID: {', '.join(invalid_ids)}"

        # 更新选择状态
        for model in self.available_models:
            model['selected'] = model['id'] in model_ids

        self.save_config()
        return True, "模型选择已更新"

    def enable_model(self, model_id: str) -> Tuple[bool, str]:
        """启用模型"""
        for model in self.available_models:
            if model['id'] == model_id:
                model['enabled'] = True
                self.save_config()
                return True, "模型已启用"
        return False, "模型不存在"

    def disable_model(self, model_id: str) -> Tuple[bool, str]:
        """禁用模型"""
        for model in self.available_models:
            if model['id'] == model_id:
                model['enabled'] = False
                model['selected'] = False  # 禁用时也取消选择
                self.save_config()
                return True, "模型已禁用"
        return False, "模型不存在"

    def add_model(self, model_config: Dict) -> Tuple[bool, str]:
        """
        添加新模型

        Args:
            model_config: 模型配置字典

        Returns:
            (成功状态, 错误消息)
        """
        required_fields = ['id', 'name', 'display_name', 'provider']
        for field in required_fields:
            if field not in model_config:
                return False, f"缺少必需字段: {field}"

        # 检查ID是否已存在
        if any(model['id'] == model_config['id'] for model in self.available_models):
            return False, "模型ID已存在"

        # 设置默认值
        model_config.setdefault('description', '')
        model_config.setdefault('enabled', True)
        model_config.setdefault('selected', False)

        self.available_models.append(model_config)
        self.save_config()
        return True, "模型已添加"

    def remove_model(self, model_id: str) -> Tuple[bool, str]:
        """移除模型"""
        original_count = len(self.available_models)
        self.available_models = [
            model for model in self.available_models
            if model['id'] != model_id
        ]

        if len(self.available_models) < original_count:
            self.save_config()
            return True, "模型已移除"
        return False, "模型不存在"

    def update_model(self, model_id: str, updates: Dict) -> Tuple[bool, str]:
        """更新模型配置"""
        for model in self.available_models:
            if model['id'] == model_id:
                # 不允许更新ID
                if 'id' in updates:
                    del updates['id']

                model.update(updates)
                self.save_config()
                return True, "模型配置已更新"
        return False, "模型不存在"

    def reset_to_defaults(self) -> None:
        """重置为默认配置"""
        default_ids = self.settings.get('default_models', [])
        for model in self.available_models:
            model['selected'] = model['id'] in default_ids
        self.save_config()

    def get_settings(self) -> Dict:
        """获取设置"""
        return self.settings.copy()

    def update_settings(self, new_settings: Dict) -> Tuple[bool, str]:
        """更新设置"""
        try:
            self.settings.update(new_settings)
            self.save_config()
            return True, "设置已更新"
        except Exception as e:
            return False, f"更新设置失败: {e}"

    def validate_selection(self) -> Tuple[bool, str]:
        """验证当前选择是否有效"""
        selected_count = len(self.get_selected_models())
        min_count = self.settings.get('min_selected_models', 2)
        max_count = self.settings.get('max_selected_models', 8)

        if selected_count < min_count:
            return False, f"当前选中 {selected_count} 个模型，至少需要 {min_count} 个"

        if selected_count > max_count:
            return False, f"当前选中 {selected_count} 个模型，最多允许 {max_count} 个"

        return True, "选择有效"


# 全局模型管理器实例
model_manager = ModelManager()