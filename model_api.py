"""
模型选择和管理的API路由
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional, Tuple
import logging

from model_manager import model_manager

# 创建路由器
model_router = APIRouter(prefix="/api/models", tags=["models"])

# Pydantic模型用于数据验证
class ModelConfig(BaseModel):
    id: str
    name: str
    display_name: str
    description: str = ""
    provider: str
    enabled: bool = True
    selected: bool = False

class ModelSelectionRequest(BaseModel):
    model_ids: List[str]

class ModelAddRequest(BaseModel):
    id: str
    name: str
    display_name: str
    description: str = ""
    provider: str
    enabled: bool = True

class ModelUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None

class SettingsUpdateRequest(BaseModel):
    min_selected_models: Optional[int] = None
    max_selected_models: Optional[int] = None
    default_models: Optional[List[str]] = None


@model_router.get("/", response_model=Dict)
async def get_models():
    """获取所有可用模型"""
    try:
        models = model_manager.get_available_models()
        settings = model_manager.get_settings()
        return {
            "models": models,
            "settings": settings,
            "selected_count": len([m for m in models if m.get('selected', False)]),
            "enabled_count": len([m for m in models if m.get('enabled', True)])
        }
    except Exception as e:
        logging.error(f"获取模型列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取模型列表失败: {str(e)}")


@model_router.get("/selected", response_model=List[ModelConfig])
async def get_selected_models():
    """获取当前选中的模型"""
    try:
        selected_models = model_manager.get_selected_models()
        return selected_models
    except Exception as e:
        logging.error(f"获取选中模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取选中模型失败: {str(e)}")


@model_router.post("/select", response_model=Dict)
async def update_model_selection(request: ModelSelectionRequest):
    """更新模型选择"""
    try:
        success, message = model_manager.update_model_selection(request.model_ids)
        if not success:
            raise HTTPException(status_code=400, detail=message)

        # 返回更新后的状态
        models = model_manager.get_available_models()
        selected_configs = model_manager.get_selected_model_configs()

        return {
            "success": True,
            "message": message,
            "selected_count": len(request.model_ids),
            "selected_models": selected_configs
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"更新模型选择失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新模型选择失败: {str(e)}")


@model_router.get("/game-config", response_model=List[Dict])
async def get_game_model_config():
    """获取用于游戏初始化的模型配置"""
    try:
        config = model_manager.get_selected_model_configs()

        if not config:
            # 如果没有选中的模型，返回默认配置
            logging.warning("没有选中的模型，返回默认配置")
            config = model_manager.get_selected_model_configs()  # 这会触发使用默认模型

        return config
    except Exception as e:
        logging.error(f"获取游戏模型配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取游戏模型配置失败: {str(e)}")


@model_router.post("/add", response_model=Dict)
async def add_model(request: ModelAddRequest):
    """添加新模型"""
    try:
        model_config = request.dict()
        success, message = model_manager.add_model(model_config)
        if not success:
            raise HTTPException(status_code=400, detail=message)

        return {
            "success": True,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"添加模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"添加模型失败: {str(e)}")


@model_router.delete("/{model_id}", response_model=Dict)
async def remove_model(model_id: str):
    """移除模型"""
    try:
        success, message = model_manager.remove_model(model_id)
        if not success:
            raise HTTPException(status_code=404, detail=message)

        return {
            "success": True,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"移除模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"移除模型失败: {str(e)}")


@model_router.put("/{model_id}", response_model=Dict)
async def update_model(model_id: str, request: ModelUpdateRequest):
    """更新模型配置"""
    try:
        updates = {k: v for k, v in request.dict().items() if v is not None}
        success, message = model_manager.update_model(model_id, updates)
        if not success:
            raise HTTPException(status_code=404, detail=message)

        return {
            "success": True,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"更新模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新模型失败: {str(e)}")


@model_router.post("/{model_id}/enable", response_model=Dict)
async def enable_model(model_id: str):
    """启用模型"""
    try:
        success, message = model_manager.enable_model(model_id)
        if not success:
            raise HTTPException(status_code=404, detail=message)

        return {
            "success": True,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"启用模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"启用模型失败: {str(e)}")


@model_router.post("/{model_id}/disable", response_model=Dict)
async def disable_model(model_id: str):
    """禁用模型"""
    try:
        success, message = model_manager.disable_model(model_id)
        if not success:
            raise HTTPException(status_code=404, detail=message)

        return {
            "success": True,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"禁用模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"禁用模型失败: {str(e)}")


@model_router.get("/settings", response_model=Dict)
async def get_settings():
    """获取模型设置"""
    try:
        settings = model_manager.get_settings()
        return settings
    except Exception as e:
        logging.error(f"获取设置失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取设置失败: {str(e)}")


@model_router.put("/settings", response_model=Dict)
async def update_settings(request: SettingsUpdateRequest):
    """更新模型设置"""
    try:
        updates = {k: v for k, v in request.dict().items() if v is not None}
        success, message = model_manager.update_settings(updates)
        if not success:
            raise HTTPException(status_code=400, detail=message)

        return {
            "success": True,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"更新设置失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新设置失败: {str(e)}")


@model_router.post("/reset", response_model=Dict)
async def reset_to_defaults():
    """重置为默认选择"""
    try:
        model_manager.reset_to_defaults()
        return {
            "success": True,
            "message": "已重置为默认模型选择"
        }
    except Exception as e:
        logging.error(f"重置默认选择失败: {e}")
        raise HTTPException(status_code=500, detail=f"重置默认选择失败: {str(e)}")


@model_router.get("/validate", response_model=Dict)
async def validate_selection():
    """验证当前模型选择"""
    try:
        success, message = model_manager.validate_selection()
        selected_models = model_manager.get_selected_models()

        return {
            "valid": success,
            "message": message,
            "selected_count": len(selected_models),
            "selected_models": [m['display_name'] for m in selected_models]
        }
    except Exception as e:
        logging.error(f"验证模型选择失败: {e}")
        raise HTTPException(status_code=500, detail=f"验证模型选择失败: {str(e)}")


@model_router.get("/providers", response_model=List[str])
async def get_providers():
    """获取所有可用的模型提供商"""
    try:
        models = model_manager.get_available_models()
        providers = list(set(model.get('provider', 'unknown') for model in models))
        providers.sort()
        return providers
    except Exception as e:
        logging.error(f"获取提供商列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取提供商列表失败: {str(e)}")


@model_router.get("/provider/{provider_name}", response_model=List[ModelConfig])
async def get_models_by_provider(provider_name: str):
    """根据提供商获取模型"""
    try:
        models = model_manager.get_available_models()
        provider_models = [
            model for model in models
            if model.get('provider', '').lower() == provider_name.lower()
        ]
        return provider_models
    except Exception as e:
        logging.error(f"获取提供商模型失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取提供商模型失败: {str(e)}")