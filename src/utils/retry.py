"""指數退避重試，供 get_judgment（5s→15s→45s）及 Claude API（最多 2 次）使用。"""
import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# 明確不重試的 exception：programming bug 或不可恢復的狀態錯誤。
# 重試這些只會放大 log 噪音、延遲真正的錯誤訊號。
# 其他 (OSError / httpx / anyio / asyncio.TimeoutError / ValueError 等) 都視為
# 可能 transient，走重試路徑。asyncio.CancelledError 在 Python 3.8+ 繼承
# BaseException 不被 except Exception 捕捉，不需特別處理。
_NON_RETRIABLE = (TypeError, AttributeError, KeyError)


async def with_retry(
    fn: Callable[..., Any],
    *args: Any,
    delays: tuple[float, ...] = (5.0, 15.0, 45.0),
    label: str = "",
    **kwargs: Any,
) -> Any:
    """執行 fn，失敗後依 delays 等待後重試。

    delays 長度決定最多重試次數，預設最多 4 次（1 次 + 3 次退避）。
    遇到 _NON_RETRIABLE 立即 propagate（不浪費重試）。
    其他 exception 全部失敗後拋出最後一次。
    """
    last_exc: Exception | None = None
    name = label or getattr(fn, "__name__", "fn")
    total_attempts = len(delays) + 1

    for attempt in range(total_attempts):
        try:
            return await fn(*args, **kwargs)
        except _NON_RETRIABLE as exc:
            logger.error("%s 非 transient 錯誤（%s），不重試：%s", name, type(exc).__name__, exc)
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < len(delays):
                wait = delays[attempt]
                logger.warning(
                    "%s 第 %d/%d 次失敗（%s），%.0f 秒後重試",
                    name, attempt + 1, total_attempts, exc, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error("%s 全部 %d 次均失敗：%s", name, total_attempts, exc)

    raise last_exc  # type: ignore[misc]
