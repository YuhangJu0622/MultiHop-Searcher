"""
Prompt Ablation Test for OpenSeeker-v1-30B-SFT

Tests 5 prompt variants × 3 questions (2-hop, 3-hop, 4-hop).
Single-turn only: sends system + user, prints the model's first response.
All calls use enable_thinking=True.
"""

import asyncio
import os
import textwrap

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv(os.path.join(os.path.dirname(__file__), "agent", ".env"))

BASE_URL = os.getenv("AGENT_BASE_URL", "")
API_KEY = os.getenv("AGENT_API_KEY", "EMPTY")
MODEL = os.getenv("AGENT_MODEL", "")

client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)

# ──────────────────────────────────────────────
# System prompt (shared across experiments 1-3)
# ──────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a Web Information Seeking Master. Your task is to thoroughly "
    "seek the internet for information and provide accurate answers to questions."
)

# ──────────────────────────────────────────────
# Prompt variant 1: Full (current project prompt)
# ──────────────────────────────────────────────
PROMPT_1_FULL = """You solve questions by calling tools and reasoning step by step.

<tools>
{"name": "search", "description": "Perform web searches. Two engines: 'google' (best for English/international content) and 'bing' (best for Chinese content). Choose engine per query for best results.", "parameters": {"query": {"type": "array", "items": {"type": "string"}, "description": "Search queries. Include multiple complementary queries."}, "engine": {"type": "array", "items": {"type": "string", "enum": ["google", "bing"]}, "description": "Search engine per query, must match length of query array."}}}
{"name": "visit", "description": "Visit webpage(s) and return extracted content.", "parameters": {"url": {"type": "array", "items": {"type": "string"}}, "goal": {"type": "string", "description": "What information to extract."}}}
</tools>

Please reason about the problem deeply in your internal thought process. 
During your reasoning process, make sure to explicitly do the following:
1. Decompose the complex question into smaller manageable sub-questions and plan which sub-question to tackle next.
2. Evaluate what you know vs what you still need.
3. Before answering: verify ALL constraints are satisfied.

When you need to gather information, output a tool call in this exact format:
<tool_call>
{"name": "search", "arguments": {"query": ["English topic query", "中文主题查询"], "engine": ["google", "bing"]}}
</tool_call>

When you are ready to answer (after verification from multiple sources), please ensure your internal thought process explicitly performs a final verification:
1. Final verification: check answer against every constraint.
2. Confirm at least 2 sources agree.
3. LANGUAGE & FORMAT CHECK: What language is the question in? Is my answer in the correct language with the exact standard translation/name?

Then, output your final answer in this exact format:
<answer>your concise answer</answer>

CRITICAL RULES:
- ALWAYS decompose the question first. List ALL sub-questions explicitly.
- NEVER answer after fewer than 3 rounds of searching for multi-hop questions.
- When you find a candidate answer, ALWAYS search to verify before committing.
- For bilingual questions, search in BOTH Chinese AND English.
- Answer with ONLY the requested information - no explanations, no extra context.
- If asked for a name, ALWAYS answer with the FULL name by default.
- **Answer language**: By default, answer in the SAME language as the question.
- **FINAL ANSWER FORMAT CHECK (MANDATORY)**: Before outputting <answer>, verify language and format.

Question: """

# ──────────────────────────────────────────────
# Prompt variant 2: No thinking method instructions
# (keep tool format + critical rules, remove reasoning guidance)
# ──────────────────────────────────────────────
PROMPT_2_NO_THINKING = """You solve questions by calling tools and reasoning step by step.

<tools>
{"name": "search", "description": "Perform web searches. Two engines: 'google' (best for English/international content) and 'bing' (best for Chinese content). Choose engine per query for best results.", "parameters": {"query": {"type": "array", "items": {"type": "string"}, "description": "Search queries. Include multiple complementary queries."}, "engine": {"type": "array", "items": {"type": "string", "enum": ["google", "bing"]}, "description": "Search engine per query, must match length of query array."}}}
{"name": "visit", "description": "Visit webpage(s) and return extracted content.", "parameters": {"url": {"type": "array", "items": {"type": "string"}}, "goal": {"type": "string", "description": "What information to extract."}}}
</tools>

When you need to gather information, output a tool call in this exact format:
<tool_call>
{"name": "search", "arguments": {"query": ["English topic query", "中文主题查询"], "engine": ["google", "bing"]}}
</tool_call>

When you are ready to answer, output your final answer in this exact format:
<answer>your concise answer</answer>

CRITICAL RULES:
- ALWAYS decompose the question first. List ALL sub-questions explicitly.
- NEVER answer after fewer than 3 rounds of searching for multi-hop questions.
- When you find a candidate answer, ALWAYS search to verify before committing.
- For bilingual questions, search in BOTH Chinese AND English.
- Answer with ONLY the requested information - no explanations, no extra context.
- If asked for a name, ALWAYS answer with the FULL name by default.
- **Answer language**: By default, answer in the SAME language as the question.
- **FINAL ANSWER FORMAT CHECK (MANDATORY)**: Before outputting <answer>, verify language and format.

Question: """

