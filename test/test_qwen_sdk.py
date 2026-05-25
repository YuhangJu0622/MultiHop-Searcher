"""
基于 NoDesk 网关利用 OpenAI SDK 调用 Qwen3.5-Plus (带思考模式)
这种方式利用了 OpenAI SDK 的 extra_body 参数将网关特有的透传参数混入 payload 中。
"""

import os
import json
from openai import OpenAI
from dotenv import load_dotenv

# 加载环境变量 (需要有 NODESK_GATEWAY_KEY 变量)
load_dotenv()
api_key = "nd-774439757b03380da6f241163360a87f438a79de501ac3f6742ee13d7ae71a95"
# 强制转换编码，避免某些 Python 环境下的 httpx ascii 报错
# 注意如果在 .env 中未配置则降级为 "你的网关Key"，转换 latin-1 是为了处理中文报错
api_key_bytes = api_key.encode("utf-8") if api_key else b""
try:
    api_key_str = api_key_bytes.decode("latin-1")
except:
    api_key_str = api_key

# 初始化 OpenAI 客户端
# 注意：
# OpenAI SDK 默认会自动在 base_url 后面拼接 /chat/completions。
# 如果网关是纯透传模式（严格匹配 /default/passthrough），这种拼接会导致 404 (请求到了 /default/passthrough/chat/completions)。
# 为了避免路径拼接问题，部分网关设计允许你提供一个基础根路径。
# 这里我们采用一种兼容思路：如果网关能识别这种请求，我们把透传根路径设置好。
# 如果执行报 404，说明你们的网关严格校验了 URL，不支持 SDK 的自动后缀拼接。
client = OpenAI(
    api_key=api_key_str,
    # 末尾加 ? 是一个 Hack，让 OpenAI 默认拼接的 /chat/completions 变成 query param，避免在纯透传网关触发 404
    base_url="https://llm-gateway-api.nodesk.tech/default/passthrough?"
)

def chat_with_sdk():
    try:
        print("正在通过 OpenAI SDK 请求模型 (思考模式)...")
        # client.chat.completions.create 会发送请求
        response = client.chat.completions.create(
            # model 字段是必需的
            model="qwen3.5-plus", 
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "9.11和9.8哪个大？请一步步思考。"}
            ],
            # extra_body 是 OpenAI SDK 的一个高级特性
            # 它允许你向发送给服务器的 JSON body 中强行注入额外的字段
            extra_body={
                # --- 网关需要的透传字段 ---
                "channel": "DMX",  # 你的实际渠道名
                "channel_url": "https://www.dmxapi.cn/v1/chat/completions",
                
                # --- Qwen 思考模式专属扩展参数 (与百炼保持一致) ---
                "enable_thinking": True,
                "thinking_budget": 1024
            }
        )

        # response 是一个 Pydantic 对象，我们可以用 model_dump_json 转回 JSON 字符串查看结构
        print("====== 完整 API 响应 ======")
        print(response.model_dump_json(indent=2))

        print("\n====== 解析后的回答 ======")
        choice = response.choices[0]
        
        # 兼容读取思考内容 (适配 o1 风格的 reasoning_content 和 deepseek 的 <think>)
        reasoning_content = getattr(choice.message, "reasoning_content", "")
        content = choice.message.content or ""
        
        if reasoning_content:
            print("【思考过程】:")
            print(reasoning_content)
            print("\n【最终回答】:")
            print(content)
        elif content and "<think>" in content:
            print("【思考与最终回答合并】:")
            print(content)
        else:
            print("【回答内容】:")
            print(content)
            
        print("\n====== Token 统计 ======")
        if response.usage:
            usage = response.usage
            print(f"总消耗: {usage.total_tokens}")
            print(f"输入: {usage.prompt_tokens}")
            print(f"输出: {usage.completion_tokens}")
            
            # 尝试读取 details 中的 reasoning_tokens
            details = getattr(usage, "completion_tokens_details", None)
            if details and getattr(details, "reasoning_tokens", None) is not None:
                print(f"其中思考消耗: {details.reasoning_tokens}")

    except Exception as e:
        print(f"API 调用失败: {e}")

if __name__ == "__main__":
    chat_with_sdk()
