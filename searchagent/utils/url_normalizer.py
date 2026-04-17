"""URL 标准化器：将不同形式的 URL 统一为规范形式，用于去重。

设计为可扩展的注册式规则模式：
- 内置 Wikipedia 多语言路径标准化规则
- 未来可通过 register() 添加更多网站的标准化规则
"""
import re
import logging
from urllib.parse import urlparse, urlunparse, unquote

logger = logging.getLogger(__name__)


class URLNormalizer:
    """URL 标准化器，支持注册式规则。"""

    def __init__(self):
        # 规则列表：[(domain_pattern, normalize_func), ...]
        self._rules = []
        # 内置 Wikipedia 规则
        self.register(r'(^|\.)wikipedia\.org$', self._normalize_wikipedia)

    def register(self, domain_pattern: str, normalize_func):
        """注册一条标准化规则。

        Args:
            domain_pattern: 正则表达式，匹配 URL 的域名部分。
            normalize_func: 标准化函数，签名 (parsed_url) -> normalized_url_str
        """
        self._rules.append((re.compile(domain_pattern), normalize_func))

    def normalize(self, url: str) -> str:
        """将 URL 标准化为规范形式。

        Args:
            url: 原始 URL

        Returns:
            标准化后的 URL 字符串（用于去重比较）
        """
        if not url:
            return url

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ''

            for pattern, func in self._rules:
                if pattern.search(hostname):
                    normalized = func(parsed)
                    if normalized != url:
                        logger.debug("[URLNormalizer] %s -> %s", url, normalized)
                    return normalized

            # 无匹配规则时，做基本的标准化：去掉末尾斜杠、统一 scheme
            path = parsed.path.rstrip('/')
            normalized = urlunparse((
                parsed.scheme or 'https',
                parsed.netloc,
                path,
                parsed.params,
                parsed.query,
                ''  # 去掉 fragment
            ))
            return normalized

        except Exception:
            # URL 解析失败时返回原始值
            return url

    @staticmethod
    def _normalize_wikipedia(parsed) -> str:
        """Wikipedia 多语言路径标准化。

        将以下路径统一为 /wiki/ 前缀：
        - /zh-hant/文革改名风 → /wiki/文革改名风
        - /zh-hans/七·二〇事件 → /wiki/七·二〇事件
        - /zh-cn/... → /wiki/...
        - /zh-tw/... → /wiki/...
        - /zh/... → /wiki/...
        """
        path = parsed.path

        # 匹配 /zh-hant/, /zh-hans/, /zh-cn/, /zh-tw/, /zh/ 等变体
        wiki_lang_pattern = re.compile(r'^/(zh-hant|zh-hans|zh-cn|zh-tw|zh)/')
        path = wiki_lang_pattern.sub('/wiki/', path)

        # 去掉末尾斜杠
        path = path.rstrip('/')

        normalized = urlunparse((
            parsed.scheme or 'https',
            parsed.netloc,
            path,
            parsed.params,
            parsed.query,
            ''  # 去掉 fragment
        ))
        return normalized


# 全局单例
url_normalizer = URLNormalizer()
