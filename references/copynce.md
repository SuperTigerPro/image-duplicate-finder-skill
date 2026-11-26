# CopyNCE 模型查重

CopyNCE Descriptor 比较静态图片的整图与固定局部裁块，筛出两张图片之间疑似裁剪、拼接或嵌套复用的候选。得分与窗口供人工核对；相似背景、边框或图例可能误报，任意裁剪、很小的嵌图和单张图内部克隆不保证检出。普通查重不需要启用模型。

## 本机模型

在 Skill 根目录运行。以下路径均为占位符，应替换为获准路径；模型报告必须位于 Skill 目录和所有 Git 工作树之外，并使用尚不存在的新目录。

~~~bash
python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend local \
  --summary-only --output-dir "/path/to/private-reports/model-001"

# 离线运行须已有兼容依赖和校验通过的官方权重
python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend local \
  --offline --weights "/path/to/private-models/descriptor_vits_224.pth.tar" \
  --summary-only --output-dir "/path/to/private-reports/offline-001"
~~~

仅选用模型时才准备运行环境和权重。程序探测兼容 Python、系统和可用后端，选择相应的 PyTorch 安装方式；优先使用可用 NVIDIA CUDA，Apple Silicon Mac 可使用 MPS，没有可用加速后端时使用 CPU。AMD、Intel 或其他显卡不代表当前实现必然支持其加速。设备必须通过实际运算检查；推理时显存不足先减小批量，必要时退回 CPU。可用 `--device cpu` 固定使用 CPU，不自动安装显卡驱动或更改系统 CUDA。

不同设备使用同一份约 87.5 MB 的权重，PyTorch 等依赖另占空间。环境、权重和特征缓存默认放在用户缓存目录。首次准备可能联网下载公共依赖和权重；本机推理不上传图片。`--offline` 禁止自动补下载，缺少依赖或权重时明确失败。可用 `COPYNCE_PYTHON` 指向兼容解释器，用 `COPYNCE_CACHE_DIR` 指定仓库外的缓存位置。

手工下载时在[官方模型表](https://github.com/eddielyc/CopyNCE#models)选择 **Descriptor / ViT-S / 224×224**；SHA-256 必须为 `829fdf4ae032bac24b4fcc60535cfc3b53026af083b33a0b8c71f1973f2166db`。不得以未校验的其他权重替代。

`--summary-only` 让本机扫描的终端输出仅包含统计；完整路径仍保存在私有报告中。该选项不等于报告已脱敏，向聊天模型回传内容前仍由宿主控制。

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

profile 不支持密码或私钥正文等字段。真实地址、账号、认证信息、私有配置、图像、特征、含路径报告和运行日志都严禁提交到 GitHub；发布包只包含经检查的公共源码。

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
  "image_roots": ["/path/to/approved-gallery"],
  "cache": "/path/to/private-cache/features.sqlite3",
  "device": "auto",
  "batch_size": 8,
  "max_pixels": 40000000
}
~~~

profile 的 `launcher` 指向远端 Skill 根目录的 `start-mcp`。它默认使用 `.venv/bin/python`；服务器环境的 `COPYNCE_PYTHON` 可指定解释器，`COPYNCE_CONFIG` 可指定私有配置。通过 SSH stdio 按需启动，不开放公网 HTTP 端口。

自定义 launcher 包装脚本必须尊重调用者已设置的 `COPYNCE_CONFIG`，只在变量未设置时补默认配置，例如使用 `${COPYNCE_CONFIG:-/path/to/private-config/server.json}`。上传客户端会为每个任务传入隔离图库配置；包装器若无条件覆盖该变量，客户端会在传图前发现图库或缓存范围不符并拒绝上传。

内置服务强制 `image_roots` 边界，拒绝根路径及输入路径经过符号链接，扫描时固定选中文件身份。客户端的远端目录扫描默认要求服务器声明严格图库边界；CLI 不提供放宽此限制的开关。服务使用与本机相同的推理引擎，工作子进程在取消或会话结束时受控回收。

## 图库已经在服务器

指定获准图库内的目录，传查询参数并取回候选。绝对路径须位于 `image_roots` 内；相对路径以第一个获准根目录为基准，`.` 表示该根目录。不接受 `..` 或符号链接。报告保留服务器图片路径，仅在本机保存；不为生成预览自动下载服务器原图。

~~~bash
python3 scripts/find_duplicates.py --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --remote-directory "." \
  --summary-only --output-dir "/path/to/private-reports/remote-001"
~~~

## 本机图片借用远端算力

先预检所选文件数、原文件总字节数和传输方式。默认预检不传图，也不执行远端扫描；目的地显示匿名标识，具体服务器由本机私有 profile 确定。去元数据 PNG 的实际传输体积可能与原文件字节数不同。

~~~bash
python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --plan-upload

python3 scripts/find_duplicates.py "/path/to/selected-gallery" --mode copynce --backend cloud \
  --profile "/path/to/private-config/profile.json" --allow-upload \
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
| `--threshold 0.75` | 最低原始余弦分数，范围 −1 至 1；默认 0.75，需用代表性正负图对校准 |
| `--whole-only` | 只比较整图；默认比较整图及固定局部裁块 |
| `--search-mode auto` | 自动选择；`exact` 枚举所有选中图对，`indexed` 近似检索候选，可能遗漏 |
| `--top-k 100` | 最多返回的图对数；达到阈值的候选更多时报告标明截断 |
| `--no-recursive` | 只扫描选定目录的直接子文件 |
| `--summary-only` | 终端只显示统计，完整私有报告仍含路径；云端终端默认不列图像路径 |

模型报告为 `schema_version=3`，读取 `matches`；每对包含原始余弦和两侧命中窗口，窗口坐标对应 EXIF 校正后的像素。`complete` 表示文件处理是否完整，`exhaustive` 表示是否枚举所有选中图对，`truncated` 表示返回数量是否达到上限；完整检索仍只覆盖生成的整图和固定裁块。与用户本机原图对应的预览最多展示 12 对，完整结果以报告为准。模型余弦与普通 similar 的 0–100 分独立。详见 [报告格式](report-format.md) 和 [宿主工具接入](client-compatibility.md)。
