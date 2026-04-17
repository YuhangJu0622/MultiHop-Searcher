"""统一超时上下文：看门狗模式。

每个问题创建一个 TimeoutContext 实例，贯穿 Planner / Searcher / Reader
整条调用链。看门狗线程到达 deadline 后直接关闭活跃的网络连接，
让阻塞操作自动抛异常冒泡到顶层 fallback。

用法::

    ctx = TimeoutContext(total_seconds=1200)
    try:
        ...  # 传入 ctx 到各组件
    except GlobalTimeoutException:
        ...  # fallback
    finally:
        ctx.cancel()
"""

import logging
import threading
import time

from ..schema import GlobalTimeoutException

logger = logging.getLogger(__name__)


class TimeoutContext:
    """统一超时上下文，所有组件共享同一个实例。

    核心机制：
    1. ``threading.Timer`` 看门狗：到达 deadline 后设置 ``_timed_out`` 标记，
       并关闭当前活跃的网络连接（streaming response / requests session 等）。
    2. ``register_connection()``：注册当前活跃连接。如果看门狗已触发，
       则立即关闭新连接并抛出 ``GlobalTimeoutException``，堵住"两次调用之间"的间隙。
    3. ``remaining`` 属性：返回剩余可用秒数，供下游动态设置 per-call timeout。
    """

    def __init__(self, total_seconds: float):
        self.deadline = time.time() + total_seconds
        self.total_seconds = total_seconds
        self._lock = threading.Lock()
        self._active_connection = None
        self._timed_out = False

        self._watchdog = threading.Timer(total_seconds, self._on_timeout)
        self._watchdog.daemon = True
        self._watchdog.start()

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def remaining(self) -> float:
        """剩余可用时间（秒），用于动态设置下游 timeout。"""
        return max(0.0, self.deadline - time.time())

    @property
    def expired(self) -> bool:
        return self._timed_out or time.time() > self.deadline

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def register_connection(self, conn):
        """注册当前活跃的 streaming response / HTTP 连接。

        如果看门狗已触发（``_timed_out is True``），立即关闭该连接
        并抛出 ``GlobalTimeoutException``，避免超时后仍发起新请求。
        """
        with self._lock:
            if self._timed_out:
                self._safe_close(conn)
                raise GlobalTimeoutException(
                    f"全局超时: 看门狗已触发 (deadline={self.deadline:.0f})"
                )
            self._active_connection = conn

    def unregister_connection(self):
        with self._lock:
            self._active_connection = None

    # ------------------------------------------------------------------
    # 看门狗回调
    # ------------------------------------------------------------------

    def _on_timeout(self):
        """Timer 回调：标记超时 + 关闭当前活跃连接。"""
        self._timed_out = True
        logger.warning(
            "[TimeoutContext] 全局超时触发 (%.0fs), 正在关闭活跃连接...",
            self.total_seconds,
        )
        with self._lock:
            if self._active_connection is not None:
                self._safe_close(self._active_connection)
                self._active_connection = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def cancel(self):
        """正常完成时取消看门狗，释放 Timer 资源。"""
        self._watchdog.cancel()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_close(conn):
        """安全关闭连接，兼容不同类型的 response 对象。"""
        try:
            if hasattr(conn, "close"):
                conn.close()
            elif hasattr(conn, "response") and hasattr(conn.response, "close"):
                conn.response.close()
        except Exception:
            pass
