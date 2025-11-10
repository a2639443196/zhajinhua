import asyncio
import random  # (新) 1. 导入 random
from typing import Set, List
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from game_controller import GameController
import game_rules  # (新) 导入以动态覆盖 GameConfig.initial_chips
# --- 1. (新) 日志记录和下载所需的库 ---
import time
from pathlib import Path
import threading
from fastapi.responses import FileResponse, JSONResponse
import os
import json
from prompt_manager import prompt_manager  # 导入提示词管理器
from prompt_api import router as prompt_router  # 导入提示词API路由

# --- (结束) ---
# --- 1. 模型管理器 (新的动态配置系统) ---
# 从模型管理器获取当前选中的玩家配置
from model_manager import model_manager

def get_current_player_configs():
    """获取当前选中的玩家配置"""
    try:
        configs = model_manager.get_selected_model_configs()
        if not configs:
            # 如果没有选中的配置，使用默认配置
            print("【警告】: 没有选中的模型，使用默认配置")
            configs = [
                {"name": "DouBao", "model": "doubao-seed-1-6-lite-251015"},
                {"name": "kimiK2", "model": "moonshotai/Kimi-K2-Instruct-0905"},
                {"name": "deepseekv3", "model": "deepseek-ai/DeepSeek-V3"},
            ]
        print(f"【信息】: 当前选中 {len(configs)} 个模型参赛")
        return configs
    except Exception as e:
        print(f"【错误】: 获取玩家配置失败: {e}")
        # 回退到硬编码配置
        return [
            {"name": "DouBao", "model": "doubao-seed-1-6-lite-251015"},
            {"name": "kimiK2", "model": "moonshotai/Kimi-K2-Instruct-0905"},
            {"name": "deepseekv3", "model": "deepseek-ai/DeepSeek-V3"},
        ]

# 旧的硬编码配置保留作为备用
legacy_player_configs = [
    {"name": "DouBao", "model": "doubao-seed-1-6-lite-251015"},
    {"name": "kimiK2", "model": "moonshotai/Kimi-K2-Instruct-0905"},
    {"name": "Qwen", "model": "Qwen/Qwen3-Next-80B-A3B-Instruct"},
    {"name": "Ling", "model": "inclusionAI/Ling-1T"},
    {"name": "deepseekv3", "model": "deepseek-ai/DeepSeek-V3"},
    # {"name": "BaiDu", "model": "baidu/ERNIE-4.5-300B-A47B"},
    {"name": "DeepSeek", "model": "deepseek-ai/DeepSeek-V3.1-Terminus"},
]

# --- 2. 自动关闭控制 ---
# 是否开启无人观看自动关闭功能
ENABLE_AUTO_SHUTDOWN = False
# 无人观看时，自动关闭游戏等待时间 (秒)
AUTO_SHUTDOWN_TIMEOUT = 60 * 5
# --------------------------
# --- (新) 全局变量，用于存储最新日志文件的路径 ---
LATEST_LOG_FILE: str | None = None


# --- (新) 日志收集器类 ---
class GameLogCollector:
    """一个线程安全的类，用于收集完整的游戏日志，包括流式消息。"""

    def __init__(self):
        self._log_history: List[str] = []
        self._stream_buffer: str = ""
        self._lock = threading.Lock()  # 确保缓冲区操作的原子性

    def _flush_buffer(self):
        """（内部）将当前缓冲区内容作为一行完整日志存入历史记录。"""
        if self._stream_buffer:
            self._log_history.append(self._stream_buffer)
            self._stream_buffer = ""

    def add_log(self, message: str):
        """为非流式消息（如 god_print）添加一条新日志。"""
        with self._lock:
            self._flush_buffer()  # 确保上一条流已结束
            self._log_history.append(message)

    def start_stream(self, message: str):
        """开始一条新的流式消息（如 god_stream_start）。"""
        with self._lock:
            self._flush_buffer()  # 确保上一条流已结束
            self._stream_buffer = message

    def append_stream(self, chunk: str):
        """向当前流式消息追加内容（如 god_stream_chunk）。"""
        with self._lock:
            self._stream_buffer += chunk

    def get_full_log(self) -> str:
        """获取完整的日志文本，用于最终保存。"""
        with self._lock:
            self._flush_buffer()  # 确保最后一条流被存入
            return "\n".join(self._log_history)

    def clear(self):
        """清空日志。"""
        with self._lock:
            self._log_history = []
            self._stream_buffer = ""


