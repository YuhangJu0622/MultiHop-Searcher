# MultiHop-Searcher

一个基于多智能体协作的复杂问题搜索求解框架。通过 **Planner-Searcher-Reader** 三级架构，将复杂的多跳推理问题自动分解为可搜索的子问题，迭代检索并整合多源信息，最终生成精确答案。

## 架构概览

```
用户问题
   │
   ▼
┌──────────────────────────────────────────┐
│              SearchAgent                 │
│  (协调器：全局超时控制 + Fallback 兜底)    │
│                                          │
│  ┌──────────┐    ┌──────────┐            │
│  │ Planner  │───▶│ Searcher │            │
│  │ 问题拆解  │◀───│ 搜索求解  │            │
│  └──────────┘    └────┬─────┘            │
│       │               │                  │
│       │          ┌────▼─────┐            │
│       │          │  Reader  │            │
│       │          │ 网页抓取  │            │
│       │          │ 内容摘要  │            │
│       │          └──────────┘            │
│       │                                  │
│  ┌────▼─────┐                            │
│  │ Recorder │  ← 维护搜索图 + 推理记忆    │
│  └──────────┘                            │
└──────────────────────────────────────────┘
   │
   ▼
最终答案
```

### 核心组件

| 组件 | 职责 |
|------|------|
| **Planner** | 将复杂问题迭代拆解为单跳子问题，根据已收集信息判断是否需要继续搜索或生成最终答案 |
| **Searcher** | 调用搜索引擎获取网页结果，通过多轮工具调用（GoogleSearch + FinalAnswer）求解子问题 |
| **Reader** | 使用 Jina Reader 抓取网页内容，并通过 LLM 并发生成摘要，支持 WebPage 缓存 |
| **Recorder** | 维护搜索图（WebSearchGraph），记录子问题拆解关系、搜索结果和推理过程 |
| **TimeoutContext** | 全局看门狗超时机制，贯穿整条调用链，超时后自动触发 Fallback 回答 |

### 工作流程

1. **问题拆解**：Planner 分析用户问题，迭代式地拆解出一个单跳子问题
2. **搜索求解**：Searcher 生成搜索 query，调用 Google Search API 获取结果
3. **内容抓取与摘要**：Reader 通过 Jina Reader 并发抓取网页，LLM 并发生成内容摘要
4. **智能筛选**：根据搜索结果数量自适应选择 URL 筛选策略（全量保留 / 交叉排序 / LLM 语义筛选）
5. **迭代推理**：将子问题答案反馈给 Planner，继续拆解下一个子问题，直到信息充分
6. **最终回答**：Planner 综合所有已收集信息，生成结构化的最终答案

## 项目结构

```
MultiHop-Searcher/
├── searchagent/                # 核心代码
│   ├── agent/
│   │   └── agent.py            # AgentInterface 入口，组装各组件
│   ├── models/
│   │   ├── basellm.py          # LLM 基类（OpenAI 兼容 API）
│   │   ├── planner.py          # Planner 问题拆解
│   │   ├── searcher.py         # Searcher 搜索求解
│   │   ├── reader.py           # Reader 网页抓取 + LLM 摘要
│   │   ├── recorder.py         # Recorder 搜索图 + 记忆管理
│   │   └── searchagent.py      # SearchAgent 协调器
│   ├── prompt/                 # Prompt 模板（中英文双语）
│   │   ├── planner.py
│   │   ├── searcher.py
│   │   └── reader.py
│   ├── tools/
│   │   ├── websearch.py        # Google Serper Search API 封装
│   │   ├── jina_reader.py      # Jina AI Reader 网页抓取
│   │   ├── visitpage.py        # Serper Scrape API 备选抓取
│   │   └── final_answer.py     # FinalAnswer 工具定义
│   ├── utils/
│   │   ├── cache.py            # WebPageCache 网页缓存
│   │   ├── timeout_context.py  # 全局超时看门狗
│   │   ├── url_normalizer.py   # URL 标准化 + 去重
│   │   └── utils.py            # 通用工具函数
│   └── schema.py               # 数据模型 + 自定义异常
├── script/
│   ├── run_question.py         # 批量测试脚本（支持断点续跑）
│   └── run_question.sh         # Shell 启动脚本
├── .env.example                # 环境变量配置模板
├── requirements.txt
├── question.jsonl              # 测试问题集
└── validation.jsonl            # 验证问题集
```

