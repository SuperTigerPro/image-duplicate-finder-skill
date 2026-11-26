# 客户端、模型与工具接入

## 权限与执行宿主

聊天模型理解意图，执行宿主管理路径、认证和进程。普通查重与本机 CopyNCE 在用户机器上运行；云端模型通过 SSH stdio 调用用户指定的服务器。根目录的 [SKILL.md](../SKILL.md) 是标准 Agent Skills 入口；其他宿主可使用 [通用提示词](universal-agent-prompt.md)。读取说明文件本身不会获得本机硬盘权限。

| 环境 | 接入方式 | 本机目录访问 |
| --- | --- | --- |
| 支持 Agent Skills，且有本地 Python 工具的宿主 | 导入完整仓库，使用宿主执行工具 | 受用户和宿主授权范围约束 |
| 自建本地 Agent，使用兼容 Chat Completions 的函数工具 | 注册本项目工具定义，由宿主运行适配器 | 模型不直接读盘，宿主执行 |
| 已有合适本地执行桥接的聊天客户端 | 使用通用提示词及实际桥接工具 | 以该客户端实际工具能力为准 |
| 没有本地工具的普通聊天 | 生成本机命令，由用户运行并反馈必要统计 | 助手不能直接扫描用户电脑 |
| 仅有云端代码执行环境 | 与用户电脑分离 | 不把本地路径当成云端已挂载目录 |

命令行和适配器不需要聊天模型 API Key；模型查重使用本机或自有服务器上的视觉模型。未声称已完成每种商业客户端的账号级集成验证。

## JSON 函数适配器

[函数定义](../adapters/chat-completions-tools.json) 使用 Chat Completions 的 `tools/function` 结构。其他工具协议需要宿主转换，不能直接作为 OpenAPI 或 MCP 配置导入。

宿主把函数 arguments 作为 stdin 交给 [tool_adapter.py](../scripts/tool_adapter.py)。必须由宿主先设置扫描白名单和报告根目录，模型参数不能扩大权限。以下均为占位路径，报告根目录应在所有 Git 工作树之外，每次 `output_dir` 使用其中尚不存在的新子目录。

~~~json
{
  "paths": ["/path/to/selected-gallery"],
  "mode": "exact",
  "output_dir": "/path/to/private-reports/exact-001",
  "recursive": true,
  "include_hidden": false
}
~~~

~~~bash
python3 scripts/tool_adapter.py \
  --allow-root "/path/to/selected-gallery" \
  --report-root "/path/to/private-reports" < arguments.json
~~~

`--allow-root` 可重复，只填用户明确选择的目录或文件，不能由模型自行设为整个磁盘。用户改变范围时由宿主更新授权。白名单是附加校验，不是操作系统沙箱；权限拒绝时不绕过限制。宿主应使用参数数组启动进程，不把 JSON 或路径拼进 shell 代码。

## 普通相似检测

`mode` 为 `exact`、`pixels`、`similar` 时，不接受模型专用字段。五个必填字段始终是 `paths`、`mode`、`output_dir`、`recursive`、`include_hidden`。仅 `similar` 可增加 `similarity_threshold`，要求 0～100 整数，默认 90：

~~~json
{
  "paths": ["/path/to/selected-gallery"],
  "mode": "similar",
  "similarity_threshold": 90,
  "output_dir": "/path/to/private-reports/similar-001",
  "recursive": true,
  "include_hidden": false
}
~~~

用户未给门槛就使用 90，不追问内部算法参数。100 需要完整分辨率严格像素核验。报告 `schema_version=2`，读取 `similar_pairs`，不能从空 `groups` 推断无相似图片。预览为本地文件，不自动发送给聊天模型。依赖及边界见 [相似检测说明](similarity.md)。

## 模型查重的宿主授权

`mode="copynce"` 必须由宿主启动时显式加 `--allow-model`，该授权包含准备必要环境和权重。模型参数不能自行开启它；需要离线限制时由宿主再加 `--model-offline`。

~~~bash
python3 scripts/tool_adapter.py \
  --allow-root "/path/to/selected-gallery" --report-root "/path/to/private-reports" \
  --allow-model < model-arguments.json
~~~

模型可提供以下字段，其余未知字段被拒绝：

| 字段 | 含义 |
| --- | --- |
| `backend` | `local` 或 `cloud`，默认 `local` |
| `model_threshold` | 原始余弦阈值 −1 至 1，默认 0.75；对应 CLI `--threshold`，不等于重复概率 |
| `whole_only` | 布尔，默认 `false`；对应 CLI `--whole-only` |
| `search_mode` | `auto`、`exact` 或 `indexed`；近似索引可能漏检 |
| `top_k` | 返回图对上限，1～20000，默认 100 |
| `remote_profile` | 仅 cloud，宿主已绑定的匿名 ID，不是服务器地址或配置文件路径 |
| `remote_directory` | 仅 cloud 且 `paths=[]`，必须在宿主授权的远端范围内 |

