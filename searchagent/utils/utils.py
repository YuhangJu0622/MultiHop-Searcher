import time, json, scrapy, inspect, sys, importlib, re, trafilatura, os, datetime, logging, urllib
import pdfplumber
import requests
from docx import Document
import pandas as pd
from io import BytesIO
from json_repair import repair_json
from contextlib import contextmanager
from htmldate import find_date
from functools import partial
from typing import Any, Dict, Union
from pdfminer.pdfdocument import PDFTextExtractionNotAllowed
import ast


class CoreLogFilter(logging.Filter):
    """核心日志过滤器：只保留关键阶段信息，过滤掉第三方库噪音。
    
    等效于之前 shell 脚本中的 grep 过滤逻辑：
        grep -E '(关键词...)' | grep -v 'httpcore' | grep -v 'htmldate' | ...
    """

    INCLUDE_KEYWORDS = (
        '[SearchAgent]', '[Planner]', '[Searcher]', '[Reader]',
        '[JinaReader]', '[GoogleSearch]', '[AgentInterface]', '[WebPageCache]',
        '[LLM _chat', '[LLM _stream_chat',
        'Process completed', 'Timeit',
        '全局超时', 'fallback', '去重', '筛选', '摘要',
    )

    EXCLUDE_LOGGERS = ('httpcore', 'htmldate', 'pdfminer', 'charset_normalizer')

    def filter(self, record: logging.LogRecord) -> bool:
        # 排除第三方库噪音
        if any(record.name.startswith(ns) for ns in self.EXCLUDE_LOGGERS):
            return False

        # WARNING 及以上级别始终保留
        if record.levelno >= logging.WARNING:
            return True

        # 检查消息是否包含任一关键词
        msg = record.getMessage()
        return any(kw in msg for kw in self.INCLUDE_KEYWORDS)