# ──────────────────────────────────────────────
# Prompt variant 3: Minimal (only tool format, no thinking, no rules)
# ──────────────────────────────────────────────
PROMPT_3_MINIMAL = """You solve questions by calling tools and reasoning step by step.

<tools>
{"name": "search", "description": "Perform web searches. Two engines: 'google' (best for English/international content) and 'bing' (best for Chinese content). Choose engine per query for best results.", "parameters": {"query": {"type": "array", "items": {"type": "string"}, "description": "Search queries. Include multiple complementary queries."}, "engine": {"type": "array", "items": {"type": "string", "enum": ["google", "bing"]}, "description": "Search engine per query, must match length of query array."}}}
{"name": "visit", "description": "Visit webpage(s) and return extracted content.", "parameters": {"url": {"type": "array", "items": {"type": "string"}}, "goal": {"type": "string", "description": "What information to extract."}}}
</tools>

When you need to gather information, output a tool call in this exact format:
<tool_call>
{"name": "search", "arguments": {"query": ["query1", "query2"], "engine": ["google", "bing"]}}
</tool_call>

When you are ready to answer:
<answer>your concise answer</answer>

Question: """

# ──────────────────────────────────────────────
# Prompt variant 4: OpenSeeker original prompt
# (uses <tool_calls>/<tool_calls_end> format)
# ──────────────────────────────────────────────
OPENSEEKER_SYSTEM = """You are a tool-augmented QA agent. Cleverly leverage appropriate tools to answer the user's question.

# Tools

You may call one or more functions to assist with the user query.
You are provided with function signatures within XML tags:
<tools>
{"name": "search", "description": "Performs batched web searches: supply an array 'query'; the tool retrieves the top 10 results for each query in one call.", "parameters": {"type": "object", "properties": {"query": {"type": "array", "items": {"type": "string"}, "description": "Array of query strings. Include multiple complementary search queries in a single call."}}, "required": ["query"]}}
{"name": "visit", "description": "Parse webpage(s) and return the summary of the content according to the goal.", "parameters": {"type": "object", "properties": {"url": {"type": ["string", "array"], "items": {"type": "string"}, "minItems": 1, "description": "The URL(s) of the webpage(s) to visit."}, "goal": {"type": "string", "description": "The goal of the visit for webpage(s)."}}, "required": ["url", "goal"]}}
</tools>

If you decide to call tools, you MUST strictly follow the format below.
All tool calls must be wrapped inside <tool_calls> and <tool_calls_end>.
Inside this block, each individual tool call must be wrapped with <tool_call> and </tool_call>.

The exact required format is:
<tool_calls>
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
<tool_calls_end>"""

PROMPT_4_OPENSEEKER = ""

# ──────────────────────────────────────────────
# Prompt variant 5: OpenSeeker original format + your thinking/rules
# ──────────────────────────────────────────────
OPENSEEKER_PLUS_SYSTEM = OPENSEEKER_SYSTEM + """

Please reason about the problem deeply in your internal thought process.
During your reasoning process:
1. Decompose the complex question into smaller manageable sub-questions.
2. Evaluate what you know vs what you still need.
3. Before answering: verify ALL constraints are satisfied.

CRITICAL RULES:
- ALWAYS decompose the question first. List ALL sub-questions explicitly.
- NEVER answer after fewer than 3 rounds of searching for multi-hop questions.
- When you find a candidate answer, ALWAYS search to verify before committing.
- For bilingual questions, search in BOTH Chinese AND English.
- Answer with ONLY the requested information - no explanations, no extra context.
- If asked for a name, ALWAYS answer with the FULL name by default.
- **Answer language**: By default, answer in the SAME language as the question.
- **FINAL ANSWER FORMAT CHECK (MANDATORY)**: Before outputting <answer>, verify language and format."""

PROMPT_5_OPENSEEKER_PLUS = ""

