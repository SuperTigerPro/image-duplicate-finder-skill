"""CLI for local and SSH-backed CopyNCE scanning. Loading the model is opt-in."""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import find_duplicates as core
    from .copynce_report import build_report, report_lines, write_report
else:
    import find_duplicates as core
    from copynce_report import build_report, report_lines, write_report


def _module(name):
    return importlib.import_module(f".{name}", __package__) if __package__ else importlib.import_module(name)


def cosine(value):
    value = float(value)
    if not math.isfinite(value) or not -1 <= value <= 1:
        raise argparse.ArgumentTypeError("原始余弦阈值须在 -1 至 1 之间")
    return value


def parse(argv):
    p = argparse.ArgumentParser(description="使用 CopyNCE Descriptor 核查整图及局部复用候选", allow_abbrev=False)
    p.add_argument("paths", type=Path, nargs="*")
    p.add_argument("--backend", choices=("local", "cloud"), default="local")
    p.add_argument("--weights", type=Path, help="指定官方权重文件")
    p.add_argument("--offline", action="store_true", help="仅使用本机已有依赖与权重")
    p.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    p.add_argument("--batch-size", type=core.positive_integer, default=8)
    p.add_argument("--whole-only", action="store_true", help="只比较完整图片")
    p.add_argument("--threshold", type=cosine, default=.75)
    p.add_argument("--top-k", type=core.positive_integer, default=100)
    p.add_argument("--search-mode", choices=("auto", "exact", "indexed"), default="auto")
    p.add_argument("--no-recursive", action="store_false", dest="recursive")
    p.add_argument("--include-hidden", action="store_true")
    p.add_argument("--extensions", type=core.parse_extensions, default=argparse.SUPPRESS)
    p.add_argument("--max-pixels", type=core.positive_integer, default=40_000_000)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--summary-only", action="store_true", help="终端仅显示统计，完整路径仅写入私有报告")
    p.add_argument("--profile", type=Path, help="本机仓库以外的私有 SSH 配置")
    p.add_argument("--remote-directory", help="服务器图库内的目录")
    p.add_argument("--require-strict-remote", action="store_true", help=argparse.SUPPRESS)
    actions = p.add_mutually_exclusive_group()
    actions.add_argument("--allow-upload", action="store_true", help="已获准向所选服务器传输这些图片")
    actions.add_argument("--plan-upload", action="store_true", help="仅预检传输的目的地、数量和字节数")
    actions.add_argument("--cleanup", action="store_true", help="清理远端已过期的临时图库")
    actions.add_argument("--cleanup-job", metavar="JOB_ID", help="重连后确认清理指定的自有临时任务（匿名任务ID）")
    p.add_argument("--upload-original", action="store_true", help="明确传输原文件字节（包含EXIF等元数据）；默认上传去元数据推理副本")
    args = p.parse_args(argv)
    if args.top_k > 20000:
        p.error("--top-k 超过模型安全上限20000")
    remote = args.remote_directory is not None
    if args.require_strict_remote and not (args.backend == "cloud" and remote):
        p.error("严格服务器路径校验仅用于 --backend cloud --remote-directory")
    cleaning = args.cleanup or args.cleanup_job is not None
    if args.backend == "local" and (remote or args.allow_upload or args.plan_upload or cleaning or args.profile
                                     or args.upload_original):
        p.error("远程连接或上传选项仅用于 --backend cloud")
    if args.backend == "cloud":
        if not args.profile:
            p.error("请用 --profile 指定本机的私有 SSH 配置")
        if args.weights or args.offline or args.device != "auto" or args.batch_size != 8 or args.max_pixels != 40_000_000:
            p.error("--weights、--offline、--device、--batch-size 和 --max-pixels 仅用于本地模型")
        if cleaning:
            if args.paths or remote or args.output_dir:
                p.error("--cleanup 仅清理过期任务，请勿指定图库或报告目录")
        elif remote:
            if not args.remote_directory.strip() or args.paths or args.plan_upload or args.allow_upload:
                p.error("选择远端目录时请只提供 --remote-directory DIR")
            if args.include_hidden or hasattr(args, "extensions"):
                p.error("已有远端图库的隐藏项和扩展名由服务器控制")
        else:
            if not (args.allow_upload or args.plan_upload):
                p.error("本地图库需要 --plan-upload 预检或 --allow-upload 明确授权")
            allowed = _module("copynce_cloud").UPLOAD_EXTENSIONS
            if hasattr(args, "extensions") and (args.extensions is None or not args.extensions <= allowed):
                p.error("上传仅允许可验证的图像格式，不能指定通配符或其他文件格式")
            if not hasattr(args, "extensions"):
                args.extensions = allowed
        if args.upload_original and not (args.allow_upload or args.plan_upload):
            p.error("--upload-original 仅用于已授权或预检的本机图库传输")
    if not hasattr(args, "extensions"):
        args.extensions = core.DEFAULT_EXTENSIONS
    if not cleaning and not remote and not args.paths:
        p.error("请指定要扫描的本地图片或目录")
    if args.plan_upload and args.output_dir:
        p.error("--plan-upload 只在终端展示传输范围")
    if args.output_dir is not None:
        args.output_dir = args.output_dir.expanduser().absolute()
        resolved = args.output_dir.resolve()
        repo = Path(__file__).resolve().parents[1]
        if resolved == repo or repo in resolved.parents or any((p / ".git").exists() for p in (resolved, *resolved.parents)):
            p.error("模型报告必须保存在 Skill 目录及所有 Git 工作树之外")
        if os.path.lexists(args.output_dir):
            p.error("报告目录已存在，请选择新目录")
    return args


