import re, json, logging, traceback, copy, time
import jsonschema
from ..schema import NonRetryableError, GlobalTimeoutException
from typing import Dict
from pydantic import Field
from ..models.basellm import BaseStreamingAgent
from ..models.reader import Reader
from ..models.recorder import Recorder
from ..schema import AgentMessage 
from ..utils.utils import *
from ..utils.url_normalizer import url_normalizer
from ..tools.tool_collection import ToolCollection
from ..tools.final_answer import FinalAnswerTool
from ..tools.websearch import GoogleSearch

logger = logging.getLogger(__name__)
class Searcher(BaseStreamingAgent):
    """A module responsible for parsing and summarizing relevant information from search results."""
    def __init__(
        self,
        llm,
        reader: Reader,
        collected_tools: ToolCollection = None,
        user_input_template: str = "{question}",
        user_context_template: str = None,
        max_turn: int = 5,
        max_length = 24576,
        **baseconfig
    ):
        """
        A module responsible for parsing and summarizing relevant information from search results.

        Args:
            user_input_template (`str`): A template string for formatting the user input. Defaults to "{question}".
            user_context_template (`str`): A template string for formatting the context of the user input. Defaults to None.
            **baseconfig: Additional configuration parameters passed to the base class.

        Methods:
            read(question, search_result, recorder, session_id=0, **kwargs): Summarizes the search results and extracts references.
            parse(search_result): Placeholder method for parsing search results.
            _update_ref(ref, ref2url, ptr): Updates reference indices in the response.
            _generate_references_from_graph(query, response, reference): Generates references from the search results.
        """ 
        self.reader = reader
        self.user_input_template = user_input_template
        self.user_context_template = user_context_template
        self.ptr=0 
        if 'qwen' in llm.model_type.lower():
            self.max_length = 24576
        else:
            self.max_length = 128000
        if collected_tools:
            self.collected_tools = collected_tools
        else:
            self.collected_tools: ToolCollection = ToolCollection(GoogleSearch(), FinalAnswerTool())
            
        self.tools_schema = [tool.to_schema() for tool in self.collected_tools.tools]
        self.search_results = {}
        self.url_to_chunk_score = {}
        self.visited_urls = set()  # 跨迭代 URL 去重集合（存储标准化后的 URL）
        super().__init__(llm=llm, max_turn=max_turn, **baseconfig)

    def search(
        self,
        question, # 当前问题 
        recorder, 
        session_id:int =0, 
        ctx=None,
        **kwargs
    ):
        """
        Parses and summarizes the search results with extracted references.
        If the search engine summary doesn't contain enough info, the crawler will crawl the most importangt pages 
        and llm will regenerate summary for current question. 

        Args:
            question (str): The sub-question being addressed.
            search_result (str): The raw search results from the Searcher.
            recorder (Recorder): The Recorder instance for maintaining search state.
            session_id (int): The session ID for tracking the search process. Defaults to 0.
            **kwargs: Additional keyword arguments.

        Returns:
            tuple: A tuple containing the summarized references and a dictionary of reference URLs.
        """     

        def prepare_search(node_name, recorder):
            """
            Args:
                node_name(`str`): The current subqury
                recorder(`recorder`)
            Return:
                topic(`str`): The main query
                history(`List[dict]`): Tha answer of the parent subqueries
            """            
            # 获取父节点，以获得历史对话信息
            parent_nodes = []
            nodes = recorder.container['content'].nodes
            for pre_node_name in nodes.keys():
                if  pre_node_name == 'root':
                    pass
                elif pre_node_name == node_name:
                    break
                else:
                    parent_nodes.append((pre_node_name, nodes[pre_node_name]))

            parent_response = [
                dict(question=node_name, answer=node['response']) for node_name, node in parent_nodes
            ]

            return nodes['root']['content'], parent_response
        
        if ctx:
            for tool in self.collected_tools.tools:
                if hasattr(tool, '_ctx'):
                    tool._ctx = ctx

        # 每个子问题开始时重置 URL 去重集合，使去重仅作用于同一子问题的迭代搜索内
        self.visited_urls = set()

        topic, history = prepare_search(node_name=question, recorder=recorder)
        message = [self.user_input_template.format(question=question)]
        if history and self.user_context_template:
            message = [self.user_context_template.format_map(item) for item in history] + message
        message = "\n".join(message)

        # searcher每轮求解后清空memory
        self.agent.memory.reset(0)
        whether_exceed_max_tokens = False
        messages = [AgentMessage(sender="user", content=message)]

        logger.info("[Searcher] 开始搜索子问题: %s", str(question)[:200])

        try:
            for turn in range(self.max_turn):
                if turn == self.max_turn-1:
                    messages.append({
                        "role": "user",
                        "content": "Maximum number of rounds exceeded, please call final answer tools immediately based on information already collected"                    
                    })
                ignore = False
                logger.info("[Searcher] --- 迭代轮次 %d/%d ---", turn + 1, self.max_turn)
                _infer_start = time.time()
                references = ""
                references_url = {}
                _final_answer_resp = None
                try:
                    for response in super().forward(messages, tools=self.tools_schema, tool_choice="auto", session_id=session_id, ctx=ctx):

                        if isinstance(response.content, str) and response.content:
                            yield 'model_response', response.content, {}

                        elif response.content:
                            tools_in_resp, url2title, query_list = [], {}, []
                            for tool in response.content.tool_calls:
                                name = get_tool_name(tool)
                                tools_in_resp.append(name)
                                if name == 'final_answer' and turn == 0 and ('visitpage' in tools_in_resp or 'GoogleSearch' in tools_in_resp):
                                    ignore = True   
                                    continue
                                arg = get_tool_arg(tool)
                                resp = parse_resp_to_json(arg)
                                if resp and isinstance(resp, dict):
                                    if name == 'final_answer':
                                        _final_answer_resp = resp
                                    elif name == 'GoogleSearch':
                                        query_list.extend(resp.get('query', []))
                except NonRetryableError as e:
                    logger.warning("[Searcher] LLM 调用失败, 放弃子问题: %s", e)
                    yield 'final_answer', "由于 API 调用失败，子问题未能解决。", {}
                    return

                if _final_answer_resp is not None:
                    if recorder.container['content'].nodes[question]['memory']:
                        references, references_url = self._generate_references_from_graph(
                            response=_final_answer_resp.get('answer', ''),
                            ref2url=recorder.container['content'].nodes[question]['memory'],
                        )
                    else:
                        references, references_url = _final_answer_resp, {}

                    recorder.update(
                        node_name=question,
                        node_content=None,
                        content=references,
                        memory=None,
                        sender='searcher_response'
                    )
                    yield 'final_answer', references, references_url

                messages = []
                # Tool calls
                if not isinstance(response.content, str):
                    # debugs = [f"arguments: {toolcall.function.arguments}, name: {toolcall.function.name}" for toolcall in response.content.tool_calls]
                    # print(debugs)
                    for tool_call in response.content.tool_calls:
                        name = tool_call.function.name

                        # ===== B-1: 安全解析 arguments =====
                        try:
                            if name == 'final_answer':
                                # final_answer 的 answer 字段应为 string，不能用 load_multiple_dict
                                # （load_multiple_dict 会将所有值包装为 list，导致 schema 校验失败）
                                args = parse_resp_to_json(tool_call.function.arguments)
                                if not isinstance(args, dict):
                                    args = {"answer": str(args) if args else ""}
                            else:
                                args = load_multiple_dict(tool_call.function.arguments)
                        except Exception as e:
                            logger.warning("[Searcher] 参数解析失败: %s", str(e)[:200])
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({
                                    "error": "ArgumentParseError",
                                    "message": f"无法解析参数: {str(e)[:100]}。请重新生成符合 JSON 格式的参数。"
                                }, ensure_ascii=False)
                            })
                            continue

                        logger.debug("[Searcher ToolCall] name=%s, args=%s", name, str(args)[:300])
                        if name:
                            # ===== B-2: 通用 Schema 校验 =====
                            tool_obj = self.collected_tools.get_tool(name)
                            if tool_obj and hasattr(tool_obj, 'parameters') and tool_obj.parameters:
                                is_valid, error_msg = self._validate_tool_args(args, tool_obj.parameters)
                                if not is_valid:
                                    logger.warning("[Searcher] %s 参数校验失败: %s", name, error_msg[:200])
                                    # ===== A: 尝试自动修复（仅 GoogleSearch）=====
                                    if name.lower() == 'googlesearch':
                                        args = self._sanitize_google_search_args(args, question)
                                        is_valid, _ = self._validate_tool_args(args, tool_obj.parameters)
                                        if is_valid:
                                            logger.info("[Searcher] %s 参数自动修复成功", name)
                                    if not is_valid:
                                        logger.warning("[Searcher] %s 参数无法修复, 反馈 LLM", name)
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tool_call.id,
                                            "content": json.dumps({
                                                "error": "InvalidArguments",
                                                "message": f"参数校验失败: {error_msg}。请根据以下 schema 重新生成参数。",
                                                "expected_schema": tool_obj.parameters,
                                            }, ensure_ascii=False)
                                        })
                                        continue

                            if name.lower() == 'googlesearch':
                                if 'intent' in args:
                                   if isinstance(args['intent'], list):
                                        args['intent'] = ' '.join(args['intent'])
                                else:
                                    args['intent'] = "" 
                                # Ensure 'query' exists; try to extract from other fields if missing
                                if 'query' not in args:
                                    # Try common alternative key names the model might use
                                    for alt_key in ['search_query', 'queries', 'search', 'q', 'keyword', 'keywords']:
                                        if alt_key in args:
                                            args['query'] = args.pop(alt_key)
                                            break
                                    # If still missing, use intent as fallback query
                                    if 'query' not in args:
                                        if args.get('intent'):
                                            args['query'] = [args['intent']] if isinstance(args['intent'], str) else args['intent']
                                        else:
                                            args['query'] = [question]
                                        logger.warning("[Searcher] GoogleSearch missing 'query', using fallback: %s", args['query'])
                                all_argumens = copy.deepcopy(list(args.keys()))
                                for key in all_argumens:
                                    if key not in ['query', 'intent']:
                                        args.pop(key)
                                logger.info("[Searcher] --- 执行 GoogleSearch --- queries=%s", args.get('query', []))
                                # ===== B-3: 安全执行 =====
                                _gs_start = time.time()
                                try:
                                    search_results = self.collected_tools.execute(name=name, tool_input=args)
                                except GlobalTimeoutException:
                                    raise
                                except Exception as e:
                                    _gs_elapsed = time.time() - _gs_start
                                    logger.warning("[Searcher] GoogleSearch 执行异常 (%.2fs): %s", _gs_elapsed, str(e)[:200])
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "content": json.dumps({
                                            "error": "ExecutionError",
                                            "message": f"搜索执行失败: {str(e)[:200]}。请尝试修改搜索关键词后重试。"
                                        }, ensure_ascii=False)
                                    })
                                    continue
                                _gs_elapsed = time.time() - _gs_start
                                _url_list = [v.get('url', '') for v in search_results.values()] if isinstance(search_results, dict) else []
                                logger.info("[Searcher] --- GoogleSearch 完成 --- 返回 URL 数量: %d, 耗时: %.2fs", len(_url_list), _gs_elapsed)
                                logger.info("[Searcher] URLs: %s", _url_list)

                                # ===== C: 空结果检查 =====
                                if not search_results:
                                    logger.warning("[Searcher] GoogleSearch 返回空结果, queries=%s", args.get('query', []))
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "content": json.dumps({
                                            "warning": "NoResults",
                                            "message": f"搜索未返回任何结果。使用的查询: {args.get('query', [])}。请尝试不同的搜索关键词或更宽泛的查询。"
                                        }, ensure_ascii=False)
                                    })
                                    continue

                                yield 'webpages', search_results, {}

                                # ---- 跨迭代 URL 去重 ----
                                deduped_results = {}
                                for k, v in search_results.items():
                                    raw_url = v.get('url', '')
                                    if not raw_url:
                                        deduped_results[k] = v  # 保留 answerBox（无 URL）
                                        continue
                                    norm_url = url_normalizer.normalize(raw_url)
                                    if norm_url not in self.visited_urls:
                                        self.visited_urls.add(norm_url)
                                        deduped_results[k] = v
                                    else:
                                        logger.info("[Searcher] 跨迭代去重，跳过: %s", raw_url)
                                # 重建索引
                                search_results = {i: v for i, v in enumerate(deduped_results.values())}
                                logger.info("[Searcher] --- URL 去重后剩余: %d 个 ---", len(search_results))

                                # ---- 智能 URL 筛选 ----
                                # 分离 answerBox（无 URL）和有 URL 的结果
                                answer_box = {k: v for k, v in search_results.items() if not v.get('url', '')}
                                url_results = {k: v for k, v in search_results.items() if v.get('url', '')}
                                
                                filtered_results = self._filter_urls(url_results, question, args.get('query', []))
                                
                                # 合并 answerBox + 筛选后的 URL
                                merged = list(answer_box.values()) + list(filtered_results.values())
                                search_results = {i: v for i, v in enumerate(merged)}
                                logger.info("[Searcher] --- URL 筛选后: %d 个（含 answerBox %d 个）---", len(search_results), len(answer_box))

                                search_results, cur_url_to_chunk_score = self.reader.get_llm_summ(search_results, question, topic, args['intent'], args['query'], ctx=ctx)

                                if self.search_results:
                                    search_results = {key+len(self.search_results):value for key, value in search_results.items()}

                                self.search_results.update(search_results)
                                if isinstance(cur_url_to_chunk_score, dict):
                                    self.url_to_chunk_score.update(cur_url_to_chunk_score)
                                # web info return to LLM: {url, title, summ, date}
                                web_result = {k: {key: val for key, val in v.items()} for k, v in search_results.items()}
                                result = json.dumps(web_result, ensure_ascii=False)
                                recorder.update(
                                    node_name=question,
                                    node_content=args['query'],
                                    content=web_result,
                                    memory=self.agent.memory,
                                    sender='searcher'
                                )
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": copy.deepcopy(result)
                                })

                            elif name.lower() == 'final_answer' and not ignore:
                                if references:
                                    self.ptr+=len(references_url)
                                    result = resp
                                    recorder.update(
                                        node_name=question,
                                        node_content=None,
                                        content= references,
                                        memory=self.agent.memory,
                                        sender='searcher_response'
                                    )
                                    logger.info("[Searcher] --- 子问题回答完成 --- 答案: %s", str(references)[:200] if isinstance(references, str) else str(references)[:200])
                                    return references
                                else:
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "content": "Cannot execute this function call. Please retry!"
                                    }) 
                                    
                            else:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": "Based on the search results, Please answer the question again."
                                })
                                

                        else:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "Cannot execute this function call. Please retry!"
                            })

                # No tool calls, model response
                else:
                    pass
                
        except GlobalTimeoutException:
            raise
        except NonRetryableError as e:
            logger.warning("[Searcher] 不可重试错误, 放弃子问题: %s", e)
            yield 'final_answer', "由于 API 调用失败，子问题未能解决。", {}
            return
        except Exception:
            raise
        
        
    def _update_ref(self, ref: str, ref2url: Dict[str, str], ptr: int) -> str:

        """
        Updates references within a given string based on reference indices and the provided pointer.

        Args:
            ref (`str`): The reference string that needs updating.
            ref2url (`dict`): A dictionary of reference indices and their associated URLs.
            ptr (`int`): The current pointer to update references with.

        Returns:
            tuple:
                - updated_ref (`str`): The updated reference string with modified indices.
                - updated_ref2url (`dict`): A dictionary mapping updated reference indices to their URLs.
                - added_ptr (`int`): The number of new references added.
            
        """            
        numbers = list({int(n) for n in re.findall(r"\[\[(\d+)\]\]", ref)})
        if not numbers:
            return ref, {}
        numbers = {n: idx + 1 for idx, n in enumerate(numbers)}
        updated_ref = re.sub(
            r"\[\[(\d+)\]\]",
            lambda match: f"[[{numbers[int(match.group(1))] + ptr}]]",
            ref,
        )
        updated_ref2url = {}
        missing = [e for e in numbers if e not in ref2url]
        if missing:
            logger.warning("[Searcher] Illegal reference id — Missing refs: %s, available: %s", missing, sorted(ref2url.keys()))
        if ref2url:
            updated_ref2url = {
                numbers[idx] + ptr: ref2url[idx] for idx in numbers if idx in ref2url
            }
        return updated_ref, updated_ref2url


    def _generate_references_from_graph(self, response, ref2url) -> tuple[str, Dict[int, dict]]:
        """
        Generates references from the search result graph and updates the reference indices.

        Args:
            query (`str`): The original query or question.
            response (`str`): The summarized response based on the search result.
            reference (`str`): The reference string containing previous references.

        Returns:
            tuple:
                - references (`str`): The formatted reference string.
                - references_url (`dict`): A dictionary of references with their corresponding URLs.
        """
        if not ref2url:
            return response, {}
        updata_ref, ref2url = self._update_ref(
            response, ref2url, self.ptr
        )
        return updata_ref, ref2url

    def _filter_urls(self, url_results: dict, question: str, queries: list, top_k: int = 10) -> dict:
        """智能 URL 筛选：根据 URL 数量自适应选择策略。

        - <= top_k: 全部保留
        - top_k+1 ~ top_k*2: 交叉排序（Round-Robin）
        - > top_k*2: 调用 LLM 筛选

        Args:
            url_results: {idx: {url, title, summ, ...}} 去重后的搜索结果
            question: 当前子问题
            queries: 搜索 query 列表
            top_k: 目标 URL 数量

        Returns:
            筛选后的结果 dict
        """
        total = len(url_results)
        if total <= top_k:
            logger.info("[Searcher] URL 数量 %d <= %d, 全部保留", total, top_k)
            return url_results

        if total <= top_k * 2:
            # 交叉排序
            return self._round_robin_filter(url_results, queries, top_k)
        else:
            # LLM 筛选
            return self._llm_filter(url_results, question, top_k)

    def _round_robin_filter(self, url_results: dict, queries: list, top_k: int) -> dict:
        """交叉排序：从每个 query 的结果中轮流取，保证多样性。"""
        logger.info("[Searcher] 使用交叉排序 (Round-Robin) 筛选 top %d", top_k)

        # 尝试从 GoogleSearch 工具获取分组结果
        grouped = {}
        for tool in self.collected_tools.tools:
            if hasattr(tool, '_last_grouped_results') and tool._last_grouped_results:
                grouped = tool._last_grouped_results
                break

        if not grouped or len(grouped) <= 1:
            # 没有分组信息，直接取前 top_k
            selected = dict(list(url_results.items())[:top_k])
            return {i: v for i, v in enumerate(selected.values())}

        # Round-Robin 轮流取
        all_urls_set = {v.get('url', '') for v in url_results.values()}
        query_iters = []
        for q, results in grouped.items():
            # 只保留在 url_results 中存在的（已去重的）
            filtered = [r for r in results if r.get('url', '') in all_urls_set]
            query_iters.append(iter(filtered))

        selected = []
        selected_urls = set()
        idx = 0
        while len(selected) < top_k and query_iters:
            iter_idx = idx % len(query_iters)
            try:
                item = next(query_iters[iter_idx])
                url = item.get('url', '')
                if url and url not in selected_urls:
                    selected.append(item)
                    selected_urls.add(url)
                idx += 1
            except StopIteration:
                query_iters.pop(iter_idx)
                if query_iters:
                    idx = idx % len(query_iters)

        logger.info("[Searcher] Round-Robin 筛选完成: %d 个 URL", len(selected))
        return {i: v for i, v in enumerate(selected)}

    def _llm_filter(self, url_results: dict, question: str, top_k: int) -> dict:
        """使用 LLM 进行语义筛选，选出最相关的 top_k 个 URL。"""
        logger.info("[Searcher] 使用 LLM 筛选 top %d (候选 %d 个)", top_k, len(url_results))
        from ..prompt.reader import URL_FILTER_PROMPT        # 构建候选列表文本
        url_list_str = ""
        idx_to_key = {}
        for idx, (k, v) in enumerate(url_results.items()):
            title = v.get('title', '无标题')
            snippet = v.get('summ', '')[:200]
            url = v.get('url', '')
            url_list_str += f"[{idx}] {title}\n    URL: {url}\n    摘要: {snippet}\n\n"
            idx_to_key[idx] = k

        prompt = URL_FILTER_PROMPT.format(
            top_k=top_k,
            question=question,
            url_list=url_list_str
        )

        try:
            response = self.reader.llm.chat([{"role": "user", "content": prompt}])
            content = response.content.strip()
            # 解析 JSON 数组
            selected_indices = json.loads(content)
            if not isinstance(selected_indices, list):
                raise ValueError(f"Expected list, got {type(selected_indices)}")

            # 只取有效索引
            selected = {}
            for i, idx in enumerate(selected_indices[:top_k]):
                if idx in idx_to_key:
                    selected[i] = url_results[idx_to_key[idx]]

            logger.info("[Searcher] LLM 筛选完成: 选中 %d 个 URL", len(selected))
            return selected

        except GlobalTimeoutException:
            raise
        except Exception as e:
            logger.warning("[Searcher] LLM 筛选失败 (%s), fallback 取前 %d 个", e, top_k)
            return {i: v for i, v in enumerate(list(url_results.values())[:top_k])}

    def _validate_tool_args(self, args: dict, schema: dict) -> tuple:
        """【B-2】通用 Schema 校验：用 tool 自身的 JSON Schema 校验参数。

        宽松策略：忽略 additionalProperties 限制（多余字段由现有逻辑删除），
        仅校验类型和必填字段。校验本身出错时放行（不阻塞主流程）。

        Returns:
            (is_valid, error_msg)
        """
        try:
            schema_copy = copy.deepcopy(schema)
            schema_copy.pop('additionalProperties', None)
            jsonschema.validate(instance=args, schema=schema_copy)
            return True, ""
        except jsonschema.ValidationError as e:
            return False, e.message
        except Exception as e:
            logger.warning("[Searcher] Schema 校验异常: %s", str(e)[:200])
            return True, ""  # 校验本身出错时放行

    def _sanitize_google_search_args(self, args: dict, question: str) -> dict:
        """【A】修复 GoogleSearch 参数中的常见类型问题。        处理以下情况：
        - query 列表中元素为 dict → 提取字符串
        - query 列表中元素为非字符串 → 转为字符串
        - query 为空列表 → 用 question 作为 fallback
        - query 不是列表 → 转为列表
        - intent 类型不对 → 转为字符串列表
        """
        # 修复 query
        if 'query' in args:
            if isinstance(args['query'], list):
                sanitized = []
                for item in args['query']:
                    if isinstance(item, str):
                        sanitized.append(item)
                    elif isinstance(item, dict):
                        # 尝试从 dict 中提取有用字符串
                        extracted = False
                        for key in ['query', 'search', 'q', 'keyword', 'keywords']:
                            if key in item:
                                val = item[key]
                                if isinstance(val, list):
                                    sanitized.extend(str(v) for v in val if v)
                                elif val:
                                    sanitized.append(str(val))
                                extracted = True
                                break
                        if not extracted:
                            sanitized.append(str(item))
                    elif item is not None:
                        sanitized.append(str(item))
                args['query'] = sanitized if sanitized else [question]
            elif isinstance(args['query'], str):
                args['query'] = [args['query']]
            else:
                args['query'] = [question]
            logger.info("[Searcher] query 自动修复结果: %s", args['query'])

        # 修复 intent
        if 'intent' in args:
            if isinstance(args['intent'], list):
                args['intent'] = [str(item) for item in args['intent'] if item is not None]
            elif isinstance(args['intent'], str):
                args['intent'] = [args['intent']]
            else:
                args['intent'] = [str(args['intent'])]
        else:
            args['intent'] = [""]
        return args