def setup_logging(log_level: str = "INFO", log_file: str = None, log_file_filtered: bool = False):
    """集中配置日志系统，支持 INFO/DEBUG 分级。

    Args:
        log_level (str): 日志级别，支持 "DEBUG", "INFO", "WARNING", "ERROR"。
            - INFO 模式：只输出 agent 执行阶段等基本信息。
            - DEBUG 模式：额外输出每次 LLM API 和搜索 API 调用的输入输出详细信息。
        log_file (str, optional): 日志文件路径。如果提供，日志会同时写入文件和控制台。
        log_file_filtered (bool): 如果为 True，文件日志仅写入核心阶段信息（等效于之前的
            grep 过滤），跳过生成完整日志。默认为 False。
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    # 日志格式（含线程标识，方便并发日志追踪）
    fmt = '%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s'
    datefmt = '%m-%d %H:%M:%S'
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    # 清除已有的 handler（避免重复配置）
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    # 控制台 handler：跟随 --log_level 参数
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 文件 handler：根据 log_level 输出对应级别的日志
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        if log_file_filtered:
            file_handler.addFilter(CoreLogFilter())
        root_logger.addHandler(file_handler)

    # 抑制第三方库的噪音日志
    _suppress_noisy_loggers()

    logging.info(f"日志系统已初始化: level={log_level}, log_file={log_file}, filtered={log_file_filtered}")


def _suppress_noisy_loggers():
    """抑制第三方库的日志噪音。"""
    noisy_loggers = {
        "watchdog": logging.WARNING,
        "readability.readability": logging.WARNING,
        "charset_normalizer": logging.WARNING,
        "htmldate.extractors": logging.WARNING,
        "htmldate.core": logging.WARNING,
        "scrapy.middleware": logging.WARNING,
        "scrapy.utils.log": logging.WARNING,
        "trafilatura.main_extractor": logging.WARNING,
        "trafilatura.htmlprocessing": logging.WARNING,
        "pdfplumber": logging.ERROR,
        "pdfminer": logging.WARNING,
        "httpx": logging.WARNING,
        "openai": logging.WARNING,
        "urllib3": logging.WARNING,
    }
    for logger_name, level in noisy_loggers.items():
        logging.getLogger(logger_name).setLevel(level)


# 默认初始化（兼容直接 import 时的行为）
_suppress_noisy_loggers()


def cal_timediff(start):
    _timediff_logger = logging.getLogger(__name__)
    now = time.time()
    diff = now - start
    _timediff_logger.info("[Timeit] Total time consumed: %.2fs", diff)
    return diff

def get_tool_name(tool_call):
    return tool_call.function.name

def get_tool_arg(tool_call):
    return tool_call.function.arguments

def parse_resp_to_json(resp_str):
    if not isinstance(resp_str, str):
        return resp_str

    if not resp_str:
        return {}
    completed_json = repair_json(resp_str)
    if not completed_json:
        return resp_str
    try:
        json_for_fc = json.loads(completed_json)
        if json_for_fc == '' or json_for_fc ==[]:
            return json_for_fc
        elif isinstance(json_for_fc, list):
            return json_for_fc[0]
        else:
            return json_for_fc
    except json.JSONDecodeError as e:
        raise ValueError(f"解析 JSON 时出错: {e}")

def extract_int(index):
    if isinstance(index, int):  
        return index
    elif isinstance(index, str):
        match = re.search(r'\d+', index)
        return int(match.group()) if match else None
    else:
        return None

def load_multiple_dict(resp_str):
    # case: Function(arguments='{"plans": ["2012年获得奥斯卡最佳男主角的演员是英国人吗？"]}{"plans": ["2013年获得奥斯卡最佳男主角的演员是英国人吗？"]}', name='SolvePlan')
    # assume that these dicts share the same key, then merge the values in a list
    merged_dict = {}
    completed_json = repair_json(resp_str)
    pattern = r'\{.*?\}'
    json_strings = re.findall(pattern, completed_json, re.S)
    for json_str in json_strings:
        try:
            json_obj = parse_resp_to_json(json_str)
            for k in json_obj:
                if k in merged_dict:
                    merged_dict[k].append(json_obj[k])
                else:
                    merged_dict[k] = [json_obj[k]] if not isinstance(json_obj[k], list) else json_obj[k]
        except json.JSONDecodeError as e:
            logging.getLogger(__name__).warning("[utils] load_multiple_dict JSON 解析失败: %s", e)
            raise
    return merged_dict

def check_ans_valid(resp_str):
    final_resp_json = parse_resp_to_json(resp_str)
    if final_resp_json.get('concise_answer', '') or final_resp_json.get('detailed_answer', ''):
        return True
    return False

def dict_value_isnone(dict):
    isnone = False
    for v in dict.values():
        if not v:
            isnone=True
            break
    return isnone
    
def parse_resp_content(resp):
    if not isinstance(resp, str): # tool call
        return parse_resp_to_json(resp.tool_calls[0].function.arguments)
    else:
        return parse_resp_to_json(resp)
    
def is_complete_json(json_str):
    try:
        json.loads(json_str)
        return True  
    except json.JSONDecodeError:
        return False  
    
def finish_condition(resp):
    if resp and "actions" in resp and resp['actions'] == 'final_response':
        return True
    else:
        return False

@contextmanager
def timeit(description: str):
    _timeit_logger = logging.getLogger(__name__)
    start_time = time.time()  
    try:
        yield 
    finally:
        end_time = time.time()  
        elapsed_time = end_time - start_time  
        _timeit_logger.info("[Timeit] %s: %.2fs", description, elapsed_time)
        
def load_class_from_string(class_path: str, path=None):
    path_in_sys = False
    if path:
        if path not in sys.path:
            path_in_sys = True
            sys.path.insert(0, path)

    try:
        module_name, class_name = class_path.rsplit('.', 1)
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        return cls
    finally:
        if path and path_in_sys:
            sys.path.remove(path)


def create_object(config: Union[Dict, Any] = None):
    """Create an instance based on the configuration where 'type' is a
    preserved key to indicate the class (path). When accepting non-dictionary
    input, the function degenerates to an identity.
    """
    if config is None or not isinstance(config, dict):
        return config
    assert isinstance(config, dict) and 'type' in config

    config = config.copy()
    obj_type = config.pop('type')
    if isinstance(obj_type, str):
        obj_type = load_class_from_string(obj_type)
    if inspect.isclass(obj_type):
        obj = obj_type(**config)
    else:
        assert callable(obj_type)
        obj = partial(obj_type, **config)
    return obj


def remove_think_tags(text: str) -> str:
    if not text:
        return text
    THINK_TAGS = re.compile(r'<think>[^<]*</think>', re.DOTALL)
    STRAY_CLOSE_TAG = re.compile(r'</think>', re.DOTALL)
    
    if re.search(STRAY_CLOSE_TAG, text):
        text = text.split('</think>', 1)[-1]
    
    # 然后处理完整标签
    text = re.sub(THINK_TAGS, '', text)
    
    return text.strip()

def parse_keys(score_dict:dict)->dict:
    original_keys = list(score_dict.keys())  # 假设这是你的字符串
    new_keys = []
    for ori_key in original_keys:
        if isinstance(ori_key, int):
            new_keys.append(ori_key)
        else:
            match = re.search(r'\d+', ori_key)  # 查找连续的数字
            if match:
                number = match.group()  # 提取匹配到的数字
                new_keys.append(int(number))
            else:
                raise ValueError(f"chunk 划分错误")
    parsed_score_dict = {new_key: score_dict[ori_key] for new_key, ori_key in zip(new_keys, original_keys)}
    return parsed_score_dict
