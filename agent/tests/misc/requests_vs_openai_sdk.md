# Requests 与 OpenAI SDK 调用大模型接口的区别

在使用 Python 调用大模型接口（尤其是通过企业网关）时，`requests` 和 `openai` SDK 的核心区别在于**抽象层级与控制权**。

## 1. URL 拼接方式（核心区别）

- `**requests`（绝对控制）：所见即所得。**
设置什么 URL 就请求什么 URL。非常适合严格校验路径的企业透传网关（如必须精确访问 `/default/passthrough`）。
- `**openai` SDK（强约定）：自动拼接。**
SDK 会将传入的 `base_url` 视为基础域名，强制在末尾拼接 `/chat/completions` 等标准路径。如果网关不支持该路径，极易导致 `404 Not Found`。（有时可通过在 URL 末尾加 `?` 来 Hack 绕过）。

## 2. 数据体（Payload）的构建

- `**requests`（极其灵活）：完全手写。**
直接构造 Python 字典（`dict`）。可以自由塞入网关所需的透传字段（如 `channel`）或特定模型的扩展参数（如 `enable_thinking`），无任何本地校验限制。
- `**openai` SDK（类型严格）：内置校验。**
基于严格的参数模型，默认只接受官方标准参数（比如 `model`, `messages`, `temperature` 等）。若需注入非官方字段（如网关参数），**必须**使用 `extra_body` 属性强行混入。

## 3. 返回结果的解析

- `**requests`（原生 JSON）：字典操作。**
返回原生 JSON 字典，需通过键值对层层提取（如 `data["choices"][0]["message"]["content"]`），容错性较弱，易触发 `KeyError`。
- `**openai` SDK（对象化）：面向对象操作。**
自动将 JSON 反序列化为 Pydantic 结构化对象，支持优雅的点（`.`）语法提取（如 `response.choices[0].message.content`），自带代码提示且开发体验极佳。

## 总结

- **常规开发**：首选 `**openai` SDK**，代码优雅、开发体验佳。
- **特殊/严格网关**：若网关对 URL 路径严格限制或自定义参数极多，使用 `**requests`** 是最稳妥的解决方案。

