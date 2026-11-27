#!/usr/bin/env python3
"""Read-only local image duplicate finder. Python 3.10+; Pillow is optional."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import stat
import struct
import sys
import unicodedata
import warnings
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.3.1"
CHUNK_SIZE = 1024 * 1024
DEFAULT_EXTENSIONS = (
    ".jpg", ".jpeg", ".jfif", ".png", ".apng", ".gif", ".webp", ".bmp",
    ".dib", ".tif", ".tiff", ".ico", ".ppm", ".pgm", ".pbm", ".pnm",
    ".pcx", ".tga", ".avif", ".heic", ".heif", ".jxl", ".svg", ".psd",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
)
NORMALIZE_MODES = {"1", "L", "LA", "P", "RGB", "RGBA"}


def display_text(value):
    """Do not allow filenames/errors to inject terminal or Markdown controls."""
    result = []
    for char in str(value):
        if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}:
            result.append("\\u%04x" % ord(char))
        else:
            result.append(char)
    return "".join(result)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class ImageFile:
    path: Path
    size: int
    stamp: tuple


class ChangedFileError(OSError):
    pass


def canonical_input(value):
    """Resolve an explicit input without following user-created symbolic links."""
    path = Path(value).expanduser().absolute()
    if sys.platform == "darwin" and path.parts[1:2] in (("tmp",), ("var",)):
        alias = Path("/", path.parts[1])
        private = Path("/private", path.parts[1])
        if alias.is_symlink() and alias.resolve() == private:
            path = private.joinpath(*path.parts[2:])
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("不接受经过符号链接的输入，请使用实际路径")
    return path.resolve(strict=True)


def assert_unchanged(record, info):
    if not stat.S_ISREG(info.st_mode) or signature(info) != record.stamp:
        raise ChangedFileError("文件在扫描期间发生变化，请停止修改后重新扫描")


@contextmanager
def checked_stream(record):
    """Pin POSIX parent directories and reject symlinks at every component."""
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    path = record.path.absolute()
    parents = []
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        try:
            parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            parents.append(parent)
            for component in path.parts[1:-1]:
                parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                parents.append(parent)
            fd = os.open(path.name, flags, dir_fd=parent)
        except BaseException:
            for handle in reversed(parents):
                os.close(handle)
            raise
    else:
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ChangedFileError("输入路径经过符号链接，请使用实际路径")
        fd = os.open(path, flags)
    try:
        assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or signature(opened)[:3] != record.stamp[:3]:
            raise ChangedFileError("打开的文件与扫描记录不一致，请重新扫描")
        # Windows stat/fstat can expose different timestamp precision. Compare
        # timestamps within the SAME API, while cross-checking identity and size.
        opened_record = ImageFile(record.path, record.size, signature(opened))
        # Keep fd alive even if an image decoder closes its supplied stream.
        with os.fdopen(os.dup(fd), "rb") as stream:
            yield stream
        assert_unchanged(opened_record, os.fstat(fd))
        assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
    finally:
        os.close(fd)
        for handle in reversed(parents):
            os.close(handle)


def exact_digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
        digest.update(block)
    return digest.hexdigest()


def feed_value(digest, value):
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    digest.update(struct.pack(">Q", len(data)))
    digest.update(data)


def pixel_digest(stream, options, pillow):
    """Hash every decoded frame, not a thumbnail or perceptual hash."""
    Image, ImageOps = pillow
    prefix = stream.read(29)
    stream.seek(0)
    # Pillow may silently down-convert multi-channel 16-bit PNGs to 8-bit.
    if (prefix[:8] == b"\x89PNG\r\n\x1a\n" and len(prefix) >= 26
            and prefix[24] == 16 and prefix[25] in {2, 4, 6}):
        raise ValueError("不以 8 位解码比较多通道 16 位 PNG；请使用 --mode exact")
    digest = hashlib.sha256(b"image-duplicate-finder:decoded-pixels:v1\0")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(stream) as image:
            if image.format == "ICO" and len(image.ico.sizes()) > 1:
                raise ValueError("多分辨率 ICO 不只包含当前解码尺寸；请使用 --mode exact")
            count = getattr(image, "n_frames", 1)
            if count > options.max_frames:
                raise ValueError(f"帧数 {count} 超过上限 {options.max_frames}；可使用 exact 模式")
            feed_value(digest, {"frames": count, "loop": image.info.get("loop") if count > 1 else None})
            total_pixels = 0
            for index in range(count):
                image.seek(index)
                width, height = image.size
                pixels = width * height
                total_pixels += pixels
                if pixels > options.max_pixels or total_pixels > options.max_total_pixels:
                    raise ValueError("图片像素数超过安全上限；请调整限制或使用 --mode exact")
                bits = getattr(image, "tag_v2", {}).get(258, ())
                if isinstance(bits, int):
                    bits = (bits,)
                if bits and max(bits) > 8 and image.mode in NORMALIZE_MODES:
                    raise ValueError("解码可能损失高位深 TIFF 精度；请使用 --mode exact")
                frame = ImageOps.exif_transpose(image)
                converted = None
                try:
                    if frame.mode in NORMALIZE_MODES:
                        converted = frame.convert("RGBA")
                        normalized = converted
                    else:
                        # Preserve scientific integer/float/CMYK data without lossy conversion.
                        normalized = frame
                    profile = frame.info.get("icc_profile") or b""
                    feed_value(digest, {
                        "size": normalized.size,
                        "mode": normalized.mode,
                        "duration_ms": float(image.info.get("duration", 0)) if count > 1 else None,
                        "icc_sha256": hashlib.sha256(profile).hexdigest(),
                    })
                    data = normalized.tobytes()
                    digest.update(struct.pack(">Q", len(data)))
                    digest.update(data)
                finally:
                    if converted is not None:
                        converted.close()
                    frame.close()
    return digest.hexdigest()


def similarity_module():
    if __package__:
        from . import image_similarity
    else:
        import image_similarity
    return image_similarity


class Scanner:
    def __init__(self, roots, options, pillow=None):
        self.roots = roots
        self.options = options
        self.pillow = pillow
        self.files = []
        self.errors = []
        self.skipped = defaultdict(int)
        self.seen_files = set()
        self.seen_dirs = set()
        self.fingerprinted = 0

    def error(self, path, operation, exc):
        self.errors.append({"path": str(path), "operation": operation,
                            "error_type": type(exc).__name__, "message": str(exc)})

    def add_file(self, path):
        if path in self.seen_files:
            return
        self.seen_files.add(path)
        if self.options.extensions is not None and path.suffix.lower() not in self.options.extensions:
            self.skipped["non_image_files"] += 1
            return
        try:
            info = os.stat(path, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                self.skipped["symlink_entries"] += 1
            elif stat.S_ISREG(info.st_mode):
                self.files.append(ImageFile(path, info.st_size, signature(info)))
            else:
                self.skipped["other_entries"] += 1
        except OSError as exc:
            self.error(path, "stat", exc)

    def walk(self, root):
        stack = [root]
        while stack:
            directory = stack.pop()
            if directory in self.seen_dirs:
                continue
            self.seen_dirs.add(directory)
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                self.error(directory, "scan", exc)
                continue
            subdirs = []
            for entry in entries:
                path = directory / entry.name
                try:
                    if not self.options.include_hidden and entry.name.startswith("."):
                        self.skipped["hidden_entries"] += 1
                    elif entry.is_symlink():
                        self.skipped["symlink_entries"] += 1
                    elif entry.is_dir(follow_symlinks=False):
                        if self.options.recursive:
                            subdirs.append(path)
                        else:
                            self.skipped["subdirectories"] += 1
                    elif entry.is_file(follow_symlinks=False):
                        self.add_file(path)
                    else:
                        self.skipped["other_entries"] += 1
                except OSError as exc:
                    self.error(path, "scan", exc)
            stack.extend(reversed(subdirs))

    def run(self):
        for root in self.roots:
            if root.is_dir():
                self.walk(root)
            else:
                # An explicitly selected hidden file/root is not filtered out.
                self.add_file(root)
        self.files.sort(key=lambda item: str(item.path))
        if self.options.mode == "similar":
            return similarity_module().scan(self, checked_stream, pixel_digest, assert_unchanged, VERSION)
        if self.options.mode == "exact":
            by_size = defaultdict(list)
            for record in self.files:
                by_size[record.size].append(record)
            candidates = [record for records in by_size.values() if len(records) > 1 for record in records]
        else:
            candidates = self.files
        by_digest = defaultdict(list)
        for record in candidates:
            try:
                with checked_stream(record) as stream:
                    digest = (exact_digest(stream) if self.options.mode == "exact"
                              else pixel_digest(stream, self.options, self.pillow))
                self.fingerprinted += 1
                by_digest[digest].append(record)
            except Exception as exc:
                # A single unreadable/corrupt/unsupported image must not abort the scan.
                self.error(record.path, "fingerprint", exc)
        matched = []
        for digest, records in by_digest.items():
            if len(records) < 2:
                continue
            stable = []
            for record in records:
                try:
                    assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
                    stable.append(record)
                except OSError as exc:
                    self.error(record.path, "verify", exc)
            if len(stable) > 1:
                matched.append((digest, sorted(stable, key=lambda item: str(item.path))))
        matched.sort(key=lambda pair: str(pair[1][0].path))
        groups = []
        hard_links = 0
        for number, (digest, records) in enumerate(matched, 1):
            members = []
            inodes = {}
            for record in records:
                inode = record.stamp[:2]
                linked_to = inodes.get(inode) if inode[1] else None
                if linked_to:
                    hard_links += 1
                if inode[1]:
                    inodes[inode] = inodes.get(inode, str(record.path))
                members.append({"path": str(record.path), "size_bytes": record.size,
                                "mtime_ns": record.stamp[3], "hard_link_to": linked_to})
            groups.append({"id": f"G{number:04d}", "digest": digest,
                           "reference": members[0]["path"], "files": members,
                           "extra_copy_logical_bytes": sum(item.size for item in records[1:])})
        return {
            "schema_version": 1,
            "tool": {"name": "image-duplicate-finder", "version": VERSION},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.options.mode,
            "algorithm": "sha256" if self.options.mode == "exact" else "decoded-pixels-sha256-v1",
            "complete": not self.errors,
            "inputs": [str(path) for path in self.roots],
            "options": {
                "recursive": self.options.recursive,
                "include_hidden": self.options.include_hidden,
                "extensions": sorted(self.options.extensions) if self.options.extensions is not None else "*",
                "max_pixels": self.options.max_pixels,
                "max_frames": self.options.max_frames,
                "max_total_pixels": self.options.max_total_pixels,
            },
            "summary": {
                "scanned_images": len(self.files), "fingerprinted_images": self.fingerprinted,
                "duplicate_groups": len(groups),
                "duplicate_images": sum(len(group["files"]) for group in groups),
                "extra_copies": sum(len(group["files"]) - 1 for group in groups),
                "extra_copy_logical_bytes": sum(group["extra_copy_logical_bytes"] for group in groups),
                "hard_link_duplicate_paths": hard_links,
                "errors": len(self.errors), "skipped": dict(sorted(self.skipped.items())),
            },
            "groups": groups, "errors": self.errors,
        }


def markdown_report(report):
    if report["mode"] == "similar":
        return similarity_module().markdown_report(report, display_text)
    def code(value):
        return "<code>" + html.escape(display_text(value)) + "</code>"

    summary = report["summary"]
    lines = ["# 本地图片查重报告", "", f"- 时间（UTC）：{report['generated_at']}",
             f"- 模式：{report['mode']} / {report['algorithm']}",
             f"- 扫描状态：{'完整' if report['complete'] else '不完整：存在读取或解码错误'}",
             f"- 图片候选：{summary['scanned_images']}；重复组：{summary['duplicate_groups']}；"
             f"额外副本：{summary['extra_copies']}",
             f"- 额外副本逻辑字节：{summary['extra_copy_logical_bytes']}（不等于可释放磁盘空间）", "",
             "参考副本仅按完整路径排序选取，不代表应保留或删除哪一张。原图没有被修改。", "",
             "## 扫描范围", ""]
    lines += ["- " + code(path) for path in report["inputs"]]
    lines += ["", "## 重复分组", ""]
    if not report["groups"]:
        lines += ["本次已成功处理的候选中未发现重复。若状态不完整，请先处理错误再下结论。", ""]
    for group in report["groups"]:
        lines += [f"### {group['id']}（{len(group['files'])} 个路径）", "",
                  "指纹：" + code(group["digest"]), ""]
        for index, item in enumerate(group["files"]):
            note = "参考" if index == 0 else "重复"
            line = f"- {note}：{code(item['path'])} — {item['size_bytes']} 字节"
            if item["hard_link_to"]:
                line += "；与 " + code(item["hard_link_to"]) + " 为同一硬链接文件"
            lines.append(line)
        lines.append("")
    lines += ["## 错误与限制", ""]
    if report["errors"]:
        for error in report["errors"]:
            lines.append("- " + code(error["path"]) + "：" + code(error["error_type"] + ": " + error["message"]))
    else:
        lines.append("- 本次未记录扫描错误。")
    lines += ["- exact 按扩展名筛选后比较原始文件字节，不验证文件是否可解码。",
              "- pixels 比较完整解码帧、EXIF 校正方向、动画时长/循环和 ICC 配置；不是近似图像检索。",
              "- 不同 ICC、原始高位深模式或解码器能力可能使视觉相同的图像不归为同组。",
              "- 隐藏项和符号链接默认跳过；详细计数及扫描选项见 JSON。",
              "- 硬链接、写时复制和文件系统压缩会影响磁盘占用；逻辑字节不是清理承诺。",
              "- 报告含本机完整路径；请勿未经许可公开上传。", ""]
    return "\n".join(lines)


def write_reports(report, directory, preview_png=None):
    """Only create a NEW directory. Never overwrite existing reports or images."""
    directory.mkdir(parents=True, exist_ok=False)
    if report["mode"] == "similar" and preview_png is None:
        report["preview"] = None
    with (directory / "duplicates.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with (directory / "duplicates.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        if report["mode"] == "similar":
            similarity_module().write_csv(writer, report)
        else:
            writer.writerow(["group_id", "role", "digest", "size_bytes", "path", "hard_link_to"])
            for group in report["groups"]:
                for index, item in enumerate(group["files"]):
                    # Absolute path cells cannot start an Excel formula.
                    writer.writerow([group["id"], "reference" if index == 0 else "duplicate",
                                     group["digest"], item["size_bytes"], item["path"], item["hard_link_to"] or ""])
    with (directory / "duplicates.md").open("x", encoding="utf-8") as stream:
        stream.write(markdown_report(report))
    if preview_png is not None:
        with (directory / "similarity-preview.png").open("xb") as stream:
            stream.write(preview_png)


def print_report(report):
    if report["mode"] == "similar":
        similarity_module().print_report(report, display_text)
        return
    summary = report["summary"]
    print(f"图片查重 | {report['mode']} | {'完整' if report['complete'] else '不完整'}")
    print(f"候选图片 {summary['scanned_images']}；重复组 {summary['duplicate_groups']}；"
          f"组内图片 {summary['duplicate_images']}；额外副本 {summary['extra_copies']}")
    print(f"额外副本逻辑字节：{summary['extra_copy_logical_bytes']}（非实际可释放磁盘空间）")
    for group in report["groups"]:
        print(f"\n{group['id']} | {len(group['files'])} 个路径 | {group['digest']}")
        for index, item in enumerate(group["files"]):
            role = "参考" if index == 0 else "重复"
            link = " [硬链接，不是独立存储副本]" if item["hard_link_to"] else ""
            print(f"  [{role}] {display_text(item['path'])} ({item['size_bytes']} B){link}")
    if not report["groups"]:
        print("已成功处理的候选中未发现重复；存在错误时不能认定整个目录没有重复。")
    print("跳过项目：" + json.dumps(summary["skipped"], ensure_ascii=False))
    for error in report["errors"]:
        print(f"错误 [{error['operation']}] {display_text(error['path'])}: "
              f"{display_text(error['message'])}", file=sys.stderr)
    print("只读扫描完成，未删除或修改任何原图。参考项不代表保留建议。")


def positive_integer(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return result


def similarity_threshold(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("相似度门槛必须是0～100的整数，推荐90") from exc
    if not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("相似度门槛必须在0～100之间，推荐90")
    return result


def parse_extensions(value):
    if value.strip() == "*":
        return None
    extensions = set()
    for item in value.split(","):
        item = item.strip().lower().lstrip(".")
        if not item or any(char in item for char in "/*?\\ \t\r\n"):
            raise argparse.ArgumentTypeError("扩展名应为逗号分隔的 jpg,png 等，或单独的 *")
        extensions.add("." + item)
    return extensions


def main(argv=None):
    # Deterministic UTF-8 also when Windows redirects output or Python runs -I.
    for output in (sys.stdout, sys.stderr):
        if output is not None and hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="backslashreplace")
    arguments = list(sys.argv[1:] if argv is None else argv)
    route = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    route.add_argument("--mode", choices=("exact", "pixels", "similar", "copynce"), default="exact")
    selected, remaining = route.parse_known_args(arguments)
    if selected.mode == "copynce":
        if __package__:
            from . import copynce_cli
        else:
            import copynce_cli
        return copynce_cli.main(remaining)
    parser = argparse.ArgumentParser(description="图片查重与相似检测：只读原图。模型选项见 --mode copynce --help。",
                                     allow_abbrev=False)
    parser.add_argument("paths", nargs="+", help="一个或多个本地文件夹/图片路径（含空格时加引号）")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--mode", choices=("exact", "pixels", "similar", "copynce"), default="exact",
                        help="exact=字节相同（默认）；pixels=像素相同；similar=整图相似；copynce=可选模型")
    parser.add_argument("--similarity-threshold", type=similarity_threshold, default=None,
                        help="similar唯一调参项：0～100整数，默认90；越高越严，100须严格像素相同")
    parser.add_argument("--no-recursive", action="store_false", dest="recursive", help="不扫描子文件夹")
    parser.add_argument("--include-hidden", action="store_true", help="包括隐藏文件/文件夹（仍不跟随符号链接）")
    parser.add_argument("--extensions", type=parse_extensions, default=set(DEFAULT_EXTENSIONS),
                        help="自定义扩展名，例如 jpg,png；* 包括所有文件，必须用引号包裹")
    parser.add_argument("--output-dir", type=Path, help="新建报告目录，写入 JSON/CSV/Markdown；已存在则拒绝覆盖")
    parser.add_argument("--max-pixels", type=positive_integer, default=40_000_000,
                        help="pixels/similar 单帧像素安全上限，默认 40000000")
    parser.add_argument("--max-frames", type=positive_integer, default=200,
                        help="pixels 模式帧数上限，默认 200")
    parser.add_argument("--max-total-pixels", type=positive_integer, default=100_000_000,
                        help="pixels 模式单文件所有帧累计像素上限，默认 100000000")
    options = parser.parse_args(arguments)
    if options.mode != "similar" and options.similarity_threshold is not None:
        parser.error("--similarity-threshold 仅适用于 --mode similar")
    if options.similarity_threshold is None:
        options.similarity_threshold = 90
    roots = []
    for value in options.paths:
        try:
            path = canonical_input(value)
            if not path.is_dir() and not path.is_file():
                parser.error("输入不是普通文件或文件夹：" + display_text(path))
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error("无法访问输入：" + display_text(value) + "；" + display_text(exc))
        roots.append(path)
    roots = sorted(set(roots), key=str)
    if options.output_dir is not None:
        options.output_dir = options.output_dir.expanduser().absolute()
        if os.path.lexists(options.output_dir):
            parser.error("报告目录已存在，不会覆盖：" + display_text(options.output_dir))
    pillow = None
    if options.mode in ("pixels", "similar"):
        try:
            from PIL import Image, ImageOps
        except ImportError:
            parser.error(f"{options.mode} 模式需要 Pillow。请用同一个 Python 安装：python3 -m pip install 'Pillow>=10.4.0'")
        pillow = (Image, ImageOps)
    if options.mode == "similar":
        try:
            similarity_module()
        except ImportError:
            parser.error("similar 模式需要 Pillow 和 NumPy；请在同一虚拟环境安装 requirements-similar.txt")
        if options.similarity_threshold < 80:
            print("提示：当前阈值低于建议范围80～95，误报和计算量可能增加；默认推荐90。", file=sys.stderr)
    scanner = Scanner(roots, options, pillow)
    report = scanner.run()
    print_report(report)
    if options.output_dir is not None:
        try:
            write_reports(report, options.output_dir, getattr(scanner, "preview_png", None))
        except OSError as exc:
            print("报告写入失败（可能留下不完整的新报告目录）：" + display_text(exc), file=sys.stderr)
            return 2
        print("报告已保存：" + display_text(options.output_dir))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n扫描已中断，未修改原图；本次结果未完整生成。", file=sys.stderr)
        raise SystemExit(130)
