from ..utils.utils import *
from ..utils.cache import WebPageCache
from ..models.basellm import GPTAPI, BaseStreamingAgent
from ..tools.jina_reader import JinaReader
from ..schema import NonRetryableError, GlobalTimeoutException
from concurrent.futures import ThreadPoolExecutor, as_completed 
import os, ast, logging, time
import concurrent.futures

logger = logging.getLogger(__name__)

class Reader(BaseStreamingAgent):
    def __init__(self, llm:GPTAPI, webpage_cache, summary_prompt, extract_prompt, search_api_key, proxy, use_jina=True, **baseconfig):
        self.llm = llm
        self.summary_prompt = summary_prompt
        self.extract_prompt = extract_prompt
        self.input_prompt = """##Publish Date:{date}
        ##Web title:{title}
        ##Web content:{content}"""
        self.webpage_cache = webpage_cache

        if use_jina:
            self.jina_reader = JinaReader()
        else:
            self.jina_reader = None
            from ..tools.visitpage import VisitPage
            self.visitpage = VisitPage(api_key=search_api_key, timeout=1, proxy=proxy)

        super().__init__(llm, **baseconfig)


    def get_llm_summ(self, search_results:dict, question, user_query, search_intent, current_query, ctx=None):
        logger.info("[Reader] --- 开始处理搜索结果 --- URL 数量: %d", len(search_results))

        url2id = {value['url']: key for key,value in search_results.items()}
        select_urls = []
        for key in url2id.keys():
            if key:
                select_urls.append(key)

        # First read the stored url from cache
        cached_results = {} 
        if self.webpage_cache:
            for key in list(select_urls):
                success, content = self.webpage_cache.get_content(url=key)
                if success:
                    cached_results[url2id[key]] = content 
                    select_urls.remove(key)
                    logger.info("[Reader] 缓存命中: %s", key)

        jina_contents = {}  # {url: clean_text}

        # If there are unstored urls, fetch them
        if select_urls:
            logger.info("[Reader] --- 抓取网页 --- 待抓取 URL: %d 个", len(select_urls))
            _fetch_start = time.time()

            if self.jina_reader:
                raw_contents = self.jina_reader.read_urls(select_urls, ctx=ctx)
                for url, content in raw_contents.items():
                    if content:
                        jina_contents[url] = content
                        # 缓存 Jina 返回的干净文本
                        cache_data = {
                            'url': url,
                            'title': '',
                            'date': '',
                            'content': self._chunk_content(content, chunk_size=512)
                        }
                        # 从 search_results 中获取 title
                        for v in search_results.values():
                            if v.get('url') == url:
                                cache_data['title'] = v.get('title', '')
                                cache_data['date'] = v.get('date', '')
                                break
                        self.webpage_cache.store_content(url=url, data=cache_data)
                    else:
                        logger.info("[Reader] Jina 抓取失败, 跳过: %s", url)
            else:
                # Fallback: 使用 VisitPage
                tool_return = self.visitpage.execute(
                    select_urls=select_urls,
                    search_results=search_results,
                    url_to_chunk_score=None,
                    webpage_cache=self.webpage_cache
                )
                if tool_return:
                    for item in tool_return.values():
                        url = item.get('url', '')
                        if url and item.get('content'):
                            # 将 chunk dict 合并为纯文本
                            if isinstance(item['content'], dict):
                                text = '\n'.join(item['content'].values())
                            else:
                                text = str(item['content'])
                            jina_contents[url] = text
                            self.webpage_cache.store_content(url=url, data=item)

            _fetch_elapsed = time.time() - _fetch_start
            logger.info("[Reader] --- 网页抓取完成 --- 成功 %d/%d, 耗时: %.2fs",
                         len(jina_contents), len(select_urls), _fetch_elapsed)

        # 构建 LLM 摘要输入
        messages = {}
        system_prompt = self.summary_prompt.format(
            current_plan=question, user_query=user_query,
            search_intent=search_intent, current_query=current_query
        )

        MAX_CONTENT_LENGTH = 1_500_000
        # 处理 Jina 抓取的内容
        for url, text in jina_contents.items():
            if len(text) > MAX_CONTENT_LENGTH:
                logger.warning("[Reader] 文本提取发生截断: %s, 原文本长度: %d 字符, 截断到: %d 字符", url, len(text), MAX_CONTENT_LENGTH)
                truncated = text[:MAX_CONTENT_LENGTH]
            else:
                truncated = text
            title = ''
            date = ''
            for v in search_results.values():
                if v.get('url') == url:
                    title = v.get('title', '')
                    date = v.get('date', '')
                    break
            content = self.input_prompt.format(date=date, title=title, content=truncated)
            chatbox = [
                {"role": 'system', 'content': system_prompt},
                {'role': 'user', 'content': content}
            ]
            messages[url] = chatbox

        # 处理缓存命中的内容
        for idx, item in cached_results.items():
            url = item.get('url', '')
            if not url or url in messages:
                continue
            if 'content' not in item or not item['content']:
                continue
            if isinstance(item['content'], dict):
                chunked_str = '=========='.join([f"Chunk {key}:{value}" for key, value in item['content'].items()])
            else:
                chunked_str = str(item['content'])
            if len(chunked_str) > MAX_CONTENT_LENGTH:
                logger.warning("[Reader] 缓存文本提取发生截断: %s, 原文本长度: %d 字符, 截断到: %d 字符", url, len(chunked_str), MAX_CONTENT_LENGTH)
                chunked_str = chunked_str[:MAX_CONTENT_LENGTH]
            title = item.get('title', '')
            date = item.get('date', '')
            content = self.input_prompt.format(date=date, title=title, content=chunked_str)
            chatbox = [
                {"role": 'system', 'content': system_prompt},
                {'role': 'user', 'content': content}
            ]
            messages[url] = chatbox

        # LLM 并发摘要
        logger.info("[Reader] --- LLM 内容摘要 --- 待摘要 URL: %d 个", len(messages))
        _summ_start = time.time()
        url2summ = {}
        _chat_kwargs = {}
        if ctx:
            _chat_kwargs['ctx'] = ctx
        SUMM_BATCH_TIMEOUT = 60
        with ThreadPoolExecutor(max_workers=20) as executor:
            future_to_url = {
                executor.submit(self.llm.chat, chatbox, **_chat_kwargs): url
                for url, chatbox in messages.items()
            }
            try:
                for future in concurrent.futures.as_completed(future_to_url, timeout=SUMM_BATCH_TIMEOUT):
                    url = future_to_url[future]
                    try:
                        ret = future.result()
                        llm_summ = ret.content
                        url2summ[url] = llm_summ

                    except NonRetryableError as e:
                        logger.warning("[Reader] 不可重试错误, 跳过 URL %s: %s", url, e)
                        url2summ[url] = ""

                    except GlobalTimeoutException:
                        raise

                    except Exception as e:
                        logger.warning("[Reader] LLM 摘要异常, 跳过 URL %s: %s", url, e)
                        url2summ[url] = ""
            except concurrent.futures.TimeoutError:
                done_urls = {future_to_url[f] for f in future_to_url if f.done()}
                pending_urls = [future_to_url[f] for f in future_to_url if not f.done()]
                logger.warning("[Reader] LLM 摘要批次超时 (%ds), 跳过未完成的 %d 个 URL: %s",
                               SUMM_BATCH_TIMEOUT, len(pending_urls),
                               [u.split('/')[-1][:40] for u in pending_urls])
                for f in future_to_url:
                    if not f.done():
                        url2summ[future_to_url[f]] = ""
                        f.cancel()
                if ctx and ctx.expired:
                    raise GlobalTimeoutException("全局超时: Reader LLM 摘要批次超时")

        _summ_elapsed = time.time() - _summ_start
        llm_summs = url2summ
        logger.info("[Reader] --- LLM 摘要完成 --- 摘要数量: %d, 耗时: %.2fs", len(llm_summs), _summ_elapsed)
        for url, summ in llm_summs.items():
            logger.info("[Reader] 摘要 URL: %s, 摘要长度: %d 字符", url, len(str(summ)))

        for key in llm_summs:
            reader_json = parse_resp_to_json(llm_summs[key])
            try:
                summary = reader_json.get('related_information', '')
                llm_summs[key] = summary
            except Exception:
                logger.debug("[Reader] 摘要 JSON 解析失败, key=%s", key)

        for page in search_results.values():
            if page['url'] in llm_summs:
                page['content'] = llm_summs[page['url']]
            else:
                page['content'] = ""
        return search_results, None

    @staticmethod
    def _chunk_content(text, chunk_size=512):
        """将文本切分为固定大小的 chunk dict。"""
        chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        return {i: chunk for i, chunk in enumerate(chunks)}