# --- 2. WebSocket 连接管理器 (无修改) ---
class ConnectionManager:
    def __init__(self):
        self.active_spectators: Set[WebSocket] = set()
        self._shutdown_timer: asyncio.Task | None = None  # 新增：自动关闭计时器任务

    async def _manage_timer(self):
        """管理自动关闭计时器：启动或取消"""
        global game_loop_task

        if not ENABLE_AUTO_SHUTDOWN or len(self.active_spectators) > 0:
            # 有观众或功能关闭：取消计时器
            if self._shutdown_timer:
                self._shutdown_timer.cancel()
                self._shutdown_timer = None
            return

        # 无观众且游戏运行中，启动计时器
        if game_loop_task and not game_loop_task.done() and not self._shutdown_timer:
            print(f"【系统】: 无人观看，{AUTO_SHUTDOWN_TIMEOUT}秒后自动关闭游戏...")
            # 创建新的计时器任务
            self._shutdown_timer = asyncio.create_task(self._shutdown_after_delay())

    async def _shutdown_after_delay(self):
        """延迟后执行关闭操作"""
        await asyncio.sleep(AUTO_SHUTDOWN_TIMEOUT)

        global game_loop_task
        if game_loop_task and not game_loop_task.done():
            # 确认在延迟结束后依然没有观众
            if len(self.active_spectators) == 0:
                print("【系统】: 达到自动关闭时间，强制停止游戏。")

                # 必须安全地取消游戏任务
                game_loop_task.cancel()
                game_loop_task = None

                # --- [动态时间修复] ---
                # 将 AUTO_SHUTDOWN_TIMEOUT (秒) 转换为动态描述
                if AUTO_SHUTDOWN_TIMEOUT < 60:
                    time_desc = f"{AUTO_SHUTDOWN_TIMEOUT} 秒"
                else:
                    minutes = AUTO_SHUTDOWN_TIMEOUT // 60
                    seconds = AUTO_SHUTDOWN_TIMEOUT % 60
                    time_desc = f"{minutes} 分钟"
                    if seconds > 0:
                        time_desc += f" {seconds} 秒"

                await self.broadcast_log(f"【系统警告】: 无人观看超过 {time_desc}，游戏已自动关闭。")
                # --- [修复结束] ---
                await self.broadcast_status(running=False)

            self._shutdown_timer = None  # 任务已完成，清空引用

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_spectators.add(ws)
        await self._manage_timer()  # 连接时，取消计时器
        global game_loop_task
        if game_loop_task is not None and not game_loop_task.done():
            await ws.send_json({"type": "status", "running": True})

    def disconnect(self, ws: WebSocket):
        self.active_spectators.discard(ws)
        # 延迟调用计时器管理，确保连接断开操作完成
        asyncio.create_task(self._manage_timer())  # 断开时，启动计时器

    async def broadcast_log(self, message: str):
        await self._broadcast_json({"type": "log", "message": message})

    async def broadcast_stream_start(self, message: str):
        await self._broadcast_json({"type": "stream_start", "message": message})

    async def broadcast_stream_chunk(self, chunk: str):
        await self._broadcast_json({"type": "stream_chunk", "chunk": chunk})

    async def broadcast_status(self, running: bool):
        await self._broadcast_json({"type": "status", "running": running})

    async def broadcast_panel_data(self, data: dict):
        await self._broadcast_json({"type": "panel_update", "data": data})  # <-- 正确：有下划线

    async def broadcast_event_log(self, data: list):
        await self._broadcast_json({"type": "event_log_update", "data": data})

    async def _broadcast_json(self, json_message: dict):
        disconnected = set()

        for ws in self.active_spectators.copy():
            try:
                await ws.send_json(json_message)
            except Exception:
                disconnected.add(ws)

        for ws in disconnected:
            self.active_spectators.discard(ws)


