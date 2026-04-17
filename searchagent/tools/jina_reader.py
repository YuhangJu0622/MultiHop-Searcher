"""Jina AI Reader API 工具类：将 URL 转换为 LLM 友好的纯文本内容。

替代原有的 VisitPage 工具，直接返回干净文本，无需再调用 LLM 进行 HTML 提取。

API 文档: https://jina.ai/reader/
"""
import logging
import requests
import time
from typing import Dict, List, Optional, Tuple
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..schema import GlobalTimeoutException

logger = logging.getLogger(__name__)

_RATE_LIMIT_KEYWORDS = ("SSLZeroReturnError", "Connection reset by peer")
_RATE_LIMIT_RETRY_WAIT = 30
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_MIN_REMAINING = 60


class _JinaRateLimitError(Exception):
    """Jina 速率限制导致的连接失败（内部使用）。"""


class JinaReader:
    """Jina AI Reader API 工具（免费模式）。

    通过 r.jina.ai 将任意 URL 转换为纯文本，
    供 LLM 摘要使用。支持并发抓取多个 URL。

    Args:
        timeout: 单个请求超时（秒）
        max_workers: 并发线程数
    """

    def __init__(
        self,
        timeout: int = 15,
        max_workers: int = 20,
    ):
        self.timeout = timeout
        self.max_workers = max_workers
        self._ctx = None

    def read_url(self, url: str) -> Optional[str]:
        """读取单个 URL 并返回纯文本内容。

        Args:
            url: 要读取的 URL

        Returns:
            纯文本内容字符串，失败时返回 None

        Raises:
            _JinaRateLimitError: Jina 速率限制导致的 SSL 连接被拒绝
            GlobalTimeoutException: 全局超时
        """
        if self._ctx and self._ctx.expired:
            raise GlobalTimeoutException("全局超时: JinaReader")

        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "Accept": "text/plain",
            "X-Return-Format": "text",
        }

        try:
            _start = time.time()
            _timeout = min(self._ctx.remaining, self.timeout) if self._ctx else self.timeout
            response = requests.get(jina_url, headers=headers, timeout=_timeout)
            _elapsed = time.time() - _start

            if response.status_code == 200:
                content = response.text.strip()
                if content:
                    logger.info("[JinaReader] 成功抓取 %s (%.1fs, %d 字符)", url, _elapsed, len(content))
                    return content
                else:
                    logger.warning("[JinaReader] 空内容: %s", url)
                    return None
            else:
                logger.warning("[JinaReader] HTTP %d for %s", response.status_code, url)
                return None

        except Exception as e:
            if any(kw in str(e) for kw in _RATE_LIMIT_KEYWORDS):
                logger.warning("[JinaReader][RATE_LIMIT] 速率限制, URL: %s", url)
                raise _JinaRateLimitError(url) from e
            logger.warning("[JinaReader] 请求失败 %s: %s", url, e)
            return None

    def _fetch_batch(self, urls: List[str]) -> Tuple[Dict[str, Optional[str]], List[str]]:
        """并发抓取一批 URL，返回结果和因速率限制失败的 URL 列表。"""
        results = {}
        rate_limited = []

        _batch_timeout = min(self._ctx.remaining, 40) if self._ctx else 40

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_url = {
                executor.submit(self.read_url, url): url
                for url in urls
            }
            try:
                for future in as_completed(future_to_url, timeout=_batch_timeout):
                    url = future_to_url[future]
                    try:
                        content = future.result()
                        results[url] = content
                    except GlobalTimeoutException:
                        raise
                    except _JinaRateLimitError:
                        rate_limited.append(url)
                    except Exception as e:
                        logger.warning("[JinaReader] 异常 %s: %s", url, e)
                        results[url] = None
            except concurrent.futures.TimeoutError:
                pending = [u for f, u in future_to_url.items() if not f.done()]
                logger.warning("[JinaReader] 批次超时, 跳过 %d 个未完成 URL", len(pending))
                for url in pending:
                    results[url] = None
                if self._ctx and self._ctx.expired:
                    raise GlobalTimeoutException("全局超时: JinaReader 批次超时")

        return results, rate_limited

    def read_urls(self, urls: List[str], ctx=None) -> Dict[str, Optional[str]]:
        """并发读取多个 URL，对 Jina 速率限制自动重试。

        Args:
            urls: URL 列表
            ctx: TimeoutContext 实例

        Returns:
            {url: content_or_None} 字典
        """
        self._ctx = ctx
        results = {}
        logger.info("[JinaReader] --- 开始并发抓取 %d 个 URL ---", len(urls))
        _start = time.time()

        batch_results, rate_limited = self._fetch_batch(urls)
        results.update(batch_results)

        for attempt in range(1, _RATE_LIMIT_MAX_RETRIES + 1):
            if not rate_limited:
                break
            if self._ctx and self._ctx.remaining < _RATE_LIMIT_MIN_REMAINING:
                logger.warning(
                    "[JinaReader][RATE_LIMIT] 全局剩余时间不足 (%.0fs < %ds), 放弃重试 %d 个 URL",
                    self._ctx.remaining, _RATE_LIMIT_MIN_REMAINING, len(rate_limited),
                )
                for url in rate_limited:
                    results[url] = None
                break

            logger.warning(
                "[JinaReader][RATE_LIMIT] === 第 %d/%d 次重试: 等待 %ds 后重试 %d 个 URL ===",
                attempt, _RATE_LIMIT_MAX_RETRIES, _RATE_LIMIT_RETRY_WAIT, len(rate_limited),
            )
            time.sleep(_RATE_LIMIT_RETRY_WAIT)

            if self._ctx and self._ctx.expired:
                raise GlobalTimeoutException("全局超时: JinaReader 速率限制重试期间")

            retry_results, still_limited = self._fetch_batch(rate_limited)
            results.update(retry_results)
            rate_limited = still_limited
        else:
            if rate_limited:
                logger.warning(
                    "[JinaReader][RATE_LIMIT] 重试 %d 次后仍有 %d 个 URL 受速率限制, 放弃: %s",
                    _RATE_LIMIT_MAX_RETRIES, len(rate_limited),
                    [u.split('/')[-1][:40] for u in rate_limited],
                )
                for url in rate_limited:
                    results[url] = None

        _elapsed = time.time() - _start
        success_count = sum(1 for v in results.values() if v is not None)
        logger.info("[JinaReader] --- 抓取完成 --- 成功: %d/%d, 耗时: %.2fs",
                     success_count, len(urls), _elapsed)
        return results