`similarity_threshold` 仅用于普通 similar；copynce 使用 `model_threshold`。所有模型请求仍包含五个必填字段。`local` 不接受 `remote_profile` 或 `remote_directory`。

### 查询既有远端图库

真实 SSH 配置由宿主在本机仓库外保存，再以 `--remote-profile ID=FILE` 绑定匿名 ID。远端白名单使用 `--allow-remote-root ID=DIR`，可重复；这两个参数和 `--allow-model` 都是宿主启动参数，不能来自模型生成的工具 JSON。相对目录以服务器第一个 `image_roots` 为基准；使用相对图库名可减少在工具参数中暴露完整服务器路径。

~~~bash
python3 scripts/tool_adapter.py \
  --allow-root "/path/to/approved-empty-local-root" \
  --report-root "/path/to/private-reports" --allow-model \
  --remote-profile "cloud1=/path/to/private-config/profile.json" \
  --allow-remote-root "cloud1=subset" < remote-arguments.json
~~~

当前宿主入口保留必填的本机 `--allow-root`，可指定预先存在的狭窄空目录；下面远端请求的 `paths=[]`，不会扫描该本机目录。

~~~json
{
  "paths": [],
  "mode": "copynce",
  "backend": "cloud",
  "remote_profile": "cloud1",
  "remote_directory": "subset/selected",
  "model_threshold": 0.75,
  "whole_only": false,
  "search_mode": "auto",
  "top_k": 100,
  "output_dir": "/path/to/private-reports/remote-001",
  "recursive": true,
  "include_hidden": false
}
~~~

远端查询要求 `include_hidden=false`，拒绝本机路径与远端目录混用。宿主白名单与服务器严格 `image_roots` 都必须通过；客户端默认拒绝未声明严格图库边界的服务。真实 IP、账号、密码、认证口令、密钥及 profile 路径都不能进入模型生成的工具参数。

### 本机图片发送至服务器

用户已授权本次选定图片传往指定服务器时，由宿主增加 `--allow-upload`，同时设置本机 `--allow-root` 和匿名 profile 绑定。工具 JSON 填本机 `paths`、`backend="cloud"`、`remote_profile`，省略 `remote_directory`。模型不能通过增加 JSON 字段授予上传权限；选择 cloud 本身也不构成上传授权。

适配器上传使用默认去元数据推理副本：Pillow 完整解码、EXIF 转向、8 位 RGB PNG，不包含 EXIF、ICC 或文本元数据；缺 Pillow 时失败，不会自动传原字节。可见图片内容仍可能敏感。适配器不提供模型参数来选择原文件上传；需要发送包含元数据的原字节时，应由本机宿主在用户明确授权后使用 CLI `--upload-original`。部署、预检和断线清理见 [CopyNCE 指南](copynce.md)。

## 返回值和隐私

宿主启动参数有效时，stdout 仅含一个 JSON 对象；过程日志位于对象内，退出码与 `exit_code` 一致：

~~~json
{
  "ok": true,
  "exit_code": 0,
  "report": {"complete": true, "summary": {}, "groups": [], "errors": []},
  "stdout": "人类可读日志",
  "stderr": ""
}
~~~

示例的 report 已简化；完整字段见 [报告格式](report-format.md)。退出码 1 仍可有部分 report，`ok=false`；2 时 `report=null`，不能复用旧报告冒充新结果；130 表示中断。未知字段、错误类型、超过 1 MiB 的 JSON 和越界路径会被拒绝。

copynce 返回 `schema_version=3` 的 `matches`、`complete`、`exhaustive` 和 `truncated`。适配器为模型 CLI 启用 `--summary-only`，但仍会读取完整私有报告并放进返回对象：**这不是自动脱敏返回。** 宿主必须在把结果交给云端聊天模型前按用户隐私偏好筛选，只回传必要统计或获准清单；完整路径和图片不应自动外发。报告不写连接 profile 中的地址或认证字段，图片完整路径仍可能包含账号名等隐私。含路径的报告、日志、配置、图片和特征均严禁发布到 GitHub。

## 没有本机工具时

可以采用 [通用提示词](universal-agent-prompt.md)，由助手提供本机命令，用户在自己电脑执行后反馈统计或脱敏报告。将说明上传到知识库不会授予本机目录权限；只有云端执行环境时，也不能假装已读取用户电脑。
