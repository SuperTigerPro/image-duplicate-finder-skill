# 报告格式

exact / pixels 保持 schema_version=1。similar 使用 schema_version=2，按图片对报告，见文末“相似模式”。copynce 使用 schema_version=3。下面先说明原有严格查重结构。

脚本在一个**新目录**中输出以下三种报告；已有目录拒绝覆盖。报告不嵌入图片内容，但含完整路径，分享前注意隐私。

## JSON：duplicates.json

顶层字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| schema_version | integer | 当前为 1，报告结构版本 |
| tool | object | 工具名和软件版本 |
| generated_at | string | UTC ISO 8601 时间 |
| mode | string | exact 或 pixels |
| algorithm | string | sha256 或 decoded-pixels-sha256-v1 |
| complete | boolean | 没有记录扫描错误为 true；仅覆盖 inputs 与 options 指定范围 |
| inputs | array of string | 已去除重复输入参数的规范化绝对路径；重叠目录可以仍出现在此字段中，但文件只计一次 |
| options | object | 递归、隐藏项、扩展名及像素/帧数限制 |
| summary | object | 扫描统计，见下表 |
| groups | array | 按参考路径排序的重复分组 |
| errors | array | 未成功处理的路径、发生阶段、异常类型和原因 |

summary：

| 字段 | 含义 |
| --- | --- |
| scanned_images | 发现的候选图片路径数量，含解码失败的文件；受扩展名过滤规则限制 |
| fingerprinted_images | 成功生成指纹的候选数；exact 对大小唯一的文件不计算指纹，因此可能小于 scanned_images |
| duplicate_groups | 重复组数 |
| duplicate_images | 全部分组内的图片路径总数，含各组参考项 |
| extra_copies | 各组（路径数 - 1）的总和 |
| extra_copy_logical_bytes | 各组排除参考项后的文件逻辑长度之和，不是可回收空间估计 |
| hard_link_duplicate_paths | 分组中被标注为先前成员硬链接的额外路径数 |
| errors | 错误条目数量 |
| skipped | 分类计数：hidden_entries、symlink_entries、non_image_files、subdirectories、other_entries；仅出现实际用到的键 |

隐藏目录和未递归目录以入口数计数，不会为了统计再遍历其内部文件。被跳过不代表其中没有重复。

每个 groups 条目：

~~~json
{
  "id": "G0001",
  "digest": "<64 位十六进制 SHA-256>",
  "reference": "/demo/a.png",
  "files": [
    {"path": "/demo/a.png", "size_bytes": 100, "mtime_ns": 0, "hard_link_to": null},
    {"path": "/demo/b.png", "size_bytes": 100, "mtime_ns": 0, "hard_link_to": null}
  ],
  "extra_copy_logical_bytes": 100
}
~~~

上例数值和路径仅演示结构。mtime_ns 是文件系统修改时间的纳秒表示。hard_link_to 非空时为同组较早成员的绝对路径。reference 恒为 files 第一项；不代表用户应保留这张图。不同扫描中输入文件变化时组号可能变化，不要当持久文件 ID 使用。

每个 errors 条目：

~~~json
{
  "path": "/demo/broken.png",
  "operation": "fingerprint",
  "error_type": "UnidentifiedImageError",
  "message": "..."
}
~~~

operation 可能是 stat、scan、fingerprint 或 verify。输入参数或报告输出阶段出错会返回退出码 2，不保证存在完整 JSON。不能只检查生成文件是否存在，还必须检查进程退出码。

## CSV：duplicates.csv

UTF-8 BOM 编码，标准 CSV 引号转义，字段如下：

~~~text
group_id,role,digest,size_bytes,path,hard_link_to
~~~

- 每个重复组的所有成员各占一行，包含参考副本。
- role 为 reference 或 duplicate。
- 路径是完整绝对路径；逗号、换行和引号由 CSV 标准引用，不可自行按逗号或换行字符串拆分。
- 不包含错误清单；错误和完整性状态以 JSON / Markdown 为准。
- 没有重复时只包含表头。这不意味着扫描没有错误。

## Markdown：duplicates.md

包含扫描时间、模式、完整性、范围、统计、逐组完整路径、硬链接标记及错误原因。路径中的 HTML 和终端控制字符做了显示转义；需要原始路径时读取 JSON / CSV，不要把转义后的展示文本当路径执行。

## 指纹规则

- exact：对完整原始文件流计算 SHA-256；不是对文件名或缩略图计算哈希。
- pixels：版本化的 SHA-256，包含全部解码帧的尺寸、规范化模式、像素字节、动画时长、循环和 ICC 配置指纹。普通 8 位图归一到 RGBA；其他模式保留原始模式和解码数据。
- pixels 指纹不应当作跨 Pillow 版本、跨解码器的长期图片标识。不同版本的解码行为可能不同。
- exact 与 pixels 的 digest 不能直接互相比对；应同时检查 mode、algorithm 和 schema_version。
- exact/pixels 不是相似图检测。严格像素核验依赖SHA-256的抗碰撞性质；similar另按下述结构报告。

## 相似模式：schema_version = 2

模式为 similar，算法标识 static-similarity-min-v1；详细定义见 [相似检测](similarity.md)。仍有 tool、generated_at、inputs、options、complete、errors，但不要把疑似相似对当作重复组。

