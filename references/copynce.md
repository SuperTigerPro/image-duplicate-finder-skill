# CopyNCE 模型查重

CopyNCE 增强流程用 Descriptor 召回整图和局部候选，以官方 SN 分配额外候选预算，再用独立 Matcher 复核图片对。得分与窗口供人工核对；相似背景、边框或图例可能误报，任意裁剪、很小的嵌图和单张图内部克隆不保证检出。普通查重不需要启用模型。

| 质量参数 | 行为 |
| --- | --- |
| `--quality fast` | 原有 Descriptor 最高余弦；旧命令省略质量参数时保持此行为 |
| `--quality enhanced` | Skill 推荐的模型调用：整图和局部召回、官方查询侧 SN、Matcher 双向整图及证据窗口复核 |
| `--quality deep` | 同一候选流程，改用固定上游代码的双向 LCE；每个无序图对最多70个视图配对 |

增强与深查使用随包默认 SN 背景索引，并按需准备两份官方权重，用户无需提供背景图片。公共索引损坏、模型资源缺失或显式自定义资源无效时明确失败，不静默降为 fast。

## 本机模型

在 Skill 根目录运行。以下路径均为占位符，应替换为获准路径；模型报告必须位于 Skill 目录和所有 Git 工作树之外，并使用尚不存在的新目录。

~~~bash
python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend local \
  --quality enhanced \
  --summary-only --output-dir "/path/to/private-reports/model-001"

# 离线运行须已有兼容依赖和校验通过的官方权重
python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend local \
  --quality fast \
  --offline --weights "/path/to/private-models/descriptor_vits_224.pth.tar" \
  --summary-only --output-dir "/path/to/private-reports/offline-001"
~~~

仅选用模型时才准备运行环境和权重。程序探测兼容 Python、系统和可用后端，选择相应的 PyTorch 安装方式；优先使用可用 NVIDIA CUDA，Apple Silicon Mac 可使用 MPS，没有可用加速后端时使用 CPU。AMD、Intel 或其他显卡不代表当前实现必然支持其加速。设备必须通过实际运算检查；推理时显存不足先减小批量，必要时退回 CPU。可用 `--device cpu` 固定使用 CPU，不自动安装显卡驱动或更改系统 CUDA。

不同设备使用相同权重：Descriptor 为87,514,773字节，Matcher 为86,733,215字节，PyTorch 等依赖另占空间。模型分阶段运行，并限制 Matcher 批量。环境、权重和特征缓存默认放在用户缓存目录。首次准备可能联网下载公共依赖和权重；本机推理不上传图片。`--offline` 禁止自动补下载，缺少依赖或权重时明确失败。可用 `COPYNCE_PYTHON` 指向兼容解释器，用 `COPYNCE_CACHE_DIR` 指定仓库外的缓存位置。

SN 索引随完整 Skill 分发，无需另行联网下载背景图片。省略 `--sn-index` 时自动校验并复制到仓库外私有缓存；环境和模型权重已准备好时，`--quality enhanced --offline` 也可使用默认 SN。只复制脚本而漏掉 `assets` 不属于完整安装，缺少默认资源时应重新获取完整 Skill。

增强复核只生成本对实际使用的整图和局部窗口，最多以128MiB运行内缓存复用这些RGB窗口；该上限不包含原图解码和模型本身。窗口缓存不写磁盘，扫描结束后释放，命中时仍核对原文件身份。评分方向、窗口选择、插值和Matcher批量保持一致。

CUDA增强复核在方向批次无需拆分时，自动使用最多128MiB的FP32编码缓存，复用双向评分中重复的图像编码。缓存按图像像素、模型、设备和实际批次位置区分，设备退回CPU时关闭自动编码缓存。

特征提取最多并行准备两张图片，按原文件顺序提交给单个模型；并发解码的原始像素总量受单图像素上限约束。首次处理在同一次RGB解码中计算SN像素摘要与生成视图。重复扫描仍重算原文件SHA，并校验格式、尺寸和当前安全规则；兼容且完整的缓存可复用像素摘要，失效项重新计算。

