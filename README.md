# image-duplicate-finder-skill

[![Tests](https://github.com/SuperTigerPro/image-duplicate-finder-skill/actions/workflows/tests.yml/badge.svg)](https://github.com/SuperTigerPro/image-duplicate-finder-skill/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**在本地图片文件夹中找出并分组罗列完全相同的图片。只读，不删图，不上传。**

这是一个遵循 [Agent Skills](https://agentskills.io/specification) 目录规范的可复用 Skill，也可作为独立 Python 命令行工具使用。无需网站账号、API Key 或云端识图服务。

## 能做什么

- 同时扫描一个或多个目录，默认递归，支持中文、空格、不同文件名和大小写扩展名。
- **exact**：按文件大小预筛，再流式计算 SHA-256；比较原始文件字节，不因名字相同就判重。
- **pixels**：比较 EXIF 方向校正后的完整解码像素，可找出仅普通元数据、无损编码格式不同的副本。
- 动画和多页 TIFF 比较全部解码帧；动画时长、循环、透明通道和 ICC 配置也参与判定。
- 终端逐组列出完整路径，并可生成 Excel 可读 CSV、机器可读 JSON 和 Markdown 报告。
- 跳过符号链接，去除重叠扫描路径带来的重复计数，标注硬链接。
- 有坏图、权限或解码错误时继续处理其他图片，同时明确标记“结果不完整”。

**不做**相似图检索、人脸聚类、裁剪/缩放图比对，也不提供自动删除操作。

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

### 常用参数

| 参数 | 行为 |
| --- | --- |
| `--mode exact` | 默认，比较原始文件 SHA-256 |
| `--mode pixels` | 比较完整解码像素，需要 Pillow |
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
| `duplicates.csv` | UTF-8 BOM，便于 Excel 打开；每个重复路径一行，包含组号、角色、指纹、字节数、完整路径及硬链接信息 |
| `duplicates.json` | 完整扫描范围、选项、统计、分组、跳过计数及错误；数据结构见 [报告格式](references/report-format.md) |
| `duplicates.md` | 易读的完整重复分组与错误说明 |

### 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 指定范围和过滤条件内扫描完成；没有重复也返回 0 |
| 1 | 存在扫描、读取、解码或安全限制错误；报告仍保留成功处理的分组，`complete=false` |
| 2 | 参数、输入、依赖或输出错误；输出故障可能留下不完整的新报告目录 |
| 130 | 用户中断，不能视为完成扫描 |

自动化调用必须检查退出码及 JSON 中的 `complete` / `errors`，不能只看有没有分组。这里的“完整”以记录的扫描范围为准，不是文件系统快照。

## 判定边界与安全

1. **exact 与 pixels 不是一回事。** 同一 PNG 添加说明信息后文件字节可能不同，但像素仍相同。重新保存的 JPEG 即使肉眼相似，也常因有损压缩产生像素差异。
2. pixels 对普通 8 位灰度、调色板、RGB、RGBA 图归一化到 RGBA。整数、浮点和 CMYK 等模式保留原模式和原始解码数据，跨这些模式不保证归为同组。
3. pixels 不做 ICC 色彩管理转换；ICC 配置不同保守地视为不同。它比较的是本机 Pillow 解码结果，不是主观视觉相似度。
4. 多通道 16 位 PNG、解码可能降低精度的高位深 TIFF，以及多分辨率 ICO 会被像素模式拒绝并记录错误，以免误报。科学图像、专有格式和未经验证的高位深解码器建议使用 exact。
5. exact 不验证文件能否解码：任何符合扩展名条件、字节相同的文件都能被找出，包括损坏图片的相同副本。pixels 会报告损坏或不支持的图片。
6. HEIC / HEIF、RAW、SVG、JXL 等是否支持像素解码取决于 Pillow 和已安装的解码器；本项目不会自动安装插件，也不会静默忽略解码失败。精确字节模式不受解码器限制。
7. 隐藏项默认跳过；显式选定的隐藏根目录/文件不受该项过滤。符号链接始终不跟随；直接传入符号链接会报参数错误。重复/重叠输入路径只计算一次，硬链接的不同路径仍会列出。
8. 检查文件的设备、inode、大小及修改/变更时间，尽量发现扫描过程中的修改。它不是事务型快照；请暂停图库同步或编辑后扫描。
9. 脚本只创建新的报告目录，不写原图。操作系统可能因读取而更新访问时间（atime）。如果磁盘写满，输出可能不完整；修复后用新目录重试。
10. **不会上传图片。** 报告包含本地完整路径，也属于可能敏感的信息。不要未经许可将原图或报告提交到 GitHub；本仓库的测试只使用临时生成的合成图。

## 测试与开发

~~~bash
# 完整测试（请先在虚拟环境安装可选依赖）
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q scripts tests
~~~

没有 Pillow 时，标准库模式和安全测试仍可运行，但像素测试会显示跳过；这不算像素模式完成验证。完整测试覆盖字节/像素查重、EXIF、动画全部帧与时长、TIFF 全部页、ICC/高位深边界、损坏图片、权限错误、中文路径、隐藏项、硬链接/符号链接、报告一致性、文件变化和不覆盖原图。

[GitHub Actions](.github/workflows/tests.yml) 配置了 Linux / macOS / Windows，以及 Python 3.10 / 3.12 / 3.14 的组合；实际执行状态见顶部徽章及 Actions 页面。本地验证记录见 [TESTING.md](TESTING.md)。

## 目录

~~~text
image-duplicate-finder-skill/
├── SKILL.md                      # Agent 入口与安全执行流程
├── scripts/find_duplicates.py    # 本地只读扫描器
├── tests/test_find_duplicates.py # 使用临时合成图片的回归测试
├── references/report-format.md  # 报告结构说明
├── requirements-pixels.txt      # 可选 Pillow 依赖
├── .github/workflows/tests.yml  # 跨平台 CI
├── TESTING.md
└── LICENSE
~~~

## 许可证

[MIT](LICENSE)。