## 快速开始

### 1. 环境配置

```bash
# 克隆项目
git clone https://github.com/your-username/MultiHop-Searcher.git
cd MultiHop-Searcher

# 创建虚拟环境（推荐 Python 3.10+）
conda create -n multihop python=3.10
conda activate multihop

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置 API Key

复制环境变量模板并填入你的密钥：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# Google Serper Search API Key (多个 key 用逗号分隔)
# 免费申请: https://serper.dev
GOOGLE_SUBSCRIPTION_KEY=your_serper_api_key

# LLM API 配置 (以阿里云 DashScope 为例)
LLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1/
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 代理设置 (可选)
HTTP_PROXY=http://127.0.0.1:7897
```

### 3. 运行

**单条问题测试：**

```bash
cd script
python run_question.py \
  --input_file "../question.jsonl" \
  --start_idx 0 --end_idx 1 \
  --google_subscription_key "$GOOGLE_SUBSCRIPTION_KEY" \
  --planner_model_name "qwen3-max" \
  --planner_api_base "$LLM_API_BASE" \
  --planner_api_key "$LLM_API_KEY" \
  --searcher_model_name "qwen3-max" \
  --searcher_api_base "$LLM_API_BASE" \
  --searcher_api_key "$LLM_API_KEY" \
  --reader_model_name "qwen3-max" \
  --reader_api_base "$LLM_API_BASE" \
  --reader_api_key "$LLM_API_KEY" \
  --max_time 480
```

**批量处理（使用 Shell 脚本）：**

```bash
# 先 source 环境变量
source ../.env

# 全量运行
bash run_question.sh

# 指定范围
bash run_question.sh --start 0 --end 10

# 断点续跑
bash run_question.sh --resume
```

### 主要参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--input_file` | 输入的 JSONL 文件路径 | `../question.jsonl` |
| `--google_subscription_key` | Serper API Key（多个用逗号分隔） | 必填 |
| `--planner_model_name` | Planner 使用的模型 | 必填 |
| `--searcher_model_name` | Searcher 使用的模型 | 必填 |
| `--reader_model_name` | Reader 使用的模型 | 必填 |
| `--max_time` | 每条问题的全局超时（秒） | `None` |
| `--max_new_tokens` | LLM 最大生成 token 数 | `8192` |
| `--google_search_topk` | 每次搜索返回的结果数 | `5` |
| `--resume` | 断点续跑模式 | `False` |
| `--start_idx` / `--end_idx` | 处理的问题 ID 范围 | 全部处理 |
| `--cache_dir` | 网页缓存目录 | 无 |

## 特性

- **迭代式多跳推理**：自动将复杂问题分解为多个单跳子问题，逐步求解
- **中英文双语支持**：自动检测问题语言，切换对应的 Prompt 模板
- **智能 URL 筛选**：根据候选数量自适应选择策略（全保留 / Round-Robin / LLM 语义筛选）
- **跨迭代 URL 去重**：避免重复抓取同一网页
- **全局超时看门狗**：`TimeoutContext` 贯穿调用链，超时自动触发 Fallback
- **Fallback 兜底机制**：主流程异常时利用已收集信息生成兜底回答
- **网页缓存**：基于 URL 哈希的本地文件缓存，避免重复抓取
- **并发处理**：搜索查询并发执行、网页抓取并发、LLM 摘要并发
- **断点续跑**：批量测试支持中断后从上次进度继续
- **搜索参数自动修复**：自动修正 LLM 生成的不合规工具调用参数

## 致谢

本项目的 Agent 架构设计参考了以下开源项目和思想：

- [ManuSearch](https://github.com/ManuSearch/ManuSearch) — 多智能体搜索框架
- [ReAct](https://arxiv.org/abs/2210.03629) — Reasoning + Acting 范式
- [Jina AI Reader](https://jina.ai/reader/) — 免费的网页内容提取 API
- [Serper.dev](https://serper.dev/) — Google Search API 服务

## License

MIT
