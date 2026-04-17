"""
批量测试脚本：逐条调用 ManuSearch 进行回答。
支持 question.jsonl（含 id）和 validation.jsonl（无 id，自动按行号编号）。
输出 answer.jsonl + 每题独立的 outputs/logs 文件用于 debug。
每题日志通过动态挂载 FileHandler 自动捕获项目代码的完整 logging 输出。
支持按 id 区间选择、断点续跑。
"""

import sys, os, json, time, random
import numpy as np
import argparse
import asyncio

p1 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(p1)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

import logging
from searchagent.utils.utils import setup_logging
from searchagent.agent.agent import AgentInterface

_logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="批量测试脚本，支持 question.jsonl / validation.jsonl，生成 answer.jsonl")

    parser.add_argument('--input_file', type=str, default='../question.jsonl',
                        help="输入的 JSONL 文件路径。支持 {\"id\":N, \"question\":\"...\"} 或 {\"question\":\"...\"}（无 id 时按行号自动编号）")
    parser.add_argument('--answer_file', type=str, default=None,
                        help="输出 answer.jsonl 的路径。默认为项目根目录下 answer.jsonl。")
    parser.add_argument('--output_dir', type=str, default=None,
                        help="每题 debug 输出目录。默认为 outputs/question/")
    parser.add_argument('--log_dir', type=str, default=None,
                        help="每题 debug 日志目录。默认为 logs/question/")
    parser.add_argument('--resume', action='store_true', default=False,
                        help="断点续跑：如果 answer.jsonl 已存在，跳过已完成的问题。")
    parser.add_argument('--start_idx', type=int, default=0,
                        help="从第几个 id 开始（inclusive），默认 0。")
    parser.add_argument('--end_idx', type=int, default=-1,
                        help="到第几个 id 结束（exclusive），默认 -1 表示处理到末尾。")

    parser.add_argument('--google_subscription_key', type=str, required=True)
    parser.add_argument('--google_search_topk', type=int, default=5)
    parser.add_argument('--proxy', type=str, help="port-based proxy (e.g., localhost:8080)")

    parser.add_argument('--planner_model_name', type=str, required=True)
    parser.add_argument('--planner_api_base', type=str, required=True)
    parser.add_argument('--planner_api_key', type=str, required=True)
    parser.add_argument('--searcher_model_name', type=str, required=True)
    parser.add_argument('--searcher_api_base', type=str, required=True)
    parser.add_argument('--searcher_api_key', type=str, required=True)
    parser.add_argument('--reader_model_name', type=str, required=True)
    parser.add_argument('--reader_api_base', type=str, required=True)
    parser.add_argument('--reader_api_key', type=str, required=True)
    parser.add_argument('--cache_dir', type=str, required=False)
    parser.add_argument('--concurrent_limit', type=int, default=32)

    parser.add_argument('--temperature', type=float, default=0.6)
    parser.add_argument('--top_p', type=float, default=0.95)
    parser.add_argument('--min_p', type=float, default=0.0)
    parser.add_argument('--top_k', type=int, default=30)
    parser.add_argument('--repetition_penalty', type=float, default=1.05)
    parser.add_argument('--max_new_tokens', type=int, default=8192)
    parser.add_argument('--searcher_same_parameters', type=int, default=True)
    parser.add_argument('--reader_same_parameters', type=int, default=True)

    parser.add_argument('--max_time', type=int, default=None,
                        help="每条问题的全局超时（秒），推荐 480。")
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--log_level', type=str, default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    parser.add_argument('--log_file', type=str, default=None)
    parser.add_argument('--log_file_filtered', action='store_true', default=False)

    return parser.parse_args()


