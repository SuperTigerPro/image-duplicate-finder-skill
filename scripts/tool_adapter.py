#!/usr/bin/env python3
"""Provider-neutral, one-shot JSON adapter. No SDK, network, or shell execution."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

try:
    from . import find_duplicates as finder
except ImportError:
    import find_duplicates as finder

MAX_REQUEST_BYTES = 1024 * 1024
FIELDS = {"paths", "mode", "output_dir", "recursive", "include_hidden"}
OPTIONAL_FIELDS = {"similarity_threshold"}


def inside(path, root):
    return path == root or path.is_relative_to(root)


def run_request(request, allowed_roots, report_root):
    """Host-controlled roots cannot be expanded by model-supplied JSON."""
    if (not isinstance(request, dict) or not FIELDS <= set(request)
            or not set(request) <= FIELDS | OPTIONAL_FIELDS):
        raise ValueError("需要 paths、mode、output_dir、recursive、include_hidden；仅允许额外的 similarity_threshold")
    paths = request["paths"]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("paths 必须是非空路径字符串数组")
    if request["mode"] not in ("exact", "pixels", "similar"):
        raise ValueError("mode 必须是 exact、pixels 或 similar")
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
        path = Path(value).expanduser()
        if path.is_symlink():
            raise ValueError("不接受符号链接输入")
        path = path.resolve(strict=True)
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
    options = parser.parse_args()
    try:
        roots = [path.expanduser().resolve(strict=True) for path in options.allow_root]
        report_root = options.report_root.expanduser().resolve()
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("请求 JSON 超过 1 MiB 上限")
        request = json.loads(raw.decode("utf-8-sig"))
        result = run_request(request, roots, report_root)
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
