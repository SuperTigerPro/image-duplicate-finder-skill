# 客户端、模型与工具接入

## 兼容原则

本项目的图像比对逻辑是纯本地 Python，不依赖 GPT、DeepSeek 或其他模型 SDK。模型负责理解意图和展示结果，宿主负责文件权限与执行。根目录的 [SKILL.md](../SKILL.md) 提供标准 Agent Skills 入口；无该规范支持时使用 [通用提示词](universal-agent-prompt.md)。

**模型兼容不等于客户端天然有本地硬盘权限。** 不同客户端能力不同，不能承诺普通聊天窗口导入说明后就可以直接扫描电脑。

| 环境 | 接入方式 | 能否自动扫描本地目录 |
| --- | --- | --- |
| 支持标准 Agent Skills 且有本地 Python 工具的宿主 | 导入整个仓库 | 可以，在用户/宿主授权范围内 |
| DeepSeek Harness 等本地 Agent 宿主 | 导入 Skill，使用宿主文件/命令工具 | 可以；取决于宿主权限，不依赖具体模型 |
| GPT / DeepSeek / 其他 OpenAI-compatible Chat Completions 模型的自建 Agent | 注册函数定义，由本地宿主执行 JSON 适配器 | 可以；模型本身不会读盘 |
| ChatGPT 客户端或自定义 GPT，已经接入合适的本地执行工具 | 使用通用提示词，并由实际工具桥接本地脚本 | 有条件可以；本项目不假定或自动配置该桥接 |
| ChatGPT / GPT / DeepSeek 普通聊天，没有本地工具 | 粘贴通用提示词；让助手生成本地命令，用户执行后反馈脱敏结果 | 需要用户在本机执行，不能假装已自动扫描 |
| 只有云端代码解释器的聊天环境 | 不是本地电脑；默认不把图库上传到云端 | 不支持直接访问本地路径 |

无需访问任何模型 API 即可运行命令行、适配器和全部测试。没有声称已在每一种商业客户端上完成账号级集成测试；可验证的内容是标准文件结构、JSON 工具契约、本地适配器以及跨平台执行。

## 通用函数工具入口

[函数定义](../adapters/chat-completions-tools.json) 使用兼容 Chat Completions 的 tools/function 格式，可由支持此格式的 OpenAI / DeepSeek 等 Agent 宿主加载。OpenAI Responses API、GPT Actions 或专有工具协议须由宿主转换成对应格式；不能将这份 JSON 直接当作 OpenAPI 或 MCP 服务。

模型输出的函数 arguments 由宿主交给 [tool_adapter.py](../scripts/tool_adapter.py) 的 stdin。适配器不启动服务、不调用模型、不执行任意 shell 命令。宿主启动时必须指定输入白名单和报告根目录，模型参数不能扩大它们。

示例参数（只作演示；使用前替换为获准路径）：

~~~json
{
  "paths": ["/data/用户选定的图片"],
  "mode": "exact",
  "output_dir": "/work/reports/run-001",
  "recursive": true,
  "include_hidden": false
}
~~~

本地宿主启动示例：

~~~bash
python3 scripts/tool_adapter.py \
  --allow-root "/data/用户选定的图片" \
  --report-root "/work/reports" < arguments.json
~~~

不要让模型自行决定 --allow-root 为整个磁盘。用户选择变化时，由宿主重新取得用户授权并更新启动参数。白名单是额外校验，不是操作系统沙箱，仍应使用宿主的最小权限执行策略；遇到拒绝不可绕过。

### 相似检测的可选单一参数

mode 可取 exact、pixels、similar。旧的五个必填字段保持不变；仅similar允许额外的 similarity_threshold 整数字段，0～100，默认90。不能在exact/pixels里传入并被静默忽略，也不能传入字符串、布尔或小数。

~~~json
{
  "paths": ["/data/用户选定的图片"],
  "mode": "similar",
  "similarity_threshold": 90,
  "output_dir": "/work/reports/similar-001",
  "recursive": true,
  "include_hidden": false
}
~~~

用户未提门槛就直接用90，不追问内部算法指标。较低值更宽松、较高值更严；100严格核验原始解码像素。相似报告schema_version=2，要读取similar_pairs而不是空groups；preview指向报告目录中的本地缩略预览。不要自动将预览或完整路径发送给云端模型。环境另需 [相似依赖](../requirements-similar.txt)，逻辑和限制见 [相似检测说明](similarity.md)。

### 返回值

在宿主启动参数有效时，stdout 是一个 JSON 对象，不混入面向人类的日志。进程退出码与 exit_code 一致：

~~~json
{
  "ok": true,
  "exit_code": 0,
  "report": {"complete": true, "summary": {}, "groups": [], "errors": []},
  "stdout": "人类可读日志",
  "stderr": ""
}
~~~

上例 report 简化了字段，完整格式见 [报告格式](report-format.md)。退出码 1 时仍返回部分 report，ok 为 false；2 时 report 为 null，不能把已有报告当作本次结果；130 表示中断。非法/超出 1 MiB 的请求、未知字段、错误类型和越界路径会被拒绝。

宿主应使用参数数组启动进程，不能把模型 arguments 拼成 shell 字符串。读取 JSON 后再按其 SDK 将结果回传模型。报告含本地路径，是否将完整路径发送给云端模型由用户和宿主的隐私策略决定；可以只回传统计及经用户认可的脱敏清单。图片字节始终不需要发送给模型。

## ChatGPT / 自定义 GPT 的最小使用方法

1. 复制 [通用提示词](universal-agent-prompt.md) 到 Instructions 或对话中，并提供项目仓库链接。
2. 如果宿主确有本地执行工具，明确授权扫描根目录及新报告位置，再调用本地脚本/适配器。
3. 没有该工具时，让助手生成本机命令，在本机运行；只反馈必要统计或脱敏报告。
4. 不要把“上传源代码文件到 GPT Knowledge”误认为能授予读取本地文件夹的权限。

这种分层方式让同一套扫描器适配不同模型，同时不虚构任何客户端的权限或集成能力。