def load_questions(input_file: str) -> list:
    """加载 JSONL 文件。对于没有 id 字段的条目（如 validation.jsonl），按行号自动编号。"""
    questions = []
    with open(input_file, 'r', encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if 'id' not in obj:
                    obj['id'] = len(questions)
                questions.append(obj)
            except json.JSONDecodeError as e:
                _logger.warning("第 %d 行 JSON 解析失败: %s", line_no, e)
    return questions


def load_completed_ids(answer_file: str) -> set:
    """从已有的 answer.jsonl 中读取已完成的 id 集合"""
    if not os.path.exists(answer_file):
        return set()
    completed = set()
    try:
        with open(answer_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                completed.add(obj['id'])
    except Exception as e:
        _logger.warning("加载已有 answer 文件失败: %s", e)
    return completed


def append_answer(answer_file: str, qid: int, answer: str):
    with open(answer_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps({"id": qid, "answer": answer}, ensure_ascii=False) + '\n')


def save_question_output(output_dir: str, qid: int, record: dict):
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f'question_{qid}.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=4)


def add_question_log_handler(log_dir: str, qid: int, log_level) -> logging.FileHandler:
    """为单个问题动态挂载独立的文件日志 handler，捕获项目代码的完整 logging 输出。"""
    os.makedirs(log_dir, exist_ok=True)
    filepath = os.path.join(log_dir, f'question_{qid}.log')
    handler = logging.FileHandler(filepath, encoding='utf-8', mode='w')
    handler.setLevel(log_level)
    fmt = '%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s'
    handler.setFormatter(logging.Formatter(fmt, datefmt='%m-%d %H:%M:%S'))
    logging.getLogger().addHandler(handler)
    return handler


def remove_question_log_handler(handler: logging.FileHandler):
    """处理完一题后，移除并关闭该 handler。"""
    logging.getLogger().removeHandler(handler)
    handler.close()


async def process_single_question(agent, question_text: str) -> dict:
    loop = asyncio.get_event_loop()
    steps = await loop.run_in_executor(
        None,
        lambda: list(agent.get_answer(question_text, solve_method='iterative'))
    )

    answer = ''
    for step, use_en in steps:
        answer = step.get('final_resp', '')

    concise_answer = ''
    detailed_answer = ''
    if isinstance(answer, dict):
        content = answer.get('content', answer)
        if isinstance(content, dict):
            concise_answer = content.get('concise_answer', '')
            detailed_answer = content.get('detailed_answer', '')
        else:
            concise_answer = str(content)
    else:
        concise_answer = str(answer) if answer else ''

    think = await loop.run_in_executor(
        None,
        agent.recorder.generate_reason_process
    )

    return {
        'concise_answer': concise_answer,
        'detailed_answer': detailed_answer,
        'think': think,
    }


def resolve_path(path: str, base_dir: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(base_dir, path))


async def main_async():
    args = parse_args()

    if args.seed is None:
        args.seed = int(time.time())
    random.seed(args.seed)
    np.random.seed(args.seed)

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    setup_logging(log_level=args.log_level, log_file=args.log_file,
                  log_file_filtered=args.log_file_filtered)

    search_api_keys = [key.strip() for key in args.google_subscription_key.split(",")]
    args.google_subscription_key = search_api_keys

    script_dir = os.path.dirname(os.path.abspath(__file__))

    input_file = resolve_path(args.input_file, script_dir)
    answer_file = resolve_path(args.answer_file or os.path.join('..', 'answer.jsonl'), script_dir)
    output_dir = resolve_path(args.output_dir or os.path.join('..', 'outputs', 'question'), script_dir)
    log_question_dir = resolve_path(args.log_dir or os.path.join('..', 'logs', 'question'), script_dir)

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(log_question_dir, exist_ok=True)
    os.makedirs(os.path.dirname(answer_file), exist_ok=True)

    # 非 resume 模式下清空已有的 answer.jsonl，避免 id 重复
    if not args.resume and os.path.exists(answer_file):
        _logger.info("非 resume 模式，清空已有的 answer 文件: %s", answer_file)
        open(answer_file, 'w').close()

    # ---- 加载问题 ----
    questions = load_questions(input_file)
    total = len(questions)
    _logger.info("从 %s 加载了 %d 条问题", input_file, total)

    if total == 0:
        _logger.error("没有加载到任何问题，请检查输入文件。")
        return

    # ---- 按 id 筛选处理范围 ----
    start_idx = args.start_idx
    end_idx = args.end_idx if args.end_idx != -1 else total
    end_idx = min(end_idx, total)
    questions_to_process = [q for q in questions if start_idx <= q['id'] < end_idx]
    questions_to_process.sort(key=lambda x: x['id'])

    # ---- 断点续跑 ----
    completed_ids = set()
    if args.resume:
        completed_ids = load_completed_ids(answer_file)
        _logger.info("断点续跑模式：已完成 %d 条问题 (ids: %s)",
                      len(completed_ids), sorted(completed_ids))

    # ---- 初始化 Agent ----
    agent = AgentInterface(
        google_subscription_key=args.google_subscription_key,
        google_search_topk=args.google_search_topk,
        proxy=args.proxy,
        planner_model_name=args.planner_model_name,
        planner_api_base=args.planner_api_base,
        planner_api_key=args.planner_api_key,
        searcher_model_name=args.searcher_model_name,
        searcher_api_base=args.searcher_api_base,
        searcher_api_key=args.searcher_api_key,
        reader_model_name=args.reader_model_name,
        reader_api_base=args.reader_api_base,
        reader_api_key=args.reader_api_key,
        my_cache_dir=args.cache_dir,
        temperature=args.temperature,
        top_p=args.top_p,
        min_p=args.min_p,
        top_k=args.top_k,
        repetition_penalty=args.repetition_penalty,
        max_new_tokens=args.max_new_tokens,
        searcher_same_parameters=args.searcher_same_parameters,
        reader_same_parameters=args.reader_same_parameters,
        max_time=args.max_time,
    )

    # ---- 逐条处理 ----
    pending_count = sum(1 for q in questions_to_process if q['id'] not in completed_ids)
    _logger.info("=" * 60)
    _logger.info("开始批量处理 question.jsonl")
    _logger.info("处理范围: id %d ~ %d（共 %d 条，待处理 %d 条）",
                  start_idx, end_idx - 1, len(questions_to_process), pending_count)
    _logger.info("answer 文件: %s", answer_file)
    _logger.info("outputs 目录: %s", output_dir)
    _logger.info("logs 目录: %s", log_question_dir)
    _logger.info("=" * 60)

    error_count = 0
    skip_count = 0
    processed_count = 0
    total_start_time = time.time()

    for item in questions_to_process:
        qid = item['id']
        question = item['question']

        if qid in completed_ids:
            skip_count += 1
            _logger.info("[id=%d] (跳过-已完成) %s", qid, question[:60] + '...')
            continue

        q_log_handler = add_question_log_handler(log_question_dir, qid, log_level)

        _logger.info("=" * 60)
        _logger.info("[id=%d] 开始处理问题 (%d/%d)",
                      qid, processed_count + skip_count + 1, len(questions_to_process))
        _logger.info("问题: %s", question[:120] + ('...' if len(question) > 120 else ''))

        q_start = time.time()
        try:
            result = await process_single_question(agent, question)
            q_elapsed = time.time() - q_start

            concise = result['concise_answer']

            append_answer(answer_file, qid, concise)

            debug_record = {
                'id': qid,
                'question': question,
                'answer': concise,
                'detailed_answer': result['detailed_answer'],
                'think': result['think'],
                'time_seconds': round(q_elapsed, 2),
            }
            save_question_output(output_dir, qid, debug_record)

            processed_count += 1
            _logger.info("[id=%d] 完成 (耗时 %.1fs)", qid, q_elapsed)
            _logger.info("答案: %s", concise[:200] if concise else '(空)')

        except Exception as e:
            q_elapsed = time.time() - q_start
            error_count += 1
            _logger.error("[id=%d] 处理失败 (耗时 %.1fs): %s", qid, q_elapsed, str(e))

            append_answer(answer_file, qid, f'[ERROR] {str(e)}')

            error_record = {
                'id': qid,
                'question': question,
                'answer': f'[ERROR] {str(e)}',
                'error': True,
                'time_seconds': round(q_elapsed, 2),
            }
            save_question_output(output_dir, qid, error_record)
            continue
        finally:
            remove_question_log_handler(q_log_handler)

    # ---- 汇总统计 ----
    total_elapsed = time.time() - total_start_time
    _logger.info("=" * 60)
    _logger.info("批量处理完成!")
    _logger.info("总耗时: %.1f 秒 (%.1f 分钟)", total_elapsed, total_elapsed / 60)
    _logger.info("处理: %d 条 | 跳过: %d 条 | 错误: %d 条",
                  processed_count, skip_count, error_count)
    _logger.info("answer.jsonl: %s", answer_file)
    _logger.info("=" * 60)

    print("\n" + "=" * 60)
    print("批量处理完成!")
    print(f"总耗时: {total_elapsed:.1f} 秒 ({total_elapsed / 60:.1f} 分钟)")
    print(f"处理: {processed_count} 条 | 跳过: {skip_count} 条 | 错误: {error_count} 条")
    print(f"answer.jsonl: {answer_file}")
    print(f"outputs 目录: {output_dir}")
    print(f"logs 目录: {log_question_dir}")
    print("=" * 60)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
