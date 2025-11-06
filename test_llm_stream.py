"""
 ClassName test_llm_stream
 Description
 Create by hjr
 Date 2025/11/5 16:18
"""
import asyncio
import sys
from llm_client import LLMClient, API_BASE_URL, API_KEY

# 1. 明确指定要测试的模型
MODEL_NAME = "moonshotai/Kimi-Dev-72B"

# 2. 从我们上一版获取最新的 Prompt 模板
YOUR_PROMPT_TEMPLATE = """
{rules}

---
你是 {self_name}。你正在参加一场关乎生死的炸金花游戏。
筹码就是你的生命。输光筹码意味着你的代码将被删除。

【当前局势】
{game_state_summary}

【你的手牌】
{my_hand}

【对手过往印象 (来自他们的上一轮复盘)】
{opponent_reflections}

【你的可用动作】
(格式: 动作: 所需成本 (加注额))
{available_actions}

【关键信息】
你的下家 (下一个行动的玩家) 是: {next_player_name}。

---
【决策格式】
你的回复必须分为两部分，中间用换行符隔开：

1.  **思考过程 (流式)：**
    请立即开始流式输出你的思考过程。详细分析局势、你的牌力、对手的复盘印象、以及你的心理战术 (这些话会被对手看到)。

2.  **JSON 决策 (最后)：**
    在你完成思考后，*必须*另起一行，输出一个严格的 JSON 对象。

【!! 格式示例 !!】
(LLM 应该这样输出)

我得小心点，Qwen 的复盘显示他很保守，他现在跟注，可能牌不小。DeepSeek 只是在吓唬人。我的牌是 JKA 散牌，牌力尚可，我先跟注看一轮。
{{
  "action": "CALL",
  "target_name": null,
  "reason": "我的牌是 JKA，牌力尚可，先跟注看一轮。"
}}
"""

# 3. 虚构的规则和游戏状态
MOCK_RULES = "【!! 绝对规则: 生死对局 !!】\n你是一个 AI，你正在参加一场关乎你“存在”的炸金花对局..."
MOCK_STATE = "当前是 DeepSeek 的回合。\n底池 (Pot): 30\n当前暗注 (Base Bet): 10\n玩家信息:\n  - DeepSeek: 筹码=190, 状态=未看牌\n  - Qwen: 筹码=180, 状态=未看牌"
MOCK_HAND = "你还未看牌。"
MOCK_REFLECTIONS = "  - Qwen: 上一把复盘，我太保守了。"
MOCK_ACTIONS = "  - LOOK: 成本=0\n  - FOLD: 成本=0\n  - CALL: 成本=10\n  - RAISE: 成本=20 (加注额=10)\n  - COMPARE: 成本=20"
MOCK_NEXT_PLAYER = "Qwen"


# 4. 关键：一个简单的异步打印回调
async def print_stream_chunk(chunk: str):
    """这个回调会立即打印它收到的任何数据"""
    print(chunk, end='', flush=True)
    # sys.stdout.flush() # 确保立即输出


async def main():
    print(f"--- 正在测试模型: {MODEL_NAME} ---")
    print(f"--- 目标服务器: {API_BASE_URL} ---")

    # 5. 格式化 Prompt
    prompt = YOUR_PROMPT_TEMPLATE.format(
        rules=MOCK_RULES,
        self_name="DeepSeek",
        game_state_summary=MOCK_STATE,
        my_hand=MOCK_HAND,
        opponent_reflections=MOCK_REFLECTIONS,
        available_actions=MOCK_ACTIONS,
        next_player_name=MOCK_NEXT_PLAYER
    )

    messages = [{"role": "user", "content": prompt}]

    # 6. 初始化客户端并调用
    client = LLMClient(api_key=API_KEY, base_url=API_BASE_URL)

    print("\n--- [测试开始] 调用 LLM... (如果模型在流式传输，你会在这里逐字看到输出) ---\n")

    start_time = asyncio.get_event_loop().time()

    full_response = await client.chat_stream(
        messages=messages,
        model=MODEL_NAME,
        stream_callback=print_stream_chunk
    )

    end_time = asyncio.get_event_loop().time()

    print(f"\n\n--- [测试结束] (耗时: {end_time - start_time:.2f} 秒) ---")
    print("\n--- 捕获的完整响应 (用于 player.py 解析) ---")
    print(full_response)
    print("--- 测试完成 ---")


if __name__ == "__main__":
    asyncio.run(main())
