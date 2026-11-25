# 测试记录

测试日期：2026-11-25。全部样本由测试代码生成；没有扫描或上传个人图片。

## 本地环境

| 环境 | 结果 |
| --- | --- |
| macOS / arm64，Python 3.12.14 + Pillow 12.3.0 | 64 项测试（含 Skill 结构、文档、跨平台回归与通用工具适配）全部通过，无跳过 |
| macOS / arm64，Python 3.10.12 + Pillow 10.4.0 | 64 项测试（含 Skill 结构、文档、跨平台回归与通用工具适配）全部通过，无跳过 |
| 隔离 Python（禁用 site-packages） | exact 可用；pixels 提供明确的依赖缺失提示并返回 2 |
| Python 语法编译 | 通过 |

完整测试命令：

~~~bash
python3 -m pip install -r requirements-pixels.txt
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
~~~

上述安装命令应在虚拟环境中运行。安装依赖请优先选择最新兼容版本；最低版本测试用于验证兼容性，不建议因此降级现有 Pillow。

## 端到端报告验证

在被 Git 忽略的本地临时测试目录中生成 8 个图片路径：

- 原始 PNG 与子目录中重命名的字节副本。
- 添加普通文本元数据的 PNG。
- 解码像素相同的 BMP。
- 修改一个像素的 PNG。
- 第一帧相同、第二帧不同的两张 GIF。
- 一张隐藏图片，默认跳过。

使用真实命令行分别运行两种模式，并核对 JSON、CSV 和 Markdown：

| 项目 | exact | pixels |
| --- | --- | --- |
| 候选图片 | 7 | 7 |
| 隐藏项跳过 | 1 | 1 |
| 重复组 | 1 | 1 |
| 组内图片 | 2 | 4 |
| 额外副本 | 1 | 3 |
| 错误 | 0 | 0 |
| 退出码 | 0 | 0 |

结果符合预期：元数据变体和 BMP 仅在 pixels 中归组；单像素差异图和后续帧不同的 GIF 没有被误判。CSV 成员数与 JSON 一致；对全部合成原图的 SHA-256、文件名和修改时间进行前后对照，确认未改动。

## 重点回归场景

- 中文/空格路径、不同文件名、同名不同内容、大小写扩展名。
- 多目录、重叠目录、单文件输入、递归与非递归、隐藏项。
- 符号链接、目录循环、硬链接及其逻辑字节说明。
- 流式读取跨多个块的大文件，不将全文件读入内存。
- 普通元数据差异、调色板归一化、EXIF 方向、透明度、ICC 差异。
- 动画全部帧、帧时长和循环；多页 TIFF 全部页。
- 高位深数据不做有损归一化；拒绝可能降精度的多通道 16 位 PNG 和多分辨率 ICO。
- 损坏图、缺少解码器、权限错误，保留部分结果并标记不完整。
- 扫描中修改文件、哈希后修改文件均不能留下已失效的重复分组。
- CSV / JSON / Markdown 一致性、恶意控制字符和 HTML 文件名。
- 报告目录已存在、源图片被误选为输出位置、报告写入失败。
- 缺少 Pillow、无第三方包的 exact 运行、无输出目录的纯终端模式。
- Windows stat/fstat 纳秒精度差异模拟、同大小文件修改检测，以及 ASCII/隔离解释器中的 UTF-8 输出。

## 通用模型工具入口验证

[JSON 适配器](scripts/tool_adapter.py) 与 [工具定义](adapters/chat-completions-tools.json) 的 10 项测试覆盖：函数契约、纯 JSON 输入输出、两种查重模式、宿主输入白名单、报告位置限制、已有报告不复用、严格参数类型、部分错误结果、非法 JSON 与 1 MiB 请求上限。没有使用模型 API Key，也没有声称已在每一种商业聊天客户端完成账号级集成。宿主能力与接入条件见 [兼容说明](references/client-compatibility.md)。

## 持续集成

[工作流](.github/workflows/tests.yml) 配置 Linux（Python 3.10、3.12、3.14）、macOS（3.12）和 Windows（3.12）。配置不等于执行结果；发布后的实际记录请查看 [GitHub Actions](https://github.com/SuperTigerPro/image-duplicate-finder-skill/actions)。

Windows 对部分特殊文件名有限制，对应测试按平台明确跳过；其他平台是否全部通过以实际工作流日志为准。