# --- (新) 游戏结束时调用的辅助函数 ---
async def save_log_and_cleanup(log_collector: GameLogCollector, controller_hand_count: int, reason: str,
                               game_controller=None):
    """
    保存日志文件，广播状态，并重置全局游戏任务。
    如果提供了game_controller，将调用完整的信息重置功能。

    Args:
        log_collector: 游戏日志收集器
        controller_hand_count: 总手牌数
        reason: 游戏结束原因
        game_controller: 可选的游戏控制器，用于执行完整的信息重置
    """
    global game_loop_task, LATEST_LOG_FILE

    log_text = log_collector.get_full_log()
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_filename = log_dir / f"game_log_{timestamp}.txt"

    try:
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(f"--- 游戏结束 ({reason}) ---\n")
            f.write(f"--- 共 {controller_hand_count} 手牌 ---\n\n")
            f.write(log_text)

        log_announce_msg = f"--- 游戏日志已保存: {log_filename} ---"
        print(f"【上帝视角】: {log_announce_msg}")
        await manager.broadcast_log(log_announce_msg)

        # 更新全局变量以便下载
        LATEST_LOG_FILE = str(log_filename)

    except Exception as e:
        log_error_msg = f"!! 保存日志失败: {e} !!"
        print(f"【上帝视角】: {log_error_msg}")
        await manager.broadcast_log(log_error_msg)

    # 如果提供了游戏控制器，调用完整的信息重置功能
    if game_controller:
        try:
            await game_controller.complete_game_reset(log_collector)
            print(f"【上帝视角】: 游戏信息完全重置完成")
        except Exception as e:
            print(f"【上帝视角】(警告): 游戏信息重置时出错: {e}")

    await manager.broadcast_status(running=False)
    game_loop_task = None


manager = ConnectionManager()
app = FastAPI()
game_loop_task: asyncio.Task | None = None

# 注册提示词API路由
app.include_router(prompt_router)

# 注册模型管理API路由
from model_api import model_router
app.include_router(model_router)

# 初始化提示词变量
from init_prompt_vars import init_prompt_variables

init_prompt_variables()


# 配置页面路由
@app.get("/config")
async def config_page():
    """提供配置页面"""
    return FileResponse('static/config.html')


# 默认配置路由
@app.get("/api/config")
async def get_config():
    """获取当前游戏配置"""
    return {
        "chip_warning_threshold": prompt_manager.variables.get('CHIP_WARNING_THRESHOLD', 300),
        "chip_critical_threshold": prompt_manager.variables.get('CHIP_CRITICAL_THRESHOLD', 150),
        "base_cheat_success_rate": prompt_manager.variables.get('BASE_CHEAT_SUCCESS_RATE', 16),
        "min_raise_percentage": prompt_manager.variables.get('MIN_RAISE_PERCENTAGE', 5),
        "max_loan_turns": prompt_manager.variables.get('MAX_LOAN_TURNS', 6),
    }


# --- 3. 游戏循环 (已修改以支持日志记录) ---
async def run_llm_game_loop(config: dict | None = None):
    global game_loop_task

    # (新) 创建日志收集器实例
    log_collector = GameLogCollector()
    controller = None  # (新) 将 controller 提升到 try 之外

    async def god_print_and_broadcast(message: str, delay: float = 0.5):
        log_collector.add_log(message)  # <-- (新) 捕获日志
        print(f"【上帝视角】: {message}")
        await manager.broadcast_log(message)
        await asyncio.sleep(delay)

    async def god_stream_start(message: str, delay: float = 0.5):
        log_collector.start_stream(message)  # <-- (新) 捕获日志
        print(f"【上帝视角】: {message}", end='', flush=True)
        await manager.broadcast_stream_start(message)
        await asyncio.sleep(delay)

    async def god_stream_chunk(chunk: str, delay: float = 0.05):
        log_collector.append_stream(chunk)  # <-- (新) 捕获日志
        print(chunk, end='', flush=True)
        await manager.broadcast_stream_chunk(chunk)
        await asyncio.sleep(delay)

    async def god_panel_update(data: dict):
        await manager.broadcast_panel_data(data)

    async def god_event_log_update(data: list):
        await manager.broadcast_event_log(data)

    # (新) 读取并应用来自前端的配置覆盖
    try:
        if isinstance(config, dict):
            ic = config.get("initial_chips")
            dt = config.get("desperation_threshold")
            if isinstance(ic, int) and ic > 0:
                game_rules.GameConfig.initial_chips = ic
            # 记录阈值以传入控制器
            despair_threshold = dt if isinstance(dt, int) and dt > 0 else 1000
        else:
            despair_threshold = 1000
    except Exception:
        # 若解析失败，回退到默认值
        despair_threshold = 1000

    # --- (新) 2. 获取当前选中的模型配置并随机打乱玩家顺序 ---
    player_configs = get_current_player_configs()
    shuffled_configs = player_configs.copy()
    random.shuffle(shuffled_configs)
    new_order_str = ", ".join([p["name"] for p in shuffled_configs])
    await god_print_and_broadcast(f"--- 玩家顺序已随机打乱 ---", 0.1)
    await god_print_and_broadcast(f"本局顺序: {new_order_str}", 0.5)
    # --- (修改结束) ---

    controller = GameController(  # (新) 赋值给外部变量
        shuffled_configs,
        god_print_callback=god_print_and_broadcast,
        god_stream_start_callback=god_stream_start,
        god_stream_chunk_callback=god_stream_chunk,
        god_panel_update_callback=god_panel_update,
        god_event_log_update_callback=god_event_log_update,
        despair_threshold=despair_threshold
    )

    try:
        await controller.run_game()
        hand_count = controller.hand_count if controller else 0
        await god_print_and_broadcast(f"--- 锦标赛结束 (共 {hand_count} 手牌) ---", 2.0)
        # (新) 游戏正常结束，保存日志并重置所有信息
        await save_log_and_cleanup(log_collector, hand_count, "正常结束", controller)

    except asyncio.CancelledError:
        hand_count = controller.hand_count if controller else 0
        await god_print_and_broadcast(f"--- 锦标赛被上帝强制终止 ---", 1.0)
        # (新) 游戏被取消，保存日志并重置所有信息
        await save_log_and_cleanup(log_collector, hand_count, "手动停止", controller)

    except Exception as e:
        hand_count = controller.hand_count if controller else 0
        await god_print_and_broadcast(f"!! 游戏控制器发生严重错误: {e} !!", 1)
        import traceback
        traceback.print_exc()
        # (新) 游戏崩溃，保存日志并重置所有信息
        await save_log_and_cleanup(log_collector, hand_count, f"崩溃 (Error: {e})", controller)


