"""Conservative static-image similarity with one public threshold, no network."""
from __future__ import annotations

import hashlib
import html
import io
import math
import os
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

ALGORITHM = "static-similarity-min-v1"
THUMB_SIZE = 128
MAX_FEATURE_IMAGES = 10_000
MAX_CANDIDATE_PAIRS = 200_000
MAX_MATCHED_PAIRS = 20_000
PREVIEW_PAIRS = 12
_DCT = None


class SimilarityLimitError(RuntimeError):
    pass


def threshold_settings(threshold):
    """Derived gates are mathematically consistent with the FIXED score."""
    return {
        "phash_max_distance": (64 * (100 - threshold)) // 100,
        "dhash_max_distance": min(64, (80 * (100 - threshold)) // 100),
        "ssim_min": -1.0 if threshold == 0 else 0.5 + threshold / 200.0,
        "color_min": threshold / 100.0,
        "alpha_min": threshold / 100.0,
        "aspect_ratio_min": math.sqrt(threshold / 100.0),
        "different_icc_score_cap": 89.9,
        "pixel_verification_required": threshold == 100,
    }


def _bits(values):
    value = 0
    for bit in values.flat:
        value = (value << 1) | int(bool(bit))
    return value


def perceptual_hash(gray):
    global _DCT
    if _DCT is None:
        x = np.arange(32, dtype=np.float64)
        k = x[:, None]
        matrix = np.cos(np.pi * (2 * x + 1) * k / 64) * math.sqrt(2 / 32)
        matrix[0] /= math.sqrt(2)
        _DCT = matrix
    with gray.resize((32, 32)) as small:
        samples = np.asarray(small, dtype=np.float64)
    low = np.round((_DCT @ samples @ _DCT.T)[:8, :8], 8)
    median = np.median(low.flat[1:])
    bits = low > median
    bits[0, 0] = False  # Ignore DC brightness; a constant image has a stable hash.
    return _bits(bits)


def difference_hash(gray):
    with gray.resize((9, 8)) as small:
        samples = np.asarray(small, dtype=np.int16)
    return _bits(samples[:, 1:] > samples[:, :-1])


def _mean_window(array, size=7):
    """Valid uniform-window mean using integral images, not SciPy/skimage."""
    summed = np.pad(array, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (summed[size:, size:] - summed[:-size, size:]
            - summed[size:, :-size] + summed[:-size, :-size]) / (size * size)


def structural_similarity(left, right):
    """Luminance SSIM: 7x7 uniform windows, sample covariance, data range 1."""
    a, b = left.astype(np.float64) / 255, right.astype(np.float64) / 255
    ma, mb = _mean_window(a), _mean_window(b)
    correction = 49 / 48
    va = np.maximum(0, (_mean_window(a * a) - ma * ma) * correction)
    vb = np.maximum(0, (_mean_window(b * b) - mb * mb) * correction)
    cov = (_mean_window(a * b) - ma * mb) * correction
    result = ((2 * ma * mb + 0.01 ** 2) * (2 * cov + 0.03 ** 2)
              / ((ma * ma + mb * mb + 0.01 ** 2) * (va + vb + 0.03 ** 2)))
    return float(np.clip(np.mean(result), -1, 1))


@dataclass
class Feature:
    record: object
    pixels: str
    phash: int
    dhash: int
    gray: object
    color: object
    alpha: object
    aspect: float
    size: tuple
    profile: str
    low_information: bool
    thumbnail: bytes


def extract_feature(record, scanner, checked_stream, pixel_digest):
    Image, ImageOps = scanner.pillow
    with checked_stream(record) as stream, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(stream) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("similar 首版仅支持静态单帧图；动画/多页图请用 exact 或 pixels")
            if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                raise ValueError("similar 不对高位深/科学或特殊颜色模式做有损比较；请用 exact 或 pixels")
            width, height = image.size
            if width * height > min(scanner.options.max_pixels, scanner.options.max_total_pixels):
                raise ValueError("图片像素数超过安全上限；可使用 --mode exact")
            # Validate precision / multi-resolution ICO with the strict hasher below.
            frame = ImageOps.exif_transpose(image)
            rgba = frame.convert("RGBA")
            try:
                size = rgba.size
                fitted = ImageOps.contain(rgba, (THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
                try:
                    xy = ((THUMB_SIZE - fitted.width) // 2, (THUMB_SIZE - fitted.height) // 2)
                    canvas = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), "white")
                    alpha_canvas = Image.new("L", (THUMB_SIZE, THUMB_SIZE), 255)
                    channel = fitted.getchannel("A")
                    try:
                        canvas.paste(fitted, xy, channel)
                        alpha_canvas.paste(channel, xy)
                        with ImageOps.grayscale(canvas) as gray:
                            phash, dhash = perceptual_hash(gray), difference_hash(gray)
                            gray_array = np.asarray(gray).copy()
                        with canvas.resize((16, 16), Image.Resampling.BOX) as small:
                            color = np.asarray(small).copy()
                        with alpha_canvas.resize((16, 16), Image.Resampling.BOX) as small:
                            alpha = np.asarray(small).copy()
                        buffer = io.BytesIO()
                        canvas.save(buffer, format="PNG")
                        thumbnail = buffer.getvalue()
                    finally:
                        channel.close()
                        canvas.close()
                        alpha_canvas.close()
                finally:
                    fitted.close()
                profile = hashlib.sha256(frame.info.get("icc_profile") or b"").hexdigest()
            finally:
                rgba.close()
                frame.close()
    # Separate checked opens: some Pillow decoders close their supplied stream.
    with checked_stream(record) as stream:
        pixels = pixel_digest(stream, scanner.options, scanner.pillow)
    histogram = np.bincount(gray_array.ravel(), minlength=256).astype(np.float64)
    probability = histogram[histogram > 0] / gray_array.size
    entropy = float(-np.sum(probability * np.log2(probability)))
    return Feature(record, pixels, phash, dhash, gray_array, color, alpha,
                   size[0] / size[1], size, profile, entropy < 1.0 or float(np.std(gray_array)) < 5.0, thumbnail)


class BKTree:
    """Exact Hamming-radius candidate search; duplicate hashes share a bucket."""
    def __init__(self):
        self.root = None

    def add(self, value, index):
        if self.root is None:
            self.root = [value, [index], {}]
            return
        node = self.root
        while True:
            distance = (value ^ node[0]).bit_count()
            if distance == 0:
                node[1].append(index)
                return
            if distance not in node[2]:
                node[2][distance] = [value, [index], {}]
                return
            node = node[2][distance]

    def query(self, value, radius):
        stack = [] if self.root is None else [self.root]
        while stack:
            node = stack.pop()
            distance = (value ^ node[0]).bit_count()
            if distance <= radius:
                yield from node[1]
            for edge in sorted(node[2], reverse=True):
                if distance - radius <= edge <= distance + radius:
                    stack.append(node[2][edge])


def pair_score(left, right, threshold):
    """Threshold may prune impossible pairs, but NEVER changes a pair's score."""
    ph = (left.phash ^ right.phash).bit_count()
    dh = (left.dhash ^ right.dhash).bit_count()
    if left.pixels == right.pixels:
        return 100.0, {"phash_distance": ph, "dhash_distance": dh, "ssim": 1.0,
                       "color_score": 100.0, "alpha_score": 100.0, "aspect_score": 100.0,
                       "icc_equal": True}
    if threshold == 100:
        return None
    gates = threshold_settings(threshold)
    if ph > gates["phash_max_distance"] or dh > gates["dhash_max_distance"]:
        return None
    aspect = 100 * (min(left.aspect, right.aspect) / max(left.aspect, right.aspect)) ** 2
    color = 100 * (1 - np.mean(np.abs(left.color.astype(np.float64) - right.color)) / 255)
    alpha = 100 * (1 - np.mean(np.abs(left.alpha.astype(np.float64) - right.alpha)) / 255)
    profile_cap = 100.0 if left.profile == right.profile else 89.9
    # 100 is reserved for strict full-resolution pixel verification.
    score = min(99.9, 100 - 100 * ph / 64, 100 - 100 * dh / 80,
                float(color), float(alpha), aspect, profile_cap)
    if score < threshold:
        return None
    ssim = structural_similarity(left.gray, right.gray)
    score = min(score, max(0, min(100, 200 * ssim - 100)))
    if score < threshold:
        return None
    return float(score), {"phash_distance": ph, "dhash_distance": dh, "ssim": round(ssim, 8),
                          "color_score": round(float(color), 6), "alpha_score": round(float(alpha), 6),
                          "aspect_score": round(aspect, 6), "icc_equal": left.profile == right.profile}


def make_pair(left, right, score, metrics):
    pair_warnings = []
    if left.low_information or right.low_information:
        pair_warnings.append("含低纹理/大面积空白图片：高分也须人工复核")
    if left.profile != right.profile:
        pair_warnings.append("ICC 配置不同，未进行色彩管理；分数上限为 89.9")
    files = [{"path": str(feature.record.path), "size_bytes": feature.record.size,
              "mtime_ns": feature.record.stamp[3], "width": feature.size[0], "height": feature.size[1]}
             for feature in (left, right)]
    files.sort(key=lambda item: item["path"])
    return {"score": math.floor(score * 10) / 10, "pixel_identical": left.pixels == right.pixels,
            "same_inode": bool(left.record.stamp[1] and left.record.stamp[:2] == right.record.stamp[:2]),
            "files": files, "metrics": metrics, "warnings": pair_warnings}


def render_preview(pairs, features, pillow, threshold):
    from PIL import ImageDraw
    Image, _ = pillow
    selected = pairs[:PREVIEW_PAIRS]
    if not selected:
        return None
    images = {str(feature.record.path): feature.thumbnail for feature in features}
    sheet = Image.new("RGB", (330, 30 + len(selected) * 158), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((8, 8), f"Similarity >= {threshold} | first {len(selected)} pairs", fill="black")
    for index, pair in enumerate(selected):
        top = 30 + index * 158
        label = "pixels equal" if pair["pixel_identical"] else "review required"
        draw.text((8, top), f"{pair['id']}  {pair['score']:.1f}/100  {label}", fill="black")
        for column, item in enumerate(pair["files"]):
            with Image.open(io.BytesIO(images[item["path"]])) as thumb:
                sheet.paste(thumb, (8 + column * 170, top + 20))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    sheet.close()
    return buffer.getvalue()


def scan(scanner, checked_stream, pixel_digest, assert_unchanged, version):
    threshold = getattr(scanner.options, "similarity_threshold", 90)
    features = []
    image_limited = False
    for record in scanner.files:
        if len(features) >= MAX_FEATURE_IMAGES:
            image_limited = True
            scanner.error(record.path, "similarity", SimilarityLimitError(
                f"达到内部图片上限 {MAX_FEATURE_IMAGES}，结果不完整；请分目录扫描"))
            break
        try:
            features.append(extract_feature(record, scanner, checked_stream, pixel_digest))
        except Exception as exc:
            scanner.error(record.path, "fingerprint", exc)
    scanner.fingerprinted = len(features)
    tree, strict_buckets = BKTree(), defaultdict(list)
    pairs = []
    candidate_count = 0
    limited = False
    for index, current in enumerate(features):
        candidates = (iter(strict_buckets[current.pixels]) if threshold == 100
                      else tree.query(current.phash, threshold_settings(threshold)["phash_max_distance"]))
        for other_index in candidates:
            if candidate_count >= MAX_CANDIDATE_PAIRS or len(pairs) >= MAX_MATCHED_PAIRS:
                scanner.error(current.record.path, "similarity", SimilarityLimitError(
                    "达到内部候选/结果安全上限，结果不完整；请提高阈值或分目录扫描"))
                limited = True
                break
            candidate_count += 1
            result = pair_score(features[other_index], current, threshold)
            if result is not None:
                score, metrics = result
                pairs.append(make_pair(features[other_index], current, score, metrics))
        if limited:
            break
        tree.add(current.phash, index)
        strict_buckets[current.pixels].append(index)
    # Revalidate every path used in a result, not merely during initial decode.
    used = {item["path"] for pair in pairs for item in pair["files"]}
    unstable = set()
    for feature in features:
        if str(feature.record.path) in used:
            try:
                assert_unchanged(feature.record, os.stat(feature.record.path, follow_symlinks=False))
            except OSError as exc:
                unstable.add(str(feature.record.path))
                scanner.error(feature.record.path, "verify", exc)
    pairs = [pair for pair in pairs if not any(item["path"] in unstable for item in pair["files"])]
    pairs.sort(key=lambda pair: (-pair["score"], pair["files"][0]["path"], pair["files"][1]["path"]))
    for number, pair in enumerate(pairs, 1):
        pair["id"] = f"S{number:05d}"
    scanner.preview_png = render_preview(pairs, features, scanner.pillow, threshold)
    notes = ["分数不是相同像素百分比或同源概率；非100分均为疑似相似，须人工复核",
             "仅检测静态整图相似，不启用裁剪、任意旋转、局部或语义匹配",
             "相似关系不传递；A像B、B像C不代表A像C；不自动删除原图"]
    if threshold < 80:
        notes.append("当前阈值低于日常建议范围80～95，误报与计算量可能明显增加；推荐默认90")
    return {
        "schema_version": 2, "tool": {"name": "image-duplicate-finder", "version": version},
        "generated_at": datetime.now(timezone.utc).isoformat(), "mode": "similar", "algorithm": ALGORITHM,
        "complete": not scanner.errors, "inputs": [str(path) for path in scanner.roots],
        "similarity_threshold": threshold,
        "options": {"recursive": scanner.options.recursive, "include_hidden": scanner.options.include_hidden,
                    "extensions": sorted(scanner.options.extensions) if scanner.options.extensions is not None else "*",
                    "max_pixels": scanner.options.max_pixels, "max_frames": scanner.options.max_frames,
                    "max_total_pixels": scanner.options.max_total_pixels,
                    "derived_internal_gates": threshold_settings(threshold)},
        "summary": {"scanned_images": len(scanner.files), "fingerprinted_images": len(features),
                    "similar_pairs": len(pairs), "images_in_pairs": len({f["path"] for p in pairs for f in p["files"]}),
                    "pixel_identical_pairs": sum(pair["pixel_identical"] for pair in pairs),
                    "candidate_pairs_checked": candidate_count,
                    "possible_pairs": len(features) * (len(features) - 1) // 2,
                    "safety_limit_reached": limited or image_limited,
                    "duplicate_groups": 0, "duplicate_images": 0, "extra_copies": 0,
                    "extra_copy_logical_bytes": 0, "hard_link_duplicate_paths": 0,
                    "errors": len(scanner.errors), "skipped": dict(sorted(scanner.skipped.items()))},
        "groups": [], "similar_pairs": pairs, "errors": scanner.errors, "warnings": notes,
        "preview": {"file": "similarity-preview.png", "pairs_shown": min(PREVIEW_PAIRS, len(pairs))} if pairs else None,
        "safety_limits": {"images": MAX_FEATURE_IMAGES, "candidate_pairs": MAX_CANDIDATE_PAIRS,
                          "matched_pairs": MAX_MATCHED_PAIRS},
    }


def write_csv(writer, report):
    writer.writerow(["pair_id", "similarity_score", "threshold", "pixel_identical", "path_a", "path_b",
                     "size_a_bytes", "size_b_bytes", "same_inode"])
    for pair in report["similar_pairs"]:
        a, b = pair["files"]
        writer.writerow([pair["id"], f"{pair['score']:.1f}", report["similarity_threshold"], pair["pixel_identical"],
                         a["path"], b["path"], a["size_bytes"], b["size_bytes"], pair["same_inode"]])


def markdown_report(report, display_text):
    def code(value):
        return "<code>" + html.escape(display_text(value)) + "</code>"
    summary = report["summary"]
    lines = ["# 本地相似图片报告", "", f"- 时间（UTC）：{report['generated_at']}",
             f"- 相似度门槛：**{report['similarity_threshold']} / 100**（默认推荐90，日常建议80～95）",
             f"- 状态：{'完整' if report['complete'] else '不完整：存在错误或达到安全上限'}",
             f"- 候选图片：{summary['scanned_images']}；相似图片对：{summary['similar_pairs']}；"
             f"涉及图片路径：{summary['images_in_pairs']}", "", "## 扫描范围", ""]
    lines += ["- " + code(path) for path in report["inputs"]]
    lines += ["", "## 判定说明", ""] + ["- " + text for text in report["warnings"]]
    if report["preview"]:
        lines += ["", "## 并排预览", "", "![相似图片对预览](similarity-preview.png)", "",
                  f"仅展示前 {report['preview']['pairs_shown']} 对；左右顺序与下方完整路径一致。"]
    lines += ["", "## 逐对清单", ""]
    if not report["similar_pairs"]:
        lines.append("已成功处理的范围内未发现达到当前门槛的图片对；有错误时不能对全目录下结论。")
    for pair in report["similar_pairs"]:
        label = "严格像素相同" if pair["pixel_identical"] else "疑似相似，须人工复核"
        lines += [f"### {pair['id']} — {pair['score']:.1f} / 100（{label}）", ""]
        for index, item in enumerate(pair["files"]):
            lines.append(f"- {'左图' if index == 0 else '右图'}：{code(item['path'])}；"
                         f"{item['width']} × {item['height']}；{item['size_bytes']} 字节")
        if pair["same_inode"]:
            lines.append("- 两个路径指向同一硬链接文件，不是独立存储副本。")
        lines += ["- 注意：" + text for text in pair["warnings"]] + [""]
    lines += ["", "## 错误与安全", ""]
    lines += ["- " + code(error["path"]) + "：" + code(error["message"]) for error in report["errors"]]
    if not report["errors"]:
        lines.append("- 未记录扫描错误。")
    lines += ["- 原图未被修改、删除或上传；报告和预览也可能敏感，请勿未经许可公开。",
              "- 100分保留给原始分辨率的严格像素核验，其他分数最多99.9。",
              "- 评分定义不随门槛变化；内部判定门槛自动推导，不需要用户配置。",
              "- 低纹理/大面积空白图即使高分也不代表内容相同或同源。", ""]
    return "\n".join(lines)


def print_report(report, display_text):
    summary = report["summary"]
    print(f"图片相似检测 | 阈值 {report['similarity_threshold']}/100 | "
          f"{'完整' if report['complete'] else '不完整'}")
    print(f"候选图片 {summary['scanned_images']}；相似图片对 {summary['similar_pairs']}；"
          f"涉及图片 {summary['images_in_pairs']}")
    for pair in report["similar_pairs"]:
        label = "严格像素相同" if pair["pixel_identical"] else "疑似相似，人工复核"
        print(f"\n{pair['id']} | {pair['score']:.1f}/100 | {label}")
        for item in pair["files"]:
            print("  " + display_text(item["path"]))
        for note in pair["warnings"]:
            print("  注意：" + note)
    if not report["similar_pairs"]:
        print("已成功处理的范围内没有达到当前门槛的图片对；有错误时不能对全目录下结论。")
    for note in report["warnings"]:
        print("说明：" + note)
    print("跳过项目：" + str(summary["skipped"]))
    for error in report["errors"]:
        print("错误：" + display_text(error["path"]) + "：" + display_text(error["message"]), file=sys.stderr)
    print("只读扫描完成，未删除、修改或上传任何原图。相似结果不用于自动清理。")
