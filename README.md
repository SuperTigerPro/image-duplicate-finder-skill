# image-duplicate-finder-skill

[![Tests](https://github.com/SuperTigerPro/image-duplicate-finder-skill/actions/workflows/tests.yml/badge.svg)](https://github.com/SuperTigerPro/image-duplicate-finder-skill/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**查找完全重复、整图相似和跨图片的局部复用。可选普通查重、本地 CopyNCE 模型或自有服务器上的模型。原始图片始终只读。**

这是一个遵循 [Agent Skills](https://agentskills.io/specification) 目录规范的可复用 Skill，也可作为独立 Python 命令行工具使用。普通模式无需模型；模型模式可本地运行，也可以经 SSH 调用自己的服务器。

## 能做什么

- 同时扫描一个或多个目录，默认递归，支持中文、空格、不同文件名和大小写扩展名。
- **exact**：按文件大小预筛，再流式计算 SHA-256；比较原始文件字节，不因名字相同就判重。
- **pixels**：比较 EXIF 方向校正后的完整解码像素，可找出仅普通元数据、无损编码格式不同的副本。
- 动画和多页 TIFF 比较全部解码帧；动画时长、循环、透明通道和 ICC 配置也参与判定。
- 终端逐组列出完整路径，并可生成 Excel 可读 CSV、机器可读 JSON 和 Markdown 报告。
- 跳过符号链接，去除重叠扫描路径带来的重复计数，标注硬链接。
- 有坏图、权限或解码错误时继续处理其他图片，同时明确标记“结果不完整”。

- **similar（新增）**：静态整图相似检测，覆盖常见缩放、重新压缩和轻微亮度变化，用户只调相似度门槛，内部指标自动联动。

- **copynce**：使用官方 Descriptor 权重比较整图与重叠的局部裁块，输出疑似复用图对、余弦分数及两侧候选窗口，供人工核对。

### 怎么选

| 需要做什么 | 选择 | 示例 |
| --- | --- | --- |
| 找严格重复，或检查整图缩放、重压缩 | 普通查重 | “找出目录下文件内容完全相同的图片。” |
| 在本机排查裁剪、拼接、嵌套 | 本地模型查重 | “用本地模型核查这批图，标出疑似复用区域。” |
| 使用服务器上的图库或算力 | 云端模型查重 | “用服务器模型检查服务器上已有的这个图库。” |

只说“帮我查重”时，Agent 会展示这三个选择。需求明确时直接执行。云端只连接用户指定的服务器；本地图片须有本次授权才会发往目标服务器。见[模型操作指南](references/copynce.md)。

## 跨模型与客户端

可使用不同语言模型驱动 Agent，查重脚本无需聊天模型 API Key。除了标准 Skill，还提供 [通用 Agent / 自定义 GPT 提示词](references/universal-agent-prompt.md)、[客户端兼容说明](references/client-compatibility.md) 及 [Chat Completions 函数工具定义](adapters/chat-completions-tools.json)。

- 支持 Skill 和本地执行的 Agent（包括 DeepSeek Harness 等）可直接调用。
- GPT / DeepSeek 等模型的本地 Agent 宿主可通过 JSON 适配器接入，输入与输出目录由宿主白名单限制。
- 普通 ChatGPT / 自定义 GPT / DeepSeek 聊天如果没有本地文件工具，只能提供本机运行指引并解释结果，不能声称直接读到了硬盘。GPT Actions 需要 OpenAPI 桥接，工具 JSON 不能直接导入 Actions。

详细条件和示例见 [兼容矩阵与接入文档](references/client-compatibility.md)。图片无需发送给聊天模型；报告中的本地路径也应按用户隐私偏好脱敏。

## 作为 Skill 安装

入口是根目录的 [SKILL.md](SKILL.md)，实现脚本随仓库一起分发。

1. 克隆或下载**整个仓库**，不要只复制说明文件。
2. 在支持本地 Agent Skills 的客户端中导入这个目录；若客户端提供 GitHub Skill 导入功能，使用本仓库 URL。
3. 不同客户端的安装目录不同，请遵循该客户端说明。比如 Claude Code 可将完整目录放在用户级 Skills 目录：

~~~bash
git clone https://github.com/SuperTigerPro/image-duplicate-finder-skill.git \
  ~/.claude/skills/image-duplicate-finder-skill
~~~

对 Agent 说：

> 用图片查重 Skill 检查“/我的图片/实验结果”，递归查找文件完全相同的图片，给我完整重复分组和 CSV 报告，不要删除原图。

或：

> 比较“/图片A”和“/图片B”，忽略普通元数据，找出解码后像素完全相同的图片，只列清单。

Agent 未获知目标目录时会先询问，不会擅自扫描个人图库。这里的安装示例不会自动安装到其他客户端。

## 作为命令行工具使用

要求 **Python 3.10+**。macOS / Linux 通常使用 `python3`；Windows 可使用 `py -3` 或所选虚拟环境的 Python。

### 1. 克隆仓库

~~~bash
git clone https://github.com/SuperTigerPro/image-duplicate-finder-skill.git
cd image-duplicate-finder-skill
~~~

### 2. 文件完全相同（默认，零第三方依赖）

~~~bash
python3 scripts/find_duplicates.py "/你的/图片 文件夹" \
  --output-dir "./reports/exact-001"
~~~

也支持多个目录：

~~~bash
python3 scripts/find_duplicates.py "/图片目录A" "/图片目录B" \
  --mode exact --output-dir "./reports/exact-002"
~~~

### 3. 像素完全相同（可选 Pillow）

建议新建虚拟环境，避免更改系统 Python。在仓库根目录执行：

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-pixels.txt
.venv/bin/python scripts/find_duplicates.py "/你的/图片 文件夹" \
  --mode pixels --output-dir "./reports/pixels-001"
~~~

Windows PowerShell 示例（命令写成一行，无需激活虚拟环境）：

~~~powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-pixels.txt
.venv\Scripts\python.exe scripts/find_duplicates.py "D:\图片" --mode pixels --output-dir ".\reports\pixels-001"
~~~

**报告目录必须是新目录**；已存在时会拒绝，不会覆盖旧报告或原图。不要把上一次的输出目录反复用于新扫描。不传 `--output-dir` 时只打印清单。

### 4. 相似图检测：只调一个门槛

安装完整图像依赖后运行：

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-similar.txt
.venv/bin/python scripts/find_duplicates.py "/你的/图片 文件夹" \
  --mode similar --similarity-threshold 90 --output-dir "./reports/similar-001"
~~~

不写 --similarity-threshold 时默认就是 **90**。范围是 **0～100的整数**，日常建议 **80～95**：

| 门槛 | 感性理解 |
| --- | --- |
| 80 / 85 | 宽松一些，少漏候选，可能需要更多人工复核 |
| **90（默认推荐）** | 一般情况下直接使用 |
| 95 | 更严格，减少误报 |
| 100 | 原始分辨率严格像素一致，不用缩略图相似冒充相同 |

越高越严，越低越宽松。低于80会提示误报和计算量可能明显增加。用户不用选择 pHash、dHash、SSIM 或颜色权重，程序根据同一个门槛自动推导内部筛选条件；同一对图片的综合分数不随门槛变化。**分数不是相同像素百分比或同源概率**。

相似结果按图片对输出，附分数、完整路径、警告和本地并排预览（前12对）；其余完整清单保留在 CSV / JSON / Markdown。除严格像素核验的100分外，只称“疑似相似”，不能作为删除依据。颜色、透明度和宽高比也参与检查；低纹理图附加复核提示。

普通 similar 模式只支持普通静态单帧图，不启用裁剪、局部复用、任意旋转或语义检索。动画、多页或不支持的图会记录错误，不偷偷拿第一帧比较。达到内部资源安全上限时返回1并标记不完整，不静默截断。完整算法、边界与报告说明见 [相似检测说明](references/similarity.md)。

Windows 使用 .venv\Scripts\python.exe 执行相同参数。

### 5. 模型查重

~~~bash
python3 scripts/find_duplicates.py "/选定图库" --mode copynce --backend local --output-dir "/可写位置/模型报告"
~~~

首次运行会自动寻找兼容 Python，在用户缓存目录创建隔离环境，从官方来源下载并校验权重。CPU、CUDA 和 Apple Silicon MPS 共用约 87.5 MB 的同一份权重；依赖另占空间。实际推理自检后选择设备，显存不足先减小批量，再回退本机 CPU，不改系统驱动，也不自动切换云端。

默认比较整图和固定局部裁块，`--whole-only` 只比较整图。**CopyNCE 是实验性候选筛查**；`--threshold 0.75` 是需要领域校准的起始余弦门槛，不是75%的重复概率。高分和候选窗口不能替代严格重复核验或人工来源审查。官方公开基础权重的范围及局限见 [CopyNCE 上游说明](https://github.com/eddielyc/CopyNCE#fine-tuning)。

模型报告强制写在仓库及所有 Git 工作树之外。`--summary-only` 只向终端输出统计，完整路径保留在私有报告。本机离线、服务器部署、严格图库边界、默认去元数据传输、取消与重连清理见[模型操作指南](references/copynce.md)。

### 常用参数

| 参数 | 行为 |
| --- | --- |
| `--mode exact` | 默认，比较原始文件 SHA-256 |
| `--mode pixels` | 比较完整解码像素，需要 Pillow |
| `--mode similar` | 静态整图相似检测，需要 Pillow + NumPy |
| `--mode copynce` | 通过 `--backend local` 或 `--backend cloud` 指定模型运行位置 |
| `--similarity-threshold 90` | similar唯一算法调参项，0～100整数，默认90，日常80～95；越高越严 |
| `--no-recursive` | 不扫描子文件夹 |
| `--include-hidden` | 包含隐藏项，仍跳过符号链接 |
| `--extensions jpg,jpeg,png` | 自定义扩展名集合，不区分大小写 |
| `--extensions '*'` | 比较全部文件，含无扩展名和非图片；星号需加引号 |
| `--output-dir PATH` | 在新目录生成三种报告 |
| `--max-pixels N` | pixels 单帧像素上限，默认 40,000,000 |
| `--max-frames N` | pixels 每文件帧数上限，默认 200 |
| `--max-total-pixels N` | pixels 每文件所有帧累计上限，默认 100,000,000 |
| `--help` / `--version` | 查看完整帮助 / 版本 |

默认扩展名包含常见的 JPG、PNG、GIF、WebP、BMP、TIFF、ICO、PPM、AVIF、HEIC、SVG、PSD 和部分相机 RAW 等。完整集合见 [DEFAULT_EXTENSIONS](scripts/find_duplicates.py)。只根据扩展名筛选；无扩展名图片需显式选择 `--extensions '*'`，同时注意它会包含其他文件。

## 如何理解结果

示例（路径为演示，不是用户数据）：

~~~text
图片查重 | exact | 完整
候选图片 4；重复组 1；组内图片 3；额外副本 2

G0001 | 3 个路径 | <SHA-256>
  [参考] /demo/a.png (... B)
  [重复] /demo/backup/a-copy.png (... B)
  [重复] /demo/export/renamed.png (... B)
~~~

- **重复组**：相同指纹的一组图片路径，至少 2 个。
- **组内图片**：所有重复组中的路径总数，包含每组第一张。
- **额外副本**：每组路径数量减 1 后求和。不是“重复组内所有文件数”。
- **参考**：按完整路径排序的第一项，仅便于展示，不推荐删除其他项。
- **逻辑字节**：按额外路径文件长度相加，**不等于可释放磁盘空间**。硬链接、APFS 写时复制和压缩文件系统都会影响实际占用。

新报告目录中会生成：

| 报告 | 用途 |
| --- | --- |
| `duplicates.csv` | UTF-8 BOM；exact/pixels每个重复路径一行；similar 和 copynce 每个候选图片对一行，包含分数、门槛及两个完整路径 |
| `duplicates.json` | 完整扫描范围、选项、统计、分组、跳过计数及错误；数据结构见 [报告格式](references/report-format.md) |
| `duplicates.md` | 易读的完整重复分组/相似图片对、警告与错误说明 |
| `similarity-preview.png` | 原图在本地可读时展示前 12 对预览；模型模式带候选区域框，宿主缺少 Pillow 时可生成 HTML 预览 |

### 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 指定范围和过滤条件内扫描完成；没有重复也返回 0 |
| 1 | 存在扫描、读取、解码或安全限制错误；报告仍保留成功处理的分组，`complete=false` |
| 2 | 参数、输入、依赖或输出错误；输出故障可能留下不完整的新报告目录 |
| 130 | 用户中断，不能视为完成扫描 |

自动化调用必须检查退出码及 JSON 中的 `complete` / `errors`，不能只看有没有分组。这里的“完整”以记录的扫描范围为准，不是文件系统快照。

模型模式还需分别核对 `files_complete`、`exhaustive`、`truncated`：退出0只表示文件处理成功，近似检索或 top-k 截断仍可能省略候选。只有 `all_matches_reported=true` 才是本次固定窗口和阈值下的完整返回清单。

## 判定边界与安全

1. **exact、pixels 与 similar 不是一回事。** 同一 PNG 添加说明信息后文件字节可能不同，但像素仍相同。重新保存的 JPEG 即使肉眼相似，也常因有损压缩产生像素差异。
2. pixels 对普通 8 位灰度、调色板、RGB、RGBA 图归一化到 RGBA。整数、浮点和 CMYK 等模式保留原模式和原始解码数据，跨这些模式不保证归为同组。
3. pixels 不做 ICC 色彩管理转换；ICC 配置不同保守地视为不同。它比较的是本机 Pillow 解码结果，不是主观视觉相似度。
4. 多通道 16 位 PNG、解码可能降低精度的高位深 TIFF，以及多分辨率 ICO 会被像素模式拒绝并记录错误，以免误报。科学图像、专有格式和未经验证的高位深解码器建议使用 exact。
5. exact 不验证文件能否解码：任何符合扩展名条件、字节相同的文件都能被找出，包括损坏图片的相同副本。pixels 会报告损坏或不支持的图片。
6. HEIC / HEIF、RAW、SVG、JXL 等是否支持像素解码取决于 Pillow 和已安装的解码器；本项目不会自动安装插件，也不会静默忽略解码失败。精确字节模式不受解码器限制。
7. 隐藏项默认跳过；显式选定的隐藏根目录/文件不受该项过滤。符号链接始终不跟随；直接传入符号链接会报参数错误。重复/重叠输入路径只计算一次，硬链接的不同路径仍会列出。
8. 检查文件的设备、inode、大小及修改/变更时间，尽量发现扫描过程中的修改。它不是事务型快照；请暂停图库同步或编辑后扫描。
9. 报告只写入新目录，模型模式另有环境、权重和特征缓存。操作系统可能因读取而更新访问时间（atime）。如果磁盘写满，输出可能不完整；修复后用新目录重试。
10. 普通查重和本地模型均在本机运行。云端上传须明确目的服务器和授权范围。真实连接配置、图片、报告、预览、缓存及日志均禁止提交到 GitHub。

## 测试与开发

~~~bash
# 普通图像模式（模型/stdio专项依赖缺失时会明确跳过）
.venv/bin/python -m pip install -r requirements-similar.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q scripts tests
~~~

没有Pillow/NumPy时，对应图像专项会跳过，这不算完整验证。测试覆盖严格查重、相似变体、全部101个整数门槛的一致性、严格100、BK-tree与暴力搜索对照、SSIM公式、颜色/透明度、非传递关系、资源上限、EXIF、动画/多页边界、损坏图、权限错误、中文路径和原图只读。

[GitHub Actions](.github/workflows/tests.yml) 配置了 Linux / macOS / Windows，以及 Python 3.10 / 3.12 / 3.14 的组合；实际执行状态见顶部徽章及 Actions 页面。本地验证记录见 [TESTING.md](TESTING.md)。

完整模型回归使用独立的 Python 3.11 环境，安装 [requirements-copynce.txt](requirements-copynce.txt)、`mcp>=1.20,<2` 和可选的 `faiss-cpu`。CI 的模型专项不下载权重、不连接真实服务器，不需要服务器资料或 Actions secrets。

### 公开提交与打包

Git 克隆不会自动启用 hooks。维护者可在自己的开发副本运行 `git config --local core.hooksPath .githooks`；有既有 hooks 时先合并检查流程。可将本机敏感标识逐行保存在仓库外私有文件，用 `IMAGE_SKILL_PRIVATE_TERMS_FILE` 引用；禁止将该文件或真实配置加入源码。

检查器对未知文件、敏感地址/凭据/私有路径及缺失必要资源拒绝放行，错误不回显敏感值。提交前用 `--staged`，推送钩子检查全部待推送提交及标签消息，不能只检查当前工作树。开发副本可以先生成经过检查的审阅包：

~~~bash
python3 scripts/check_public_files.py --working-tree
python3 scripts/check_public_files.py --working-tree --write-archive "/path/to/private-output/skill.zip"
python3 scripts/check_public_files.py --working-tree --archive "/path/to/private-output/skill.zip"
~~~

正式提交后可用 `--commit HEAD` 替换 `--working-tree`。打包只包含源码白名单，复核要求ZIP与对应源码逐字节一致。真实IP、登录用户名、口令、密钥、profile、图库、缓存、报告和日志均不得进入 GitHub、Actions、Issue、PR或发布附件。正常查重流程不执行 Git 提交或上传；`.gitignore`和GitHub秘密扫描只是补充防护。

## 目录

~~~text
image-duplicate-finder-skill/
├── SKILL.md                      # Agent 入口与安全执行流程
├── scripts/find_duplicates.py    # 统一只读命令行
├── scripts/image_similarity.py   # 单门槛相似检测
├── scripts/tool_adapter.py       # 带宿主路径白名单的JSON入口
├── scripts/copynce_*.py          # 准备、推理、检索、报告、SSH与隔离任务
├── scripts/check_public_files.py # 源码/历史/ZIP防泄漏检查与白名单打包
├── start-mcp                    # SSH stdio服务，无公网监听
├── agents/openai.yaml           # Skill界面元数据
├── adapters/chat-completions-tools.json
├── tests/                       # 临时合成样本与安全/契约回归
├── references/similarity.md      # 单门槛、评分与检测范围
├── references/copynce.md         # 本地和云端模型指南
├── references/report-format.md   # 三种报告结构
├── requirements-pixels.txt       # 仅Pillow
├── requirements-similar.txt      # Pillow + NumPy
├── requirements-copynce.txt      # 固定的最小推理依赖
├── .github/workflows/tests.yml  # 跨平台 CI
├── TESTING.md
└── LICENSE
~~~

## 许可证

原有代码使用 [MIT](LICENSE)。CopyNCE 移植代码保留 [Apache-2.0 许可](third_party/CopyNCE-LICENSE)与来源署名；权重从[作者发布页](https://github.com/eddielyc/CopyNCE#models)获取，不随 Skill 包重新上传。