def select_inputs(args):
    roots = []
    for item in args.paths:
        path = core.canonical_input(item)
        if not (path.is_dir() or path.is_file()):
            raise ValueError("输入需要是普通文件或目录")
        roots.append(path)
    scanner = core.Scanner(sorted(set(roots), key=str), args)
    upload = args.backend == "cloud"
    for root in scanner.roots:
        if root.is_dir():
            scanner.walk(root)
        elif upload and root.suffix.lower() not in args.extensions:
            scanner.error(root, "upload-format", ValueError("文件扩展名不在允许的图片范围内"))
        else:
            scanner.add_file(root)
    if upload:
        validate = _module("copynce_cloud").validate_upload_image
        accepted = []
        for item in scanner.files:
            try:
                with core.checked_stream(item) as stream:
                    validate(stream, item.path.suffix)
            except (OSError, ValueError) as err:
                scanner.error(item.path, "upload-format", err)
            else:
                accepted.append(item)
        scanner.files = accepted
    return scanner


def _worker():
    req = json.load(sys.stdin)
    raw = _module("copynce_engine").scan(list(map(Path, req["paths"])), weights=Path(req["weights"]),
                                          cache=Path(req["cache"]), extensions=None,
                                          file_stamps=req["file_stamps"], **req["options"])
    raw.pop("inputs", None)
    print(json.dumps(raw, ensure_ascii=True, allow_nan=False))


def local_scan(records, args, options):
    setup = _module("copynce_setup")
    python, weights = setup.prepare(weights=args.weights, offline=args.offline, device=args.device)
    req = {"paths": list(records), "file_stamps": {name: item.stamp for name, item in records.items()},
           "weights": str(weights), "cache": str(setup.user_cache() / "descriptors.sqlite3"),
           "options": dict(options, device=args.device, batch_size=args.batch_size, max_pixels=args.max_pixels)}
    command = [str(python), str(Path(__file__).resolve()), "--local-worker"]
    proc = subprocess.run(command, input=json.dumps(req), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode == 130:
        raise KeyboardInterrupt
    if proc.returncode:
        diag = proc.stderr.strip().splitlines()
        raise RuntimeError("本地推理失败：" + (diag[-1][-1000:] if diag else str(proc.returncode)))
    return json.loads(proc.stdout)


def empty_result():
    return {"matches": [], "errors": [], "error_count": 0, "mode_used": "exact", "exhaustive": True,
            "truncated": False, "candidate_file_count": 0, "image_count": 0, "view_count": 0,
            "pairs_possible": 0, "pairs_assessed": 0, "pairs_above_threshold": 0, "pairs_returned": 0}


def main(argv=None):
    args = parse(argv)
    try:
        if args.cleanup_job is not None:
            _module("copynce_cloud").cleanup_job(args.profile, args.cleanup_job)
            print("已确认指定临时任务已清理。")
            return 0
        if args.cleanup:
            print(f"已清理过期任务数：{_module('copynce_cloud').cleanup(args.profile)}")
            return 0
        opts = dict(tiles=not args.whole_only, threshold=args.threshold, top_k=args.top_k,
                    mode=args.search_mode, recursive=args.recursive)
        remote = args.remote_directory is not None
        scanner = None if remote else select_inputs(args)
        records = {} if remote else {str(item.path): item for item in scanner.files}
        if args.plan_upload:
            _module("copynce_cloud").load_profile(args.profile)
            print(json.dumps({"destination": "selected-private-server", "inputs": list(map(str, scanner.roots)),
                              "cleanup": "remove this task's temporary copies; reconnect to verify interrupted cleanup",
                              "transfer_format": "original-bytes-including-metadata" if args.upload_original else "metadata-free-rgb-png",
                              "skipped": dict(scanner.skipped), "extensions": sorted(args.extensions),
                              "files": len(records), "bytes": sum(r.size for r in records.values()),
                              "errors": scanner.errors, "uploaded": False}, ensure_ascii=True))
            return int(bool(scanner.errors))
        if not remote and not records:
            raw = empty_result()
        elif args.backend == "local":
            raw = local_scan(records, args, opts)
        else:
            policy = {"require_strict_remote": True} if remote else {}
            if args.allow_upload:
                policy["file_stamps"] = {name: record.stamp for name, record in records.items()}
                policy["upload_original"] = args.upload_original
            raw = _module("copynce_cloud").cloud_scan(args.profile, paths=[r.path for r in records.values()],
                                                    remote_directory=args.remote_directory,
                                                    allow_upload=args.allow_upload, **opts, **policy)
        if records and any(row[f"path_{side}"] not in records for row in raw["matches"] for side in ("a", "b")):
            raise ValueError("返回了本次输入范围之外的图片路径")
        inputs = [args.remote_directory] if remote else scanner.roots
        options = dict(opts, source="remote-directory" if remote else "local-files")
        if not remote:
            options.update(include_hidden=args.include_hidden,
                           extensions=None if args.extensions is None else sorted(args.extensions))
        if args.backend == "local":
            options.update(max_pixels=args.max_pixels, requested_device=args.device, batch_size=args.batch_size)
        report = build_report(raw, args.backend, inputs, options,
                              () if remote else scanner.errors, {} if remote else scanner.skipped)
        if args.output_dir:
            write_report(args.output_dir, report, records)
        for line in report_lines(report, include_paths=not args.summary_only and args.backend == "local"):
            print(line)
        return 0 if report["complete"] else 1
    except KeyboardInterrupt:
        print("已取消，本次扫描未完成。", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as err:
        print("模型查重失败：" + core.display_text(err), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if sys.argv[1:] == ["--local-worker"]:
        try:
            _worker()
        except KeyboardInterrupt:
            sys.exit(130)
        except Exception as err:
            print("推理错误：" + core.display_text(err), file=sys.stderr)
            sys.exit(2)
    else:
        sys.exit(main())
