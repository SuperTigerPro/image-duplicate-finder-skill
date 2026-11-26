#!/usr/bin/env python3
"""One-shot JSON adapter; model, remote profiles and uploads require host grants."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import sys
from pathlib import Path, PurePosixPath

try:
    from . import find_duplicates as finder
except ImportError:
    import find_duplicates as finder

MAX_REQUEST_BYTES = 1024 * 1024
FIELDS = {"paths", "mode", "output_dir", "recursive", "include_hidden"}
MODEL_FIELDS = {"backend", "model_threshold", "whole_only", "search_mode", "top_k", "remote_profile", "remote_directory"}
OPTIONAL_FIELDS = {"similarity_threshold"} | MODEL_FIELDS


def inside(path, root):
    return path == root or path.is_relative_to(root)


def run_request(request, allowed_roots, report_root, *, allow_model=False, remote_profiles=None,
                remote_roots=None, allow_upload=False, model_offline=False):
    """Host-controlled roots cannot be expanded by model-supplied JSON."""
    if (not isinstance(request, dict) or not FIELDS <= set(request)
            or not set(request) <= FIELDS | OPTIONAL_FIELDS):
        raise ValueError("请求缺少必填字段，或包含不支持的字段；认证信息不能由工具参数传入")
    model = request["mode"] == "copynce"
    if model and not allow_model:
        raise ValueError("宿主尚未授权模型运行；需要宿主启用 --allow-model")
    if not model and set(request) & MODEL_FIELDS:
        raise ValueError("模型选项仅适用于 copynce 模式")
    backend = request.get("backend", "local")
    if backend not in ("local", "cloud"):
        raise ValueError("backend 必须是 local 或 cloud")
    remote = model and backend == "cloud" and "remote_directory" in request
    paths = request["paths"]
    if not isinstance(paths, list) or (not paths and not remote) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("paths 必须是非空路径字符串数组")
    if request["mode"] not in ("exact", "pixels", "similar", "copynce"):
        raise ValueError("mode 必须是 exact、pixels、similar 或 copynce")
    threshold = request.get("similarity_threshold")
    if "similarity_threshold" in request:
        if type(threshold) is not int or not 0 <= threshold <= 100:
            raise ValueError("相似度阈值必须是0～100的整数，推荐90")
        if request["mode"] != "similar":
            raise ValueError("similarity_threshold 仅适用于 similar 模式")
    if not isinstance(request["output_dir"], str) or not request["output_dir"]:
        raise ValueError("output_dir 必须是新报告目录的路径")
    if type(request["recursive"]) is not bool or type(request["include_hidden"]) is not bool:
        raise ValueError("recursive 和 include_hidden 必须是布尔值")
    canonical_inputs = []
    for value in paths:
        path = finder.canonical_input(value)
        if not any(inside(path, root) for root in allowed_roots):
            raise ValueError("输入路径超出宿主已授权的扫描范围：" + str(path))
        canonical_inputs.append(str(path))
    output = Path(request["output_dir"]).expanduser().resolve()
    if not inside(output, report_root) or output == report_root:
        raise ValueError("output_dir 必须是宿主报告根目录内的新子目录")
    arguments = ["--mode", request["mode"], "--output-dir", str(output)]
    if threshold is not None:
        arguments += ["--similarity-threshold", str(threshold)]
    if not request["recursive"]:
        arguments.append("--no-recursive")
    if request["include_hidden"]:
        arguments.append("--include-hidden")
    if model:
        arguments += ["--backend", backend, "--summary-only"]
        threshold = request.get("model_threshold", .75)
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
            raise ValueError("model_threshold 必须是 -1 至 1 的有限余弦分数")
        top_k = request.get("top_k", 100)
        if type(top_k) is not int or not 1 <= top_k <= 20000:
            raise ValueError("top_k 必须是 1 至 20000 的整数")
        search = request.get("search_mode", "auto")
        if search not in ("auto", "exact", "indexed"):
            raise ValueError("search_mode 必须是 auto、exact 或 indexed")
        whole = request.get("whole_only", False)
        if type(whole) is not bool:
            raise ValueError("whole_only 必须是布尔值")
        arguments += ["--threshold", str(threshold), "--top-k", str(top_k), "--search-mode", search]
        if whole:
            arguments.append("--whole-only")
        if backend == "local":
            if "remote_profile" in request or "remote_directory" in request:
                raise ValueError("远端配置仅用于 cloud 模式")
            if model_offline:
                arguments.append("--offline")
        else:
            name = request.get("remote_profile")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("请使用宿主授权的匿名 remote_profile ID")
            profiles = remote_profiles or {}
            if name not in profiles:
                raise ValueError("远端连接不在宿主已授权的 profile 清单中")
            arguments += ["--profile", str(profiles[name])]
            if remote:
                directory = request["remote_directory"]
                if paths or request["include_hidden"] or not isinstance(directory, str) or not directory:
                    raise ValueError("远端目录不能与本机路径或隐藏项选项混用")
                chosen = PurePosixPath(directory)
                allowed = (remote_roots or {}).get(name, ())
                if (".." in chosen.parts or "\\" in directory or directory.startswith("~")
                        or not any(chosen == PurePosixPath(root) or chosen.is_relative_to(PurePosixPath(root))
                                   for root in allowed)):
                    raise ValueError("远端目录超出宿主授权范围")
                arguments += ["--remote-directory", directory, "--require-strict-remote"]
            elif not allow_upload:
                raise ValueError("宿主尚未授权向该服务器上传本机图片")
            else:
                arguments.append("--allow-upload")
    arguments += ["--"] + canonical_inputs
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            exit_code = finder.main(arguments)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 2
    # Existing reports must never be returned as a new scan's result.
    report = None
    if exit_code in (0, 1):
        report = json.loads((output / "duplicates.json").read_text(encoding="utf-8"))
    return {"ok": exit_code == 0, "exit_code": exit_code, "report": report,
            "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="从 stdin 接收工具参数 JSON，仅处理宿主授权的本地路径")
    parser.add_argument("--allow-root", action="append", required=True, type=Path,
                        help="宿主授权的输入根目录或文件；可重复。绝不能由模型自行扩大")
    parser.add_argument("--report-root", required=True, type=Path,
                        help="宿主授权的报告根目录；每次请求须使用其中的新子目录")
    parser.add_argument("--allow-model", action="store_true", help="宿主确认本次允许使用模型和准备其依赖")
    parser.add_argument("--model-offline", action="store_true", help="模型只使用已有环境和权重")
    parser.add_argument("--remote-profile", action="append", default=[], metavar="ID=FILE",
                        help="宿主授权的匿名ID与仓库外私有profile；不能来自模型参数")
    parser.add_argument("--allow-remote-root", action="append", default=[], metavar="ID=DIR",
                        help="本次可扫描的远端目录；可重复")
    parser.add_argument("--allow-upload", action="store_true", help="宿主已取得本次选定文件向已授权服务器传输的许可")
    options = parser.parse_args()
    try:
        roots = [path.expanduser().resolve(strict=True) for path in options.allow_root]
        report_root = options.report_root.expanduser().resolve()
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("请求 JSON 超过 1 MiB 上限")
        request = json.loads(raw.decode("utf-8-sig"))
        profiles = {}
        remote_roots = {}
        for value in options.remote_profile:
            name, sep, value = value.partition("=")
            if not sep or not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("remote-profile 需要 ID=FILE")
            profiles[name] = Path(value).expanduser().resolve()
        for value in options.allow_remote_root:
            name, sep, value = value.partition("=")
            if not sep or not value or name not in profiles:
                raise ValueError("allow-remote-root 需要已授权的 ID=DIR")
            remote_roots.setdefault(name, []).append(value)
        result = run_request(request, roots, report_root, allow_model=options.allow_model,
                             remote_profiles=profiles, remote_roots=remote_roots, allow_upload=options.allow_upload,
                             model_offline=options.model_offline)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        result = {"ok": False, "exit_code": 2, "report": None,
                  "stdout": "", "stderr": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "exit_code": 130, "report": None,
                          "stdout": "", "stderr": "用户中断，结果不完整"}, ensure_ascii=False))
        raise SystemExit(130)
