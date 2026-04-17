from enum import IntEnum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel
import os, sys
    
# need to integrate int, so asdict can convert AgentStatusCode to int
class ModelStatusCode(IntEnum):
    END = 0  # end of streaming
    STREAM_ING = 1  # response is in streaming
    SERVER_ERR = -1  # triton server's error
    SESSION_CLOSED = -2  # session has been closed
    SESSION_OUT_OF_LIMIT = -3  # request length out of limit
    SESSION_INVALID_ARG = -4  # invalid argument
    SESSION_READY = 2  # session is ready for inference


class AgentStatusCode(IntEnum):
    END = 0  # end of streaming
    STREAM_ING = 1  # response is in streaming
    SERVER_ERR = -1  # triton server's error
    SESSION_CLOSED = -2  # session has been closed
    SESSION_OUT_OF_LIMIT = -3  # request length out of limit
    SESSION_INVALID_ARG = -4  # invalid argument
    SESSION_READY = 2  # session is ready for inference
    PLUGIN_START = 3  # start tool
    PLUGIN_END = 4  # finish tool
    PLUGIN_RETURN = 5  # finish tool
    CODING = 6  # start python
    CODE_END = 7  # end python
    CODE_RETURN = 8  # python return


# ---- 自定义异常 ----
class GlobalTimeoutException(Exception):
    """全局超时：整个问题的时间用完了，必须立刻停止所有操作。

    触发场景：
    - TimeoutContext 看门狗到期
    - 各组件检测到 ctx.expired 为 True

    上层处理：SearchAgent 捕获后进入 _fallback_answer()
    """
    pass

class NonRetryableError(Exception):
    """不可重试错误：这个请求/调用彻底失败了，不要再尝试了。

    触发场景：
    - 内容安全审查不通过（DataInspectionFailed / content_filter）
    - API 认证失败（401）
    - 请求参数错误（400）
    - API 调用重试耗尽

    上层处理：放弃当前任务，降级处理（Searcher 放弃子问题，Reader 跳过 URL）
    """
    pass


class AgentMessage(BaseModel):
    content: Any
    sender: str = 'user'
    formatted: Optional[Any] = None
    extra_info: Optional[Any] = None
    type: Optional[str] = None
    receiver: Optional[str] = None
    stream_state: Union[ModelStatusCode, AgentStatusCode] = AgentStatusCode.END