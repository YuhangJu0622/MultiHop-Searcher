"""
基于 NoDesk 网关利用 Requests / OpenAI 原生格式调用 Qwen3.5-Plus (带思考模式)
因为网关是纯透传模式（/default/passthrough），官方 OpenAI SDK 强制拼接 `/chat/completions` 会导致路径错乱，
所以对于透传网关，使用 requests 构造遵循 OpenAI 格式的 Payload 是最稳妥的。
"""

import os
import json
import requests
from dotenv import load_dotenv

# 加载环境变量 (需要有 NODESK_GATEWAY_KEY 变量)
load_dotenv()
api_key = "nd-774439757b03380da6f241163360a87f438a79de501ac3f6742ee13d7ae71a95"

def chat_with_thinking():
    # 网关透传端点
    gateway_url = "https://llm-gateway-api.nodesk.tech/default/passthrough"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # 构造兼容 OpenAI 的请求 Payload
    payload = {
        # --- 网关需要的透传字段 ---
        "channel": "DMX",  # 你的实际渠道名
        "channel_url": "https://www.dmxapi.cn/v1/chat/completions",
        
        # --- OpenAI 标准字段 ---
        "model": "qwen3.5-plus", 
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "9.11和9.8哪个大？请一步步思考。"}
        ],
        
        # --- Qwen 思考模式专属扩展参数 ---
        # 对于通过 DMX 等平台调用的千问系列，配置以下参数开启思考功能
        "enable_thinking": True,
        "thinking_budget": 1024
    }

    try:
        print(f"正在通过网关请求模型 (思考模式)...")
        response = requests.post(gateway_url, headers=headers, json=payload, timeout=60)
        
        # 检查 HTTP 状态码
        response.raise_for_status()
        
        # 解析 JSON 返回数据
        data = response.json()
        
        print("====== 完整 API 响应 ======")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        
        print("\n====== 解析后的回答 ======")
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0].get("message", {})
            
            # 兼容读取思考内容
            reasoning_content = message.get("reasoning_content", "")
            content = message.get("content", "")
            
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
                
        # 打印 Token 统计
        if "usage" in data:
            usage = data["usage"]
            print("\n====== Token 统计 ======")
            print(f"总消耗: {usage.get('total_tokens')}")
            print(f"输入: {usage.get('prompt_tokens')}")
            print(f"输出: {usage.get('completion_tokens')}")
            
            details = usage.get("completion_tokens_details", {})
            reasoning_tokens = details.get("reasoning_tokens")
            if reasoning_tokens is not None:
                print(f"其中思考消耗: {reasoning_tokens}")

    except Exception as e:
        print(f"API 调用失败: {e}")

if __name__ == "__main__":
    chat_with_thinking()