# ──────────────────────────────────────────────
# Prompt variant 6: OpenSeeker base, project tool format, OpenSeeker tags
# (工具格式=项目, 标签格式=OpenSeeker, 约束=项目)
# vs Variant 5: only tool format differs → isolate tool format impact
# ──────────────────────────────────────────────
VARIANT6_SYSTEM = """You are a tool-augmented QA agent. Cleverly leverage appropriate tools to answer the user's question.

# Tools

You may call one or more functions to assist with the user query.
You are provided with function signatures within XML tags:
<tools>
{"name": "search", "description": "Perform web searches. Two engines: 'google' (best for English/international content) and 'bing' (best for Chinese content). Choose engine per query for best results.", "parameters": {"query": {"type": "array", "items": {"type": "string"}, "description": "Search queries. Include multiple complementary queries."}, "engine": {"type": "array", "items": {"type": "string", "enum": ["google", "bing"]}, "description": "Search engine per query, must match length of query array."}}}
{"name": "visit", "description": "Visit webpage(s) and return extracted content.", "parameters": {"url": {"type": "array", "items": {"type": "string"}}, "goal": {"type": "string", "description": "What information to extract."}}}
</tools>

If you decide to call tools, you MUST strictly follow the format below.
All tool calls must be wrapped inside <tool_calls> and <tool_calls_end>.
Inside this block, each individual tool call must be wrapped with <tool_call> and </tool_call>.

The exact required format is:
<tool_calls>
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
<tool_calls_end>

Please reason about the problem deeply in your internal thought process.
During your reasoning process:
1. Decompose the complex question into smaller manageable sub-questions.
2. Evaluate what you know vs what you still need.
3. Before answering: verify ALL constraints are satisfied.

CRITICAL RULES:
- ALWAYS decompose the question first. List ALL sub-questions explicitly.
- NEVER answer after fewer than 3 rounds of searching for multi-hop questions.
- When you find a candidate answer, ALWAYS search to verify before committing.
- For bilingual questions, search in BOTH Chinese AND English.
- Answer with ONLY the requested information - no explanations, no extra context.
- If asked for a name, ALWAYS answer with the FULL name by default.
- **Answer language**: By default, answer in the SAME language as the question.
- **FINAL ANSWER FORMAT CHECK (MANDATORY)**: Before outputting <answer>, verify language and format."""

PROMPT_6_VARIANT = ""

# ──────────────────────────────────────────────
# Prompt variant 7: OpenSeeker base, OpenSeeker tool format, project tags
# (工具格式=OpenSeeker, 标签格式=项目, 约束=项目)
# vs Variant 5: only tag format differs → isolate tag format impact
# ──────────────────────────────────────────────
VARIANT7_SYSTEM = """You are a tool-augmented QA agent. Cleverly leverage appropriate tools to answer the user's question.

# Tools

You may call one or more functions to assist with the user query.
You are provided with function signatures within XML tags:
<tools>
{"name": "search", "description": "Performs batched web searches: supply an array 'query'; the tool retrieves the top 10 results for each query in one call.", "parameters": {"type": "object", "properties": {"query": {"type": "array", "items": {"type": "string"}, "description": "Array of query strings. Include multiple complementary search queries in a single call."}}, "required": ["query"]}}
{"name": "visit", "description": "Parse webpage(s) and return the summary of the content according to the goal.", "parameters": {"type": "object", "properties": {"url": {"type": ["string", "array"], "items": {"type": "string"}, "minItems": 1, "description": "The URL(s) of the webpage(s) to visit."}, "goal": {"type": "string", "description": "The goal of the visit for webpage(s)."}}, "required": ["url", "goal"]}}
</tools>

When you need to gather information, output a tool call in this exact format:
<tool_call>
{"name": "search", "arguments": {"query": ["query1", "query2"]}}
</tool_call>

Please reason about the problem deeply in your internal thought process.
During your reasoning process:
1. Decompose the complex question into smaller manageable sub-questions.
2. Evaluate what you know vs what you still need.
3. Before answering: verify ALL constraints are satisfied.

CRITICAL RULES:
- ALWAYS decompose the question first. List ALL sub-questions explicitly.
- NEVER answer after fewer than 3 rounds of searching for multi-hop questions.
- When you find a candidate answer, ALWAYS search to verify before committing.
- For bilingual questions, search in BOTH Chinese AND English.
- Answer with ONLY the requested information - no explanations, no extra context.
- If asked for a name, ALWAYS answer with the FULL name by default.
- **Answer language**: By default, answer in the SAME language as the question.
- **FINAL ANSWER FORMAT CHECK (MANDATORY)**: Before outputting <answer>, verify language and format."""

PROMPT_7_VARIANT = ""

# ──────────────────────────────────────────────
# Test questions: 2-hop, 3-hop, 4-hop
# ──────────────────────────────────────────────
TEST_QUESTIONS = [
    {
        "hops": "2-hop",
        "question": "Who directed the film that won the Best Picture Oscar in the same year the Berlin Wall fell?",
    },
    # {
    #     "hops": "3-hop",
    #     "question": "哪位科学家提出了相对论，他的出生城市现在属于哪个国家，该国的首都是什么？",
    # },
    # {
    #     "hops": "4-hop",
    #     "question": (
    #         "The author of 'Harry Potter' was born in a town whose county "
    #         "shares its name with a river. What is the length of that river in kilometers?"
    #     ),
    # },
]

