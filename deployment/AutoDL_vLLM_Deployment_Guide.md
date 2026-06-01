# 📝 AutoDL 平台大模型部署经验与避坑指南

本文档记录了在 AutoDL 平台上使用 vLLM 部署大模型（如 Qwen 系列）的完整经验，重点总结了环境配置原理、目录结构以及网络避坑策略。

## 一、 AutoDL 环境依赖与底层逻辑

在云算力平台上部署大模型，理解其预装环境的底层逻辑是排查问题的关键。

### 1. GPU 驱动与 CUDA 的关系及版本选择

*   **驱动版本 (Driver Version)**：如 `595.71.05`，这是宿主机安装的底层显卡驱动。它决定了机器能支持的**最高 CUDA 版本**（如 CUDA 13.2）。驱动具有向下兼容性。
*   **CUDA Toolkit vs. CUDA Runtime (核心区别)**：
    *   **CUDA Toolkit (系统级)**：安装在 `/usr/local/cuda`。包含 `nvcc` 编译器，主要用于**从源码编译** C++ 扩展库（如自定义的 FlashAttention 算子）。
    *   **CUDA Runtime (Python 包级)**：随 PyTorch (`pip install torch`) 动态下载的运行库（如 `libcudart.so`）。**vLLM 运行时主要依赖的是这部分库**。
*   **如何选择镜像？**
    *   **避开最新版 PyTorch**（如 2.8.0），因为 vLLM 等高度优化的推理框架对最新版 PyTorch 的底层 C++ 算子兼容往往有滞后。
    *   **推荐黄金组合**：选择生态最成熟的 `PyTorch 2.4.x` 或 `2.5.x` + `Python 3.10/3.11/3.12` + `CUDA 12.1/12.4`。在此环境下，`pip install vllm` 可以直接拉取预编译的 wheel 包，实现“秒安装”，无需漫长的本地编译。

### 2. AutoDL 预装库的位置与环境变量

AutoDL 的镜像环境结构有其固定模式：

*   **Python/PyTorch 位置**：默认安装在 Miniconda 的 `base` 环境中，路径为 `/root/miniconda3/`。
*   **环境变量的“隐身”**：终端提示符可能不显示 `(base)`，但 AutoDL 已将 `/root/miniconda3/bin` 注入到 `$PATH` 的最前端。因此，直接敲击 `python` 或 `pip` 调用的就是 `base` 环境，**无需手动 `conda activate base`**。

### 3. 磁盘规划：系统盘 vs. 数据盘 (致命坑点)

*   **系统盘 (`/`)**：通常只有 30GB。如果使用默认配置，HuggingFace 或 ModelScope 会将模型缓存下载到 `~/.cache/`（即系统盘），极易导致磁盘爆满、机器卡死。
*   **数据盘 (`/root/autodl-tmp/`)**：容量大（50GB起步，可扩容）。
*   **经验法则**：任何大文件下载前，**必须**通过环境变量或代码参数，将缓存路径重定向到数据盘。

### 4. 安装 vLLM

在确认上述环境后，安装非常简单：
```bash
# 若默认阿里云源下载慢，可临时切换清华源
pip install vllm -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 二、 基于 vLLM 的模型下载与部署流程

### 1. 模型下载：放弃 HuggingFace，拥抱 ModelScope

**惨痛教训**：在国内云服务器上，即使配置了 `HF_ENDPOINT` 镜像站、使用了 AutoDL 的“学术加速”代理，甚至使用了 `hf download` 和 `huggingface-cli`，HuggingFace 的下载依然极易出现软链接卡死、并发阻塞（一直卡在 `Fetching files: 0%`）或极低网速（<1MB/s）。

**最佳实践**：部署开源模型（尤其是 Qwen、DeepSeek 等国产模型）时，**直接使用阿里云的 ModelScope（魔搭社区）**。走国内/内网骨干网，速度可达几百 MB/s，且无需配置任何代理。

**下载代码模板**：
```python
# download_model.py
import os
from modelscope import snapshot_download

# 强制指定下载到数据盘！
cache_dir = "/root/autodl-tmp/modelscope_cache"
os.makedirs(cache_dir, exist_ok=True)

# 下载模型并获取绝对路径
model_dir = snapshot_download('qwen/Qwen2.5-0.5B-Instruct', cache_dir=cache_dir)
print(f"模型路径: {model_dir}")
```

### 2. 启动 vLLM 服务

获取到模型的绝对路径后，使用 `vllm serve` 启动服务。

*   **后台运行**：切勿直接在前台运行，终端一关服务即停。必须使用 `screen -S vllm_server` 创建后台虚拟终端。
*   **端口映射**：为了让外部机器能访问 API，需将端口设置为 **`6006`**，配合 AutoDL 控制台的“自定义服务”功能暴露公网 URL。

**启动命令示例**：
```bash
vllm serve "/root/autodl-tmp/modelscope_cache/qwen/Qwen2.5-0.5B-Instruct" \
    --host 0.0.0.0 \
    --port 6006 \
    --max-model-len 4096 \
    --trust-remote-code
```
*(注：观察启动日志，若出现 `Using FlashAttention version 2` 和 `Uvicorn running on http://0.0.0.0:6006`，即代表服务完美启动。)*

### 3. 远程 API 调用

在本地或其他远程机器上，将 AutoDL 提供的“自定义服务”公网地址作为 `base_url`，即可实现与 OpenAI 完全兼容的 API 调用：

```bash
# 替换 URL 和 model 路径
curl https://uXXXXX-XXXX.autodl.io/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "/root/autodl-tmp/modelscope_cache/qwen/Qwen2.5-0.5B-Instruct",
        "messages": [{"role": "user", "content": "你好"}]
    }'
```

---
**总结**：在 AutoDL 部署大模型，核心在于**选对底层镜像（避开最新版）、管好磁盘路径（用数据盘）、绕过网络深坑（用魔搭社区）**。掌握这三点，部署流程将无比丝滑。