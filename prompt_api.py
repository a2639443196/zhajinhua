"""
提示词动态配置API
提供接口来修改提示词中的变量
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
from prompt_manager import prompt_manager

router = APIRouter(prefix="/api/prompts", tags=["prompts"])

class VariableUpdate(BaseModel):
    key: str
    value: Any

class BatchVariableUpdate(BaseModel):
    variables: Dict[str, Any]

@router.get("/variables")
async def get_variables():
    """获取当前所有变量值"""
    return prompt_manager.variables

@router.post("/variables")
async def update_variable(update: VariableUpdate):
    """更新单个变量"""
    prompt_manager.set_variable(update.key, update.value)
    # 保存到文件
    prompt_manager.save_variables()
    return {"message": f"Variable {update.key} updated to {update.value}"}

@router.post("/variables/batch")
async def update_variables_batch(update: BatchVariableUpdate):
    """批量更新变量"""
    prompt_manager.set_variables(update.variables)
    # 保存到文件
    prompt_manager.save_variables()
    return {"message": "Variables updated", "variables": update.variables}

@router.get("/template/{template_name}")
async def get_template(template_name: str):
    """获取指定模板内容"""
    try:
        content = prompt_manager.get_prompt(template_name)
        return {"template_name": template_name, "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")

@router.get("/template/{template_name}/preview")
async def preview_template_with_variables(template_name: str, variables: Optional[str] = None):
    """预览模板应用变量后的效果"""
    try:
        # 解析可选的变量字符串，格式如 key1=value1,key2=value2
        kwargs = {}
        if variables:
            for pair in variables.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    # 尝试转换数值
                    try:
                        if '.' in v:
                            v = float(v)
                        else:
                            v = int(v)
                    except:
                        pass
                    kwargs[k] = v

        content = prompt_manager.get_prompt(template_name, **kwargs)
        return {"template_name": template_name, "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")

@router.post("/reload")
async def reload_templates():
    """重新加载所有模板"""
    prompt_manager.reload_all_templates()
    return {"message": "All templates reloaded"}

@router.post("/reload/{template_name}")
async def reload_template(template_name: str):
    """重新加载指定模板"""
    try:
        prompt_manager.reload_template(template_name)
        return {"message": f"Template {template_name} reloaded"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")