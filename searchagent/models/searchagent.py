import queue, copy, re, uuid, json, time, logging, traceback
from ..schema import AgentMessage, GlobalTimeoutException, NonRetryableError
from ..utils.utils import *
from ..utils.timeout_context import TimeoutContext
from .planner import Planner
from .recorder import Recorder
from .searcher import Searcher
from ..prompt.planner import FALLBACK_PROMPT_CN, FALLBACK_PROMPT_EN

logger = logging.getLogger(__name__)


# Core search agent that coordinates multiple stages of search, reading, and reasoning
class SearchAgent:
    """
    A multi-agent framework for complex search tasks, making up the workflow between Planner, Searcher, Reader and Recorder.

    Args:
        planner (`Planner`): The planner agent responsible for decomposing the user query into sub-questions.
        searcher (`Searcher`): The searcher agent responsible for generating search queries and retrieving web results.
        reader (`Reader`): The reader agent responsible for parsing and summarizing search results.
        recorder (`Recorder`): The recorder agent responsible for maintaining the state of the search process.
        max_turn (`int`): The maximum number of iterations for the search process.

    Methods:
        iterative(`query`): Executes the search process iteratively, refining the search results over multiple turns.
    """
    def __init__(
        self,
        planner: Planner,
        searcher: Searcher,
        recorder: Recorder,
        llm,
        iterative_prompt,
        max_turn: int = 15,
        max_time: int = None,
    ):
        """
        Initializes the SearchAgent with required components for each stage of the search process.
        
        Args:
            planner (`Planner`): Responsible for generating the overall plan.
            searcher (`Searcher`): Searches for relevant content based on subqueries.
            reader (`Reader`): Parses and summarizes the search results.
            recorder (`Recorder`): Records intermediate results and memories.
            max_turn (`int`): Maximum number of iterations (turns) for the process.
            max_time (`int`): Maximum total time in seconds. None means no limit.
        """
        self.planner = planner
        self.searcher = searcher
        self.recorder = recorder
        self.max_turn = max_turn
        self.llm = llm
        self.iterative_prompt = iterative_prompt
        self.max_time = max_time

    def forward(self, query, mode='iterative'):
    
        start_time = time.time()
        logger.info("[SearchAgent] ===== 开始处理问题 =====")
        logger.info("[SearchAgent] 原始问题: %s", str(query))

        # 创建统一超时上下文（看门狗）
        ctx = None
        if self.max_time:
            ctx = TimeoutContext(self.max_time)
            logger.info("[SearchAgent] 全局超时: %ds, deadline=%.0f", self.max_time, ctx.deadline)

        self.recorder.container['content'].add_root_node(node_content=query)
        try:
            self.planner.agent.system_prompt = self.iterative_prompt
            for response in self.iterative(query, ctx=ctx):
                yield response
        except Exception as e:
            logger.warning("[SearchAgent] 主流程异常 (%s: %s)，进入 fallback 回答 (已用 %.1fs)",
                           type(e).__name__, str(e)[:200], time.time() - start_time)
            yield from self._fallback_answer(query)
        finally:
            if ctx:
                ctx.cancel()
        elapsed = time.time() - start_time
        logger.info("[SearchAgent] ===== 处理完成 ===== 总耗时: %.2fs", elapsed)

    def _fallback_answer(self, query):
        """使用 Recorder 中已收集到的信息 + LLM 生成兜底回答。"""
        collected_info = ""
        try:
            graph = self.recorder.container['content']
            for node_name, node_data in graph.nodes.items():
                if node_name not in ('root', 'response') and node_data.get('response'):
                    collected_info += f"子问题: {node_name}\n回答: {node_data['response']}\n\n"
        except Exception:
            pass

        ascii_count = sum(1 for c in str(query) if c.isascii() and c.isalpha())
        use_en = ascii_count / max(len(str(query)), 1) > 0.5
        system_prompt = FALLBACK_PROMPT_EN if use_en else FALLBACK_PROMPT_CN

        if collected_info:
            user_content = f"已收集信息:\n{collected_info}\n用户问题: {query}"
        else:
            user_content = query

        formatted_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            final_resp = ""
            for _status_code, resp, _ in self.llm.stream_chat(formatted_messages):
                if isinstance(resp, str):
                    final_resp = resp
                elif hasattr(resp, 'content') and resp.content:
                    final_resp = resp.content
            final_resp = remove_think_tags(final_resp)
        except Exception as e:
            logger.warning("[SearchAgent] fallback LLM 调用失败 (%s), 使用已收集信息兜底", e)
            if collected_info:
                final_resp = collected_info
            else:
                final_resp = str(query)

        parsed = parse_resp_to_json(final_resp)
        if isinstance(parsed, dict) and 'concise_answer' in parsed:
            yield {
                "final_resp": parsed,
                "status": "reasoning"
            }
        else:
            yield {
                "final_resp": final_resp,
                "status": "reasoning"
            }
        

    def iterative(self, query, ctx=None):
        """
        Executes the search process iteratively, refining the search results over multiple turns.

        Args:
            query (`str`): The user query to be processed.
            ctx (`TimeoutContext`, optional): Unified timeout context (watchdog).

        Returns:
            response(`AgentMessage`): The final response generated by the Reasoner.
        """
        planner_message = queue.Queue()
        planner_message.put(query)
        _graph_state = dict(node={}, adjacency_list={}, ref2url={})
        references_url = {}

        try:

            for turn in range(self.max_turn):
                _plan_start = time.time()
                logger.info("[Planner] ===== Plan 阶段 (Turn %d/%d) =====", turn + 1, self.max_turn)

                message = planner_message.get()
                logger.info("[Planner] 输入: %s",
                            str(message)[:200] if isinstance(message, str) else f"list(len={len(message)})" if isinstance(message, list) else str(message)[:200])

                for response in self.planner.plan(
                    message=message,
                    recorder=self.recorder,
                    ctx=ctx
                ):
                    current_plan = parse_resp_to_json(response.content)

                    if isinstance(current_plan, dict) and 'actions' in current_plan:
                        if current_plan['actions'] == 'final_response':
                            yield {
                                'final_resp': current_plan,
                                'status': 'reasoning',
                                'ref2url': references_url
                            }
                        else:
                            yield {
                                'plan': current_plan,
                                'status': 'planning'
                            }

                _plan_elapsed = time.time() - _plan_start
                logger.info("[Planner] 输出: actions=%s, content=%s",
                            current_plan.get('actions', '?') if isinstance(current_plan, dict) else '?',
                            str(current_plan.get('content', ''))[:200] if isinstance(current_plan, dict) else str(current_plan)[:200])
                logger.info("[Planner] --- Plan 阶段完成 --- 耗时: %.2fs", _plan_elapsed)

                # Execute search and summarize results for each sub-query
                
                if not finish_condition(current_plan) and current_plan['actions'] == 'extract_problems':
                    current_subquery = current_plan['content']
                    step_message = [] 
                    if isinstance(current_subquery, list):
                        current_subquery = current_subquery[-1]
                    logger.info("[Searcher] ===== Search 阶段: %s =====", str(current_subquery)[:200])

                    _search_start = time.time()

                    for tool_name, search_result, references_url in self.searcher.search(
                        question=current_subquery, 
                        recorder=self.recorder,
                        ctx=ctx,
                    ):
                        if tool_name == 'webpages':
                            yield {
                                'status':'webpages',
                                'content':search_result
                            }
                        else:
                            yield {
                                'status': 'searching',
                                'substatus': tool_name,
                                'tool_return': search_result,
                                'ref2url': references_url
                            }

                    _search_elapsed = time.time() - _search_start
                    _graph_state.update(node=self.recorder.container['content'].nodes, adjacency_list=self.recorder.container['content'].adjacency_list)
                    _graph_state['ref2url'].update(references_url)
                    if isinstance(search_result, dict):
                        if 'answer' in search_result:
                            search_result = search_result['answer']
                    step_message.append(
                        AgentMessage(
                            sender="searcher",
                            content=search_result if search_result else 'can not find realted information!',
                            formatted=copy.deepcopy(_graph_state)
                        )
                    )
                    logger.info("[Searcher] --- Search 阶段完成 --- 耗时: %.2fs", _search_elapsed)

                    planner_message.put(step_message)

                elif finish_condition(current_plan):
                    logger.info("[SearchAgent] ===== 最终回答 ===== 迭代轮次: %d", turn + 1)
                    response.formatted = _graph_state
                    return response

                else:
                    step_message = f"Error: {current_plan['evaluation_previous_goal']}."
                    planner_message.put(step_message)

            # reason after max_turn
            if not finish_condition(current_plan):
                logger.info("[SearchAgent] 超出最大迭代次数 (%d), 强制生成最终回答", self.max_turn)
                _reason_start = time.time()
                message="Maximum number of rounds exceeded, please answer user questions immediately based on information already collected"
                for response in self.planner.plan(message=message, recorder=self.recorder, ctx=ctx):
                    reason_message = parse_resp_to_json(response.content)
                    yield {
                        'final_resp': reason_message, 
                        'status': 'reasoning',
                        'ref2url': references_url # global index
                    }
                _reason_elapsed = time.time() - _reason_start
                logger.info("[SearchAgent] --- 强制回答完成 --- 耗时: %.2fs", _reason_elapsed)
                response.formatted = _graph_state
                return response
                
        except Exception:
            raise