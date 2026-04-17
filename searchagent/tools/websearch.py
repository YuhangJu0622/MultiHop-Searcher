import time
import logging
import json
import os
from dotenv import load_dotenv
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Union, Optional
from http.client import HTTPSConnection
from cachetools import TTLCache, cached
from ..tools.basetool import BaseTool
from ..schema import GlobalTimeoutException, NonRetryableError
from ..utils.url_normalizer import url_normalizer
import concurrent.futures

logger = logging.getLogger(__name__)

class GoogleSearch(BaseTool):
    """
    Wrapper around the Serper.dev Google Search API.

    To use, you should pass your serper API key to the constructor.

    Args:
        api_key (List[str]): API KEY to use serper google search API.
            You can create a free API key at https://serper.dev.
        search_type (str): Serper API supports ['search', 'images', 'news',
            'places'] types of search, currently we only support 'search' and 'news'.
        topk (int): The number of search results returned in response from api search results.
        **kwargs: Any other parameters related to the Serper API. Find more details at
            https://serper.dev/playground
    """

    name: str = "GoogleSearch"
    description: str = "Performs a google web search for your query and your search intent then returns a string of the top search results."
    parameters: Optional[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "array",
                "items":{
                    "type": "string"
                },
                "description": "The search queries to perform."
            },
            "intent":{
                "type": "array",
                "items":{
                    "type": "string"
                },
                "description": "The detailed intent of the performing this search."
            },

        },
        "required": ["query","intent"],
        "additionalProperties": False
    }
    model_config = {"arbitrary_types_allowed": True}

    api_key: List[str]
    topk: int
    black_list: List[str]
    key_ctr : int
    invalid_keys : set
    _ctx: Optional[object] = None  # TimeoutContext 实例
    _last_grouped_results: Optional[dict] = None  # 上一次按 query 分组的搜索结果
    _lock: Lock = None

    def __init__(
        self,
        api_key: List[str] = None,
        topk: int = 5,
        black_list: List[str] = [
            'enoN',
            'youtube.com',
            'researchgate.net',
            'facebook.com',
            'instagram.com',
            'linkedin.com',
            'vice.com',
            'businessinsider.com',
            'scribd.com',
        ],
        key_ctr: int = 0,
        invalid_keys: set = set()
    ):

        super().__init__(api_key=api_key, topk=topk, black_list=black_list, key_ctr=key_ctr, invalid_keys=invalid_keys)
        self._lock = Lock()

    def __hash__(self):
        return hash(self.description)

    def __eq__(self, other):
        if isinstance(other, GoogleSearch):
            return self.description == other.description
        return False


    def execute(self, *, intent:str, query: Union[str, List[str]]) -> dict:
        """Google search API
        Args:
            query (List[str]): list of search query strings
        """
        MAX_QUERIES = 3
        queries = query if isinstance(query, list) else [query]
        if len(queries) > MAX_QUERIES:
            logger.warning("[GoogleSearch] Too many queries (%d), truncating to %d", len(queries), MAX_QUERIES)
            queries = queries[:MAX_QUERIES]

        # DEBUG: 记录搜索请求
        logger.debug("[GoogleSearch execute] intent=%s, queries=%s", intent, queries)

        _search_start = time.time()
        # 按 query 分组收集结果
        grouped_results = {}  # {query_str: [result_dict, ...]}
        seen_normalized_urls = set()

        _batch_timeout = min(self._ctx.remaining, 40) if self._ctx else 40

        with ThreadPoolExecutor() as executor:
            future_to_query = {executor.submit(self._search, q): q for q in queries}

            try:
                for future in concurrent.futures.as_completed(future_to_query, timeout=_batch_timeout):
                    q = future_to_query[future]
                    try:
                        results = future.result(timeout=20)
                    except GlobalTimeoutException:
                        raise
                    except Exception as exc:
                        logger.warning("[GoogleSearch] query '%s' 异常: %s", q, exc)
                    else:
                        query_results = []
                        for result in results.values():
                            raw_url = result.get('url', '')
                            norm_url = url_normalizer.normalize(raw_url) if raw_url else ''
                            if norm_url and norm_url in seen_normalized_urls:
                                logger.debug("[GoogleSearch] 同批次去重: %s", raw_url)
                                continue
                            if norm_url:
                                seen_normalized_urls.add(norm_url)
                            query_results.append(result)
                        grouped_results[q] = query_results
            except concurrent.futures.TimeoutError:
                pending = [q for f, q in future_to_query.items() if not f.done()]
                logger.warning("[GoogleSearch] 批次超时, 跳过 %d 个未完成 query: %s", len(pending), pending)
                if self._ctx and self._ctx.expired:
                    raise GlobalTimeoutException("全局超时: GoogleSearch 批次超时")

        _search_elapsed = time.time() - _search_start

        # 合并为扁平 dict（兼容下游）
        _search_results = {}
        for q, results in grouped_results.items():
            for result in results:
                url = result.get('url', '')
                if url not in _search_results:
                    _search_results[url] = result
                else:
                    _search_results[url]['summ'] += f"\n{result['summ']}"

        for item in _search_results.values():
            if not item['url']:
                clues = f"This is an official summary of Wikipedia's information as summarized by the authoritative Google: {item['summ']}"
                item['summ'] = clues
        search_results = {idx: result for idx, result in enumerate(_search_results.values())}

        # DEBUG: 记录搜索结果（含耗时）
        logger.debug("[GoogleSearch execute] results_count=%d, elapsed=%.2fs, urls=%s", len(search_results), _search_elapsed, [v.get('url', '') for v in search_results.values()])

        # 保存分组结果供筛选使用
        self._last_grouped_results = grouped_results
        return search_results


    @cached(cache=TTLCache(maxsize=100, ttl=600))
    def _search(self, query: str, max_retry: int = 3) -> dict:
        max_num_retries, errmsg = 0, ''
        while max_num_retries < max_retry:
            if self._ctx and self._ctx.expired:
                raise GlobalTimeoutException("全局超时: GoogleSearch")

            with self._lock:
                if len(self.invalid_keys) == len(self.api_key):
                    raise NonRetryableError('[GoogleSearch] All keys have insufficient quota.')

                while True:
                    self.key_ctr += 1
                    if self.key_ctr == len(self.api_key):
                        self.key_ctr = 0

                    if self.api_key[self.key_ctr] not in self.invalid_keys:
                        break

            api_key = self.api_key[self.key_ctr]

            try:
                response = self._call_serper_api(api_key, query)
                if 'status' in response:
                    if response['statusCode'] == 400:
                        self.invalid_keys.add(api_key)
                        logger.warning("[GoogleSearch] Retry %d/%d due to status 400", max_num_retries + 1, max_retry)
                        continue
                return self._parse_response(response)
            except Exception as e:
                errmsg = f'{type(e).__name__}: {e}'
                logger.warning("[GoogleSearch] Retry %d/%d: %s", max_num_retries + 1, max_retry, errmsg)
                time.sleep(0.5)

            max_num_retries += 1
        raise NonRetryableError(f'GoogleSearch 重试 {max_num_retries} 次后仍失败: {errmsg}')


    def _call_serper_api(self, api_key, query: str) -> dict:

        logger.debug("[Serper API Request] query=%s", query)

        _timeout = min(self._ctx.remaining, 30) if self._ctx else 30
        conn = HTTPSConnection("google.serper.dev", timeout=_timeout)
        payload = json.dumps({
            "q": query
        })
        headers = {
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        }
        try:
            _api_start = time.time()
            conn.request("POST", "/search", payload, headers)
            res = conn.getresponse()
            data = res.read()
            _api_elapsed = time.time() - _api_start
            result = json.loads(data.decode("utf-8"))

            # DEBUG: 记录搜索 API 原始响应摘要（含耗时）
            organic_count = len(result.get('organic', []))
            has_answer_box = bool(result.get('answerBox'))
            has_kg = bool(result.get('knowledgeGraph'))
            logger.debug(
                "[Serper API Response] query=%s, elapsed=%.2fs, organic_results=%d, has_answer_box=%s, has_knowledge_graph=%s",
                query, _api_elapsed, organic_count, has_answer_box, has_kg
            )

            return result
        finally:
            conn.close()


    def _filter_results(self, results: List[tuple]) -> dict:
        filtered_results = {}
        count = 0
        url_count = 0
        for url, snippet, title in results:
            if all(domain not in url for domain in self.black_list):
                filtered_results[count] = {
                    'url': url,
                    'summ': json.dumps(snippet, ensure_ascii=False),
                    'title': title,
                }
                count += 1
                if url:
                    url_count += 1
                    if url_count >= self.topk:
                        break
        return filtered_results

    def _parse_response(self, response: dict) -> dict:
        raw_results = []
        if isinstance(response, str):
            import ast
            response = ast.literal_eval(response)
        if response.get('answerBox'):
            answer_box = response.get('answerBox', {})
            if answer_box.get('answer'):
                raw_results.append(('', answer_box.get('answer'), ''))
            elif answer_box.get('snippet'):
                raw_results.append(('', answer_box.get('snippet').replace('\n', ' '), ''))
            elif answer_box.get('snippetHighlighted'):
                raw_results.append(('', answer_box.get('snippetHighlighted'), ''))

        if response.get('knowledgeGraph'):
            kg = response.get('knowledgeGraph', {})
            description = kg.get('description', '')
            attributes = '. '.join(f'{attribute}: {value}' for attribute, value in kg.get('attributes', {}).items())
            raw_results.append(
                (
                    kg.get('descriptionLink', ''),
                    f'{description}. {attributes}' if attributes else description,
                    f"{kg.get('title', '')}: {kg.get('type', '')}.",
                )
            )

        if 'result' in response:
            for result in response['result']:
                description = result.get('body', '')
                attributes = '. '.join(
                    f'{attribute}: {value}' for attribute, value in result.get('attributes', {}).items()
                )
                raw_results.append(
                    (
                        result.get('href', ''),
                        f'{description}. {attributes}' if attributes else description,
                        result.get('title', ''),
                    )
                )
        elif 'organic' in response: # for serper.dev free
            for result in response['organic']:
                description = result.get('snippet', '')
                raw_results.append(
                    (
                        result.get('link', ''),
                        description,
                        result.get('title', ''),
                    )
                )
        else:
            logger.warning("[GoogleSearch] Unexpected search response: %s", response)

        return self._filter_results(raw_results)