手工下载时在[官方模型表](https://github.com/eddielyc/CopyNCE#models)选择 **Descriptor / ViT-S / 224×224**；SHA-256 必须为 `829fdf4ae032bac24b4fcc60535cfc3b53026af083b33a0b8c71f1973f2166db`。不得以未校验的其他权重替代。

`--summary-only` 让本机扫描的终端输出仅包含统计；完整路径仍保存在私有报告中。该选项不等于报告已脱敏，向聊天模型回传内容前仍由宿主控制。

## 默认 SN 背景与自定义索引

默认使用 [`assets/sn-openimages-1000`](../assets/sn-openimages-1000/README.md) 中的公共索引：从 Open Images validation 独立抽样 1,000 张图片，种子为20261127，使用官方 Descriptor 提取512维FP32整图特征。它与2026-11-27完整Copydays测试使用的背景一致；已核对与当时的Copydays图片无相同原始字节及规范RGB像素，未证明不存在所有跨库近似同源。索引约2.8MB，另附[逐图署名、许可及来源记录](../assets/sn-openimages-1000/attribution.csv)，不分发这1,000张原图。

这是 Skill 构建的背景资源，使用官方 SN 算法；背景规模和来源不等同上游的 DISC21 百万训练图设置，也不保证对所有专业图库都是最佳选择。默认无需用户提供背景，运行时仍检查重叠并排除同一文件或完整RGB像素。固定哈希、模型及预处理版本检查通过后，索引才复制到仓库外私有缓存；修改随包文件不能绕过校验。

需要自定义背景时，可以显式传 `--sn-index "/path/to/private-background/index"`。该选项优先于默认资源；路径无效、不兼容或校验失败时直接报错，不以默认背景替代。自定义背景须独立、获准使用，不取本次待查图库；已有兼容索引可直接复用，或者先从明确选定的图片构建：

~~~bash
python3 scripts/copynce_setup.py --build-sn-index "/path/to/private-background/index" \
  --provenance "independent-authorized-background" "/path/to/approved-background-images"
~~~

自定义索引至少需要5张去重后可用的背景图；与本次输入重合的文件及完整RGB像素会被排除，排除后仍须至少5张。来源组标签可通过Python接口提供；没有标签时不能保证不同变体属于不同来源，自定义背景仍须核实其独立性。构建与使用均只读原图，不训练模型。运行索引及自定义索引保存在仓库外，Unix上使用600权限文件和700目录；索引含全局FP32特征、内容摘要、模型及预处理版本、来源信息和完整性指纹，不含图片路径。

采用[固定上游代码](https://github.com/eddielyc/CopyNCE/blob/c2223d18abf3289202666a0d2b9db1c3a7f1b483/scripts/run/score_normalization.py)的参数 `alpha=1,start=1,end=5`：Q/R/A逐行L2归一化，查询与背景相似度降序排列，取Python切片 `[1:5]`，从查询与参考的整图余弦减去这4个近邻的平均相似度。SN不是概率，可能超出余弦范围；同一查询的参考排名不变。现有22视图最高余弦保留为独立局部召回通道，不把它减去整图偏置后称为官方SN。

每图整图/局部K候选合并后形成基础集合，不以0.75提前筛除；额外候选按定向SN安排并限制每图份额。SN不直接修正Matcher分数，候选固定且全部完成复核时不会改变Matcher输出。无序图对保留双向SN。候选调度及证据窗口复核是Skill扩展，不是完整论文复现；旧Descriptor的Copydays阈值不能移用于这些新分数。

Matcher采用官方独立ViT-S/224权重，SHA-256为 `dd8dea1c345039bbda303c42457aa3a0389d46fb37ea39506bfb1faddedda1df`；以 `weights_only=True` 读取并严格加载。官方eval已执行sigmoid，输出称为未校准分类分数。增强档比较双向整图及Descriptor选出的少量证据视图后取最大值；不同视图数量的聚合分数需分别校准。

深查采用[公开代码LCE](https://github.com/eddielyc/CopyNCE/blob/c2223d18abf3289202666a0d2b9db1c3a7f1b483/core/models/copy_detector.py#L437-L460)：26查询视图和10参考视图的35种星形配对，目录无序查重执行两个方向，共70种。论文描述的260种组合与当前公开代码不同，本实现不混称。旋转/裁块窗口不是精确复制边界，单纯相似背景仍可能高分。

增强报告使用schema4：分别记录 `descriptor_cosine`、`local_cosine`、`sn_a_to_b`、`sn_b_to_a`、`matcher_score`，顶层 `score_type=classifier_sigmoid`。`files_complete`只表示文件处理完整，`budget_complete`表示计划复核完成；`complete`还要求没有错误。`stage_totals`记录召回、计划、已核验、失败及待核验数量；最终top_k截断与计算预算耗尽分别显示。返回上限不限制内部复核数量。未覆盖所有可能图对时，不能称全部疑似复用均已找到。

## 私有 SSH 连接

用户在本机配置 SSH 别名、服务器地址、账号及验证方式，宿主读取本机私有连接 profile。密码、验证码、密钥口令和私钥正文不进入聊天、工具 JSON、仓库或日志。优先复用 SSH 别名和 ssh-agent；需要交互认证时，先在本机完成认证，或配置可复用的已认证会话。客户端以 `BatchMode=yes` 运行，不会在后台要求输入密码。

首次连接应在本机终端通过可信渠道核对主机指纹并记录 known_hosts。客户端始终保持 `StrictHostKeyChecking=yes`，主机密钥变化时停止；不能关闭校验来绕过首次配置。客户端关闭 agent/X11 转发、额外端口转发和 SSH 本地命令。

profile 必须在所有 Git 工作树之外，Unix 上属于当前用户并设为 `600` 权限。只用一次可使用本机私有临时文件；需要记住时保存到本机私有配置目录。示例仅包含虚构主机和占位路径：

~~~json
{
  "host": "server.example.org",
  "launcher": "/path/to/skill/start-mcp"
}
~~~

| 字段 | 含义 |
| --- | --- |
| `host` | 已配置的 SSH 别名，或带可选用户名的主机名/IP；真实值只放本机私有文件 |
| `launcher` | 远端 MCP 启动程序的绝对路径 |
| `port` | 可选，整数端口 |
| `identity_file`、`ssh_config` | 可选，本机密钥文件或 SSH 配置文件路径 |
| `jump_host`、`control_path` | 可选，跳板机或本机已认证的 SSH 复用套接字 |

profile 不支持密码或私钥正文等字段。真实地址、账号、认证信息、私有配置、用户图像及其特征、含路径报告和运行日志都严禁提交到 GitHub；发布包只包含经检查的公共源码和默认 SN 公共资源。

## 自有服务器部署

宿主在用户已授权的服务器和部署范围内准备服务；只查询既有服务的授权不自动扩大为修改系统环境。新 POSIX 服务器可在已获取的 Skill 根目录创建独立环境，以下是 CPU 示例：

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install numpy Pillow 'mcp>=1.20,<2'
# 需要近似索引时安装兼容版本；没有它时 auto 可退回完整图对检索
.venv/bin/python -m pip install faiss-cpu
~~~

CUDA 构建应与服务器驱动和 Python 兼容，参见 [PyTorch 安装说明](https://pytorch.org/get-started/locally/)。无需 root，不自动安装驱动。把已校验权重、特征缓存和配置放在仓库外；默认配置位置为 `~/.config/image-duplicate-finder/server.json`，内容使用实际绝对路径并设 `chmod 600`：

~~~json
{
  "weights": "/path/to/private-models/descriptor_vits_224.pth.tar",
  "matcher_weights": "/path/to/private-models/matcher_vits_224.pth.tar",
  "image_roots": ["/path/to/approved-gallery"],
  "cache": "/path/to/private-cache/features.sqlite3",
  "device": "auto",
  "batch_size": 8,
  "max_pixels": 40000000
}
~~~

服务器已配置 `matcher_weights` 且省略 `sn_index` 时，自动使用该完整 Skill 自带的默认 SN，校验后放入服务器仓库外缓存。需要自定义背景才在配置中增加 `"sn_index": "/path/to/private-background/index"`，其无效时不会静默改用默认。也可运行 `python3 scripts/copynce_setup.py --quality enhanced` 准备两份权重和默认 SN，JSON 输出中的 `sn_index` 可用于私有配置；离线时仍须已有兼容环境和权重。

profile 的 `launcher` 指向远端 Skill 根目录的 `start-mcp`。它默认使用 `.venv/bin/python`；服务器环境的 `COPYNCE_PYTHON` 可指定解释器，`COPYNCE_CONFIG` 可指定私有配置。通过 SSH stdio 按需启动，不开放公网 HTTP 端口。

自定义 launcher 包装脚本必须尊重调用者已设置的 `COPYNCE_CONFIG`，只在变量未设置时补默认配置，例如使用 `${COPYNCE_CONFIG:-/path/to/private-config/server.json}`。上传客户端会为每个任务传入隔离图库配置；包装器若无条件覆盖该变量，客户端会在传图前发现图库或缓存范围不符并拒绝上传。

内置服务强制 `image_roots` 边界，拒绝根路径及输入路径经过符号链接，扫描时固定选中文件身份。客户端的远端目录扫描默认要求服务器声明严格图库边界；CLI 不提供放宽此限制的开关。服务使用与本机相同的推理引擎，工作子进程在取消或会话结束时受控回收。

增强客户端传图前协商API2、schema4、两份实际权重哈希、SN实现与背景索引指纹；缺资源或版本不兼容时停止。背景索引是独立只读资源，不加入扫描 `image_roots`，上传任务也不能指定任意背景路径。默认背景随服务器 Skill 分发，不从客户端上传；自定义背景在服务器准备或经单独授权传输，允许上传本次图片不等于允许上传私有背景数据。CPU回退仅改变当前执行地点的设备，不改变数据驻留位置。

## 图库已经在服务器

指定获准图库内的目录，传查询参数并取回候选。绝对路径须位于 `image_roots` 内；相对路径以第一个获准根目录为基准，`.` 表示该根目录。不接受 `..` 或符号链接。报告保留服务器图片路径，仅在本机保存；不为生成预览自动下载服务器原图。

~~~bash
python3 scripts/find_duplicates.py --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --quality enhanced --remote-directory "." \
  --summary-only --output-dir "/path/to/private-reports/remote-001"
~~~

## 本机图片借用远端算力

先预检所选文件数、原文件总字节数和传输方式。默认预检不传图，也不执行远端扫描；目的地显示匿名标识，具体服务器由本机私有 profile 确定。去元数据 PNG 的实际传输体积可能与原文件字节数不同。

~~~bash
python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --plan-upload

python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --quality enhanced --allow-upload \
  --summary-only --output-dir "/path/to/private-reports/upload-001"
~~~

`--allow-upload` 表示用户已授权本次选定图片传往该服务器；仅选择 cloud 不等于授权传图。默认先在本机用 Pillow 完整解码，校正 EXIF 方向，转为 8 位 RGB PNG，并移除 EXIF、ICC 和文本元数据。缺 Pillow 时明确失败，可在当前环境安装 [Pillow 依赖](../requirements-pixels.txt)，不会静默改传原文件。多帧、高位深、特殊颜色格式、损坏图或超限图像会被拒绝。转换只作用于临时推理副本，原图不变；图片中的可见内容仍可能敏感。

仅当用户明确同意发送原文件及其元数据时，在预检和发送命令中都加 `--upload-original`。该选项保留原字节；不会通过转换规避原本不支持的图像类型，远端仍可能拒绝解码或推理。

上传使用随机文件名、独立的远端临时图库和特征缓存。上传前校验隔离配置、空图库和实际模型权重，结果在本机映射回原路径。正常完成或取消时只清理本任务副本和缓存；不清理用户已有图库、正常特征缓存或共享权重。传输需要本机 `sftp`、远端 POSIX 环境及 `python3`。

短时 SFTP 断连可在同一任务内重试。若清理失败，错误会给出匿名的 32 位十六进制任务 ID；重连后无需等待到期即可精确清理，下面的 `JOB_ID` 应替换为错误给出的 ID：

~~~bash
python3 scripts/find_duplicates.py --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --cleanup-job JOB_ID

# 批量清理本账号已到 24 小时期限的自有临时任务
python3 scripts/find_duplicates.py --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --cleanup
~~~

自动看门狗和后续操作会尝试回收过期任务，但服务器停机或权限错误可能阻止自动清理；不能在未确认时声称服务器副本已删除。精确清理校验任务 ID、所属目录和 owner 标记，不扩展到其他目录。此恢复入口用于清理，不承诺跨进程恢复未完成的推理。

## 检索和核对

| CLI 参数 | 用法 |
| --- | --- |
| `--threshold 0.75` | 仅 fast 的最低原始余弦分数，范围 −1 至 1；默认 0.75，需领域校准 |
| `--matcher-threshold 0.5` | 仅增强/深查的分类分数门槛；未校准起点，不是真实重复概率 |
| `--candidate-k 32` | 每张图、每个整图/局部通道的基础候选数量，与报告条数分开 |
| `--matcher-budget 10000` | 本次最多新增核验的无序图对数；缓存不占预算，计划未完成返回1 |
| `--local-view-pairs 2` | 增强档每个方向额外复核的局部证据视图数 |
| `--sn-extra-budget 1000` | 基础候选之外，以SN排序并限制每图份额的额外候选预算 |
| `--sn-index DIR` | 本机增强/深查的可选自定义背景索引；省略时使用随包默认背景 |
| `--whole-only` | 只比较整图；默认比较整图及固定局部裁块 |
| `--search-mode auto` | 自动选择；fast 的 `exact` 枚举所有选中图对，`indexed` 近似检索；增强/深查目前仅支持 exact 或 auto，auto 使用精确分块召回 |
| `--top-k 100` | 最多返回的图对数；达到阈值的候选更多时报告标明截断 |
| `--no-recursive` | 只扫描选定目录的直接子文件 |
| `--summary-only` | 终端只显示统计，完整私有报告仍含路径；云端终端默认不列图像路径 |

fast 模型报告为 `schema_version=3`，读取 `matches`；每对包含原始余弦和两侧命中窗口，窗口坐标对应 EXIF 校正后的像素。`complete` 表示文件处理是否完整，`exhaustive` 表示是否枚举所有选中图对，`truncated` 表示返回数量是否达到上限；完整检索仍只覆盖生成的整图和固定裁块。与用户本机原图对应的预览最多展示 12 对，完整结果以报告为准。模型余弦与普通 similar 的 0–100 分独立。详见 [报告格式](report-format.md) 和 [宿主工具接入](client-compatibility.md)。

## 预算不足后继续

本机和服务器已有图库使用同一私有SQLite缓存逐对保存成功的正、负Matcher分数。预算耗尽后，用相同图库、质量和视图参数重跑，并选择新报告目录；已核验图对从缓存读取，预算只计算本次新增核验。可以增加预算，也可以分多次继续。更改原图内容、模型、流程或复核视图策略会使对应条目失效；背景改变时重新计算SN及候选调度，不复用旧偏置。

schema4的 `stage_totals.matcher_cache_hits`、`matcher_new_verified`、`matcher_attempted_new` 分别说明缓存成功核验数、本次成功数和实际新增尝试数；`matcher_attempted` 包含缓存，因此可能大于本次计算预算。全部复用时Matcher设备标 `cache`，原计算设备另列，不伪称本次进行了GPU推理。

授权上传仍按任务隔离并清理图片和全部查询缓存；预算不足也会正常清理。重新上传会创建新任务，不能跨上传任务恢复Matcher进度。仅描述子/Matcher缓存延续不等于允许长期保留远端上传图片。