# --- 4. FastAPI 路由 (无修改) ---
@app.get("/")
async def get_index():
    return FileResponse("index.html")


@app.get("/mobile")
async def get_mobile():
    return FileResponse("mobile.html")


# --- (新) 日志下载 API 端口 ---
@app.get("/download_latest_log")
async def download_latest_log():
    """
    提供最近一次游戏日志的下载。
    """
    global LATEST_LOG_FILE
    if LATEST_LOG_FILE and os.path.exists(LATEST_LOG_FILE):
        return FileResponse(
            path=LATEST_LOG_FILE,
            # (新) 确保浏览器以下载方式处理
            filename=os.path.basename(LATEST_LOG_FILE),
            media_type='text/plain'
        )
    return JSONResponse(
        status_code=404,
        content={"error": "No log file available or found."}
    )


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
                    # (新) 将前端传入的配置转发到游戏循环
                    game_loop_task = asyncio.create_task(run_llm_game_loop(config=data.get("config")))
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
    # (新) 定义人设文件路径和条目数限制 'n'
    BASE_DIR = Path(__file__).parent.resolve()
    PERSONA_FILE_PATH = BASE_DIR / "used_personas.json"
    ITEM_LIMIT = 50  # <--- 这是 n (条目数)

    try:
        if PERSONA_FILE_PATH.exists():
            # (新) 读取文件内容
            with PERSONA_FILE_PATH.open("r", encoding="utf-8") as f:
                content = f.read().strip()

            item_count = 0
            if content:
                # (新) 解析 JSON 并获取条目数 (长度)
                data = json.loads(content)
                if isinstance(data, list):
                    item_count = len(data)
                else:
                    print(f"【系统】: 警告：人设文件 '{PERSONA_FILE_PATH.name}' 格式非列表，将按 0 条处理。")

            # (新) 检查条目数是否 > n
            if item_count > ITEM_LIMIT:
                print(
                    f"【系统】: 人设文件 '{PERSONA_FILE_PATH.name}' (共 {item_count} 条) 超过 {ITEM_LIMIT} 条限制，正在清空...")

                # 重置为空列表 "[]"，以确保 game_controller 能正确解析
                with PERSONA_FILE_PATH.open("w", encoding="utf-8") as f:
                    f.write("[]")

                print(f"【系统】: 人设文件已清空。")

        else:
            # 文件不存在，GameController 会自动处理，无需操作
            pass

    except json.JSONDecodeError:
        # (新) 处理文件损坏或内容非法的情况
        print(f"【系统】: !! 人设文件 '{PERSONA_FILE_PATH.name}' 格式错误 (非JSON)，正在清空...")
        try:
            with PERSONA_FILE_PATH.open("w", encoding="utf-8") as f:
                f.write("[]")
            print(f"【系统】: 人设文件已清空。")
        except Exception as write_e:
            print(f"【系统】: !! 清空损坏的人设文件时失败: {write_e} !!")

    except Exception as e:
        print(f"【系统】: !! 启动时检测人设文件出错: {e} !!")

    # (原有的启动代码)
    print("服务器已启动。请在浏览器中打开 http://127.0.0.1:9900 观战")
    print("打开页面后，请点击“开始游戏”按钮。")
    uvicorn.run(app, host="0.0.0.0", port=9900)