# ──────────────────────────────────────────────
# Experiment definitions
# ──────────────────────────────────────────────
EXPERIMENTS = [
    {
        "id": 1,
        "name": "Full prompt (current project)",
        "system": SYSTEM_PROMPT,
        "user_prefix": PROMPT_1_FULL,
    },
    {
        "id": 2,
        "name": "No thinking method instructions",
        "system": SYSTEM_PROMPT,
        "user_prefix": PROMPT_2_NO_THINKING,
    },
    {
        "id": 3,
        "name": "Minimal (tool format only)",
        "system": SYSTEM_PROMPT,
        "user_prefix": PROMPT_3_MINIMAL,
    },
    {
        "id": 4,
        "name": "OpenSeeker original prompt",
        "system": OPENSEEKER_SYSTEM,
        "user_prefix": PROMPT_4_OPENSEEKER,
    },
    {
        "id": 5,
        "name": "OpenSeeker + thinking/rules",
        "system": OPENSEEKER_PLUS_SYSTEM,
        "user_prefix": PROMPT_5_OPENSEEKER_PLUS,
    },
    {
        "id": 6,
        "name": "OpenSeeker (project tool format + OpenSeeker tags)",
        "system": VARIANT6_SYSTEM,
        "user_prefix": PROMPT_6_VARIANT,
    },
    {
        "id": 7,
        "name": "OpenSeeker (OpenSeeker tool format + project tags)",
        "system": VARIANT7_SYSTEM,
        "user_prefix": PROMPT_7_VARIANT,
    },
]


async def check_service():
    """Quick health check before running experiments."""
    import httpx
    health_url = BASE_URL.replace("/v1", "") + "/health"
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as c:
            r = await c.get(health_url)
            if r.status_code == 200:
                print("  Service health check: OK")
                return True
    except Exception:
        pass
    print("  !! vLLM service is NOT reachable. Start it on AutoDL first.")
    print(f"  Tried: {health_url}")
    return False


async def call_model(system_prompt: str, user_content: str) -> tuple[str, str]:
    """Call the model once and return (reasoning, content)."""
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.6,
        max_tokens=4096,
        extra_body={"enable_thinking": True},
    )
    msg = resp.choices[0].message
    reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
    content = msg.content or ""
    if content.strip().startswith("<!DOCTYPE") or content.strip().startswith("<html"):
        raise ConnectionError("vLLM service returned HTML (likely 404). Is the service running?")
    return reasoning, content


def print_separator(char: str = "=", width: int = 100):
    print(char * width)


def print_result(exp_id: int, exp_name: str, hops: str, question: str,
                 reasoning: str, content: str):
    print_separator("=")
    print(f"  Experiment {exp_id}: {exp_name}")
    print(f"  Question ({hops}): {question[:80]}...")
    print_separator("-")

    print("  [THINKING]")
    if reasoning:
        for line in reasoning.split("\n")[:30]:
            print(f"    {line}")
        if reasoning.count("\n") > 30:
            print(f"    ... ({reasoning.count(chr(10))} lines total, truncated)")
    else:
        print("    (empty)")

    print()
    print("  [CONTENT]")
    if content:
        for line in content.split("\n")[:40]:
            print(f"    {line}")
        if content.count("\n") > 40:
            print(f"    ... ({content.count(chr(10))} lines total, truncated)")
    else:
        print("    (empty)")

    print_separator("=")
    print()


async def main():
    print_separator("#")
    print("  OpenSeeker Prompt Ablation Test")
    print(f"  Model: {MODEL}")
    print(f"  Base URL: {BASE_URL}")
    print(f"  Questions: {len(TEST_QUESTIONS)}")
    print(f"  Experiments: {len(EXPERIMENTS)}")
    print_separator("#")
    print()

    if not await check_service():
        return

    for exp in EXPERIMENTS[5:]:
        for q in TEST_QUESTIONS:
            user_content = exp["user_prefix"] + q["question"]

            print(f">>> Running Experiment {exp['id']} ({exp['name']}) | {q['hops']}: {q['question'][:60]}...")

            try:
                reasoning, content = await call_model(exp["system"], user_content)
            except Exception as e:
                reasoning = ""
                content = f"ERROR: {e}"

            print_result(
                exp["id"], exp["name"], q["hops"], q["question"],
                reasoning, content,
            )


if __name__ == "__main__":
    asyncio.run(main())