新增/不同字段：

| 字段 | 含义 |
| --- | --- |
| similarity_threshold | 本次0～100整数门槛，默认90 |
| similar_pairs | 达到当前门槛的逐对结果，按分数降序再按完整路径排序 |
| groups | 恒为空数组，避免旧客户端误把相似关系当等价分组 |
| options.derived_internal_gates | 从唯一门槛自动推导的内部门槛，仅供审计，不是额外用户参数 |
| warnings | 非概率、非传递、范围和低门槛等说明 |
| preview | 有图片对时包含 file、pairs_shown；预览最多12对，不限制完整清单 |
| safety_limits | 内部图片/候选/结果安全上限；触发时有明确错误与退出码1 |

summary 的新增字段为 similar_pairs、images_in_pairs、pixel_identical_pairs、candidate_pairs_checked、possible_pairs、safety_limit_reached。possible_pairs以成功提取特征的图片数计算；candidate_pairs_checked只统计索引筛出的候选数，不保证每对都执行耗时SSIM。原 duplicate_groups / duplicate_images / extra_copies / extra_copy_logical_bytes 等旧字段置0，不能据此判断相似模式是否有结果，也不据疑似相似计算磁盘收益。

每个 similar_pairs 条目：

~~~json
{
  "id": "S00001",
  "score": 94.0,
  "pixel_identical": false,
  "same_inode": false,
  "files": [
    {"path": "/demo/a.png", "size_bytes": 100, "mtime_ns": 0, "width": 384, "height": 256},
    {"path": "/demo/a-resized.png", "size_bytes": 80, "mtime_ns": 0, "width": 192, "height": 128}
  ],
  "metrics": {"phash_distance": 2, "dhash_distance": 3, "ssim": 0.97,
              "color_score": 98.1, "alpha_score": 100.0, "aspect_score": 100.0, "icc_equal": true},
  "warnings": []
}
~~~

数值仅演示字段结构，不是对该示例逐项计算的结果。score固定定义并向下保留1位小数；100只给完整分辨率严格像素核验通过的图片对，其余上限99.9。文件按完整路径排序，与预览左右顺序一致。

相似模式的 CSV 表头改为：

~~~text
pair_id,similarity_score,threshold,pixel_identical,path_a,path_b,size_a_bytes,size_b_bytes,same_inode
~~~

每个图片对一行；一个图片路径可能出现在多个图片对中，不能把行数当图片数。CSV仍为UTF-8 BOM，路径按标准CSV引用。Markdown为逐对分数/路径/警告，并引用有结果时生成的 similarity-preview.png。不要把预览包含的对数当总结果数。

相似模式的安全上限、动画/多页/特殊模式不支持、损坏图和文件变化会记录 errors，complete=false，进程退出1。部分结果不适合做全目录阈值召回比较。原图不会被删除或修改。

## 模型模式（schema_version = 3）

`mode="copynce"` 时读取 `matches`。每对含 `path_a`、`path_b`、`score`、`size_a`、`size_b`、`box_a`、`box_b`。分数为原始余弦。区域以经过 EXIF 方向校正的原图像素计，格式为 `[x0, y0, x1, y1)`，右/下边界除外。区域框对应参与匹配的窗口，精确复用边界需要回图核实。

| 字段 | 用法 |
| --- | --- |
| `files_complete`、`complete`、`errors` | 前两者同义，表示所选文件是否成功处理。坏图等会记录错误；仍可交付部分结果。 |
| `exhaustive` | 检索是否覆盖全部成功处理的图对。近似检索可能遗漏候选。 |
| `truncated` | 达到门槛的候选数量是否超过返回上限。 |
| `all_matches_reported` | 成功处理、覆盖所有图对且未截断时为真。 |
| `model_id`、`weights_sha256`、`pipeline`、`precision` | 模型、权重、预处理/裁块版本及数值精度；缓存也按权重、预处理、裁块及文件内容和状态区分。 |
| `summary.cache_hits`、`summary.cache_misses` | 本次复用和新提取特征的成功图片数。 |
| `safety_limits` | 内部图片10000、特征窗口100000、返回图对20000上限。超过输入/特征上限保留部分结果并标记文件处理不完整，不静默宣称扫描全部完成。 |
| `match_type` | 每个matches条目中的whole-whole、whole-region或region-region；描述命中窗口组合，不能据局部命中判整图相同。 |

近似检索或设置返回上限的任务可以正常退出 0。仅当 `all_matches_reported` 为真时才称为“本次范围与阈值下的完整候选清单”。本机原图可读时，PNG 预览最多展示 12 对并绘制匹配框；宿主缺少 Pillow 时可使用 HTML 预览。服务器已有图库只返回路径与区域，无需下载原图。CSV、JSON、Markdown 记录本次返回的完整图对清单。

模型余弦分数范围为-1至1，不是概率，不与similar模式0～100分混用。完整精确检索只覆盖当前生成的整图和固定裁块，不承诺任意裁剪或单图内部克隆的召回。模型报告拒绝保存在Skill目录或任何Git工作树；云端终端输出统计，完整私有报告含原图路径，禁止公开上传。临时传图结果映射回本机原始路径，不保留远端任务配置和服务器错误原文。
