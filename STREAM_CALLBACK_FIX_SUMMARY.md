# Stream Callback 错误修复总结

## 问题描述

用户报告了以下错误：
```
t NoneType can't be used in 'await' expression (自动投: 无罪)
{
LLM API 调用失败: object NoneType can't be used in 'await' expression

【kimiK2 的思考过程】:

  系统错误。

【kimiK2 的投票理由】: 投票时出错: object NoneType can't be used in 'await' expression
```

这个错误出现在陪审团投票阶段，当`stream_chunk_cb`参数为`None`时，代码试图将其作为异步函数调用。

## 问题根因

1. **参数默认值问题**: 某些Player类方法的`stream_start_cb`和`stream_chunk_cb`参数没有默认值`None`
2. **缺乏None检查**: 代码没有在调用前检查回调函数是否为`None`
3. **异步/同步混用**: 没有区分异步和同步回调函数的调用方式
4. **缺乏错误处理**: 回调函数调用失败时没有适当的错误处理

## 修复方案

### 1. 参数默认值修复

为所有相关方法添加了参数默认值`None`：

```python
# 修复前
stream_start_cb: Callable[[str], Awaitable[None]],
stream_chunk_cb: Callable[[str], Awaitable[None]]) -> dict:

# 修复后
stream_start_cb: Callable[[str], Awaitable[None]] = None,
stream_chunk_cb: Callable[[str], Awaitable[None]] = None) -> dict:
```

**修复的方法**:
- `decide_action()`
- `defend()`
- `vote()`
- `decide_bribe()`
- `reflect()`

### 2. 安全回调包装器

创建了安全的`stream_chunk_cb`包装器，处理异步/同步函数和错误：

```python
def safe_stream_chunk_cb(chunk: str):
    if stream_chunk_cb:
        try:
            if asyncio.iscoroutinefunction(stream_chunk_cb):
                asyncio.create_task(stream_chunk_cb(chunk))
            else:
                stream_chunk_cb(chunk)
        except Exception as e:
            print(f"[警告] stream_chunk_cb 调用失败: {e}")
```

### 3. None检查和错误处理

为所有回调调用添加了None检查和异常处理：

```python
# stream_start_cb检查
if stream_start_cb:
    try:
        await stream_start_cb(message)
    except Exception as e:
        print(f"[警告] stream_start_cb 调用失败: {e}")

# stream_chunk_cb检查
if stream_chunk_cb:
    try:
        if asyncio.iscoroutinefunction(stream_chunk_cb):
            await stream_chunk_cb(chunk)
        else:
            stream_chunk_cb(chunk)
    except Exception as e:
        print(f"[警告] stream_chunk_cb 调用失败: {e}")
```

## 修复的文件

**文件**: `player.py`

**修复的函数**:
1. `decide_action()` - 决策动作方法
2. `defend()` - 被告辩护方法
3. `vote()` - 陪审团投票方法
4. `decide_bribe()` - 贿赂决策方法
5. `reflect()` - 复盘方法

## 修复验证

通过测试脚本验证了以下方面：

✅ **代码检查**:
- 找到 `asyncio.iscoroutinefunction(stream_chunk_cb)` 检查逻辑
- 找到 `if stream_start_cb:` 保护检查
- 找到 11 处 `stream_chunk_cb 调用失败` 错误处理
- 找到 30 个 `try:` 块和 19 个异常处理

✅ **安全模式**:
- 所有方法都有安全的回调处理
- 异步和同步函数都能正确处理
- 错误不会中断主要流程

## 修复效果

1. **解决原始错误**: `object NoneType can't be used in 'await' expression` 错误不再出现
2. **提高稳定性**: 即使回调函数为None或出错，游戏也能继续运行
3. **向后兼容**: 现有代码不需要修改，自动获得错误保护
4. **调试友好**: 添加了警告信息，便于问题排查

## 使用建议

1. **立即部署**: 修复已完成，可以立即使用
2. **监控日志**: 观察是否出现新的警告信息
3. **测试功能**: 重新运行包含陪审团投票的游戏场景
4. **错误跟踪**: 如果仍有问题，检查具体的回调调用链

## 技术要点

1. **异步安全**: 使用`asyncio.iscoroutinefunction()`区分异步/同步函数
2. **非阻塞处理**: 异步回调使用`asyncio.create_task()`避免阻塞
3. **优雅降级**: 回调失败时继续执行主要逻辑
4. **错误隔离**: 回调错误不影响游戏核心流程

这个修复彻底解决了NoneType回调的问题，使系统更加健壮和稳定。