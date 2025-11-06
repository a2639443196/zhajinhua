import uvicorn
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from typing import Set, List, Dict, Callable

from game_controller import GameController

# --- 1. LLM 玩家配置 (无修改) ---
player_configs = [
    {"name": "Qwen3", "model": "Qwen/Qwen3-VL-32B-Instruct"},
    {"name": "Doubao", "model": "doubao-seed-1-6-lite-251015"},
    {"name": "Baidu", "model": "baidu/ERNIE-4.5-300B-A47B"},
    {"name": "Kimi2", "model": "moonshotai/Kimi-K2-Instruct-0905"},
    {"name": "DeepSeekV3.1", "model": "deepseek-ai/DeepSeek-V3.1-Terminus"},
    {"name": "Ling", "model": "inclusionAI/Ling-1T"},
]


# --- 2. WebSocket 连接管理器 (已修改) ---
class ConnectionManager:
    def __init__(self):
        self.active_spectators: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_spectators.add(ws)
        global game_loop_task
        if game_loop_task is not None and not game_loop_task.done():
            await ws.send_json({"type": "status", "running": True})

    def disconnect(self, ws: WebSocket):
        self.active_spectators.discard(ws)

    async def broadcast_log(self, message: str):
        await self._broadcast_json({"type": "log", "message": message})

    async def broadcast_stream_start(self, message: str):
        await self._broadcast_json({"type": "stream_start", "message": message})

    async def broadcast_stream_chunk(self, chunk: str):
        await self._broadcast_json({"type": "stream_chunk", "chunk": chunk})

    async def broadcast_status(self, running: bool):
        await self._broadcast_json({"type": "status", "running": running})

    async def broadcast_panel_data(self, data: dict):
        await self._broadcast_json({"type": "panel_update", "data": data})

    async def _broadcast_json(self, json_message: dict):
        disconnected = set()

        # --- (关键 BUG 修复) ---
        # 遍历集合的副本 (.copy())，以允许在迭代期间安全地断开连接 (disconnect)
        for ws in self.active_spectators.copy():
            # --- (修复结束) ---
            try:
                await ws.send_json(json_message)
            except Exception:
                disconnected.add(ws)

        # (这个循环是安全的，因为它遍历的是一个新集合)
        for ws in disconnected:
            self.active_spectators.discard(ws)


manager = ConnectionManager()
app = FastAPI()
game_loop_task: asyncio.Task | None = None


# --- 3. 游戏循环 (无修改) ---
async def run_llm_game_loop():
    global game_loop_task

    async def god_print_and_broadcast(message: str, delay: float = 0.5):
        print(f"【上帝视角】: {message}")
        await manager.broadcast_log(message)
        await asyncio.sleep(delay)

    async def god_stream_start(message: str, delay: float = 0.5):
        print(f"【上帝视角】: {message}", end='', flush=True)
        await manager.broadcast_stream_start(message)
        await asyncio.sleep(delay)

    async def god_stream_chunk(chunk: str, delay: float = 0.05):
        print(chunk, end='', flush=True)
        await manager.broadcast_stream_chunk(chunk)
        await asyncio.sleep(delay)

    async def god_panel_update(data: dict):
        await manager.broadcast_panel_data(data)

    controller = GameController(
        player_configs,
        god_print_callback=god_print_and_broadcast,
        god_stream_start_callback=god_stream_start,
        god_stream_chunk_callback=god_stream_chunk,
        god_panel_update_callback=god_panel_update
    )

    try:
        await controller.run_game()
        await god_print_and_broadcast(f"--- 锦标赛结束 (共 {controller.hand_count} 手牌) ---", 2.0)
        await manager.broadcast_status(running=False)
        game_loop_task = None
    except asyncio.CancelledError:
        await god_print_and_broadcast(f"--- 锦标赛被上帝强制终止 ---", 1.0)
    except Exception as e:
        await god_print_and_broadcast(f"!! 游戏控制器发生严重错误: {e} !!", 1)
        await manager.broadcast_status(running=False)
        game_loop_task = None
        await god_print_and_broadcast("--- 游戏已停止，请手动点击“开始游戏”重启 ---", 0)
        import traceback
        traceback.print_exc()


# --- 4. FastAPI 路由 (无修改) ---
@app.get("/")
async def get_index():
    return FileResponse("index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    global game_loop_task
    try:
        while True:
            data = await ws.receive_json()

            if data.get("type") == "START_GAME":
                if game_loop_task is None or game_loop_task.done():
                    await manager.broadcast_log("上帝点击了【开始游戏】...")
                    await manager.broadcast_status(running=True)
                    game_loop_task = asyncio.create_task(run_llm_game_loop())
                else:
                    await ws.send_json({"type": "log", "message": "游戏已在运行中。"})

            elif data.get("type") == "STOP_GAME":
                if game_loop_task and not game_loop_task.done():
                    game_loop_task.cancel()
                    game_loop_task = None
                    await manager.broadcast_log("上帝点击了【停止游戏】...")
                    await manager.broadcast_status(running=False)
                else:
                    await ws.send_json({"type": "log", "message": "游戏未在运行。"})

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        print(f"WebSocket 错误: {e}")
        manager.disconnect(ws)


if __name__ == "__main__":
    print("服务器已启动。请在浏览器中打开 http://127.0.0.1:9900 观战")
    print("打开页面后，请点击“开始游戏”按钮。")
    uvicorn.run(app, host="0.0.0.0", port=9900)
