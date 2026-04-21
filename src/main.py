"""FastAPI 進入點：lifespan 管理 DB / MCP / worker，掛載 API router 及靜態前端。"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.api import analyses, cases, expansion, judgments, stream, tasks, workers
from src.db.database import init_db
from src import mcp_client
from src.worker.runner import start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 額外 API：自然語言策略拆解（strategy endpoint）
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Header
from pydantic import BaseModel

strategy_router = APIRouter(tags=["strategy"])


class StrategyRequest(BaseModel):
    query: str


@strategy_router.post("/strategy")
async def decompose_strategy(
    body: StrategyRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    from src.pipeline.strategy import decompose_query
    strategies = await decompose_query(body.query, api_key=x_api_key)
    return {"strategies": strategies}


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化資料庫
    await init_db()
    logger.info("DB 初始化完成")

    # 啟動 MCP 客戶端（在獨立 Task 中執行，避免 anyio cancel scope 洩漏）
    mcp_task = asyncio.create_task(mcp_client.init_mcp())
    try:
        await asyncio.wait_for(asyncio.shield(mcp_task), timeout=30.0)
    except BaseException as exc:
        logger.warning("MCP 啟動失敗或超時：%s", exc)
        mcp_task.cancel()
        try:
            await mcp_task
        except BaseException:
            pass

    # 啟動時：恢復 pending 任務 + stage25_inflight，走 asyncio.create_task 非同步派出。
    # 不再有長壽 worker loop 需要 cancel — 正常 API flow 與 recovery 都是 create_task
    # 的 fire-and-forget，透過 _stage_sem(5) 控制全域併發上限。
    await start_worker()
    logger.info("Recovery 完成，進入服務狀態")

    yield

    await mcp_client.close_mcp()
    logger.info("Server 關閉完成")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="判決智能檢索",
    description="給律師用的司法院判決 AI 篩選工具",
    version="1.0.3",
    lifespan=lifespan,
)

app.include_router(tasks.router, prefix="/api")
app.include_router(analyses.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(judgments.router, prefix="/api")
app.include_router(expansion.router, prefix="/api")
app.include_router(cases.router, prefix="/api")
app.include_router(workers.router, prefix="/api")
app.include_router(strategy_router, prefix="/api")

app.mount("/", StaticFiles(directory="src/ui/static", html=True), name="static")
