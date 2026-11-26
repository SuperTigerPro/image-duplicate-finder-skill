"""Reports for verified CopyNCE similarities and their best matching windows."""
from __future__ import annotations

import csv
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .find_duplicates import VERSION, checked_stream, display_text, assert_unchanged
else:
    from find_duplicates import VERSION, checked_stream, display_text, assert_unchanged


def _pair(row, index):
    score = row["score"]
    if type(score) not in (float, int) or not math.isfinite(score) or not -1 <= score <= 1:
        raise ValueError("无效的余弦分数")
    output = {"id": f"M{index:05}", "score": score}
    whole = []
    for side in ("a", "b"):
        path, size, box = [row[f"{key}_{side}"] for key in ("path", "size", "box")]
        if (not isinstance(path, str) or not path or len(size) != 2 or len(box) != 4
                or any(type(n) is not int for n in (*size, *box))):
            raise ValueError("无效的图像路径、尺寸或坐标")
        w, h = size
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError("返回的窗口超出图片范围")
        output.update({f"path_{side}": path, f"size_{side}": list(size), f"box_{side}": list(box)})
        whole.append(tuple(box) == (0, 0, w, h))
    output["match_type"] = "whole-whole" if all(whole) else "whole-region" if any(whole) else "region-region"
    return output


def build_report(raw, backend, inputs, options, enumeration_errors=(), skipped=None):
    if raw.get("schema_version") == 4:
        return _enhanced_report(raw, backend, inputs, options, enumeration_errors, skipped)
    if (not isinstance(raw.get("errors"), list) or any(not isinstance(e, dict) for e in raw["errors"])
            or type(raw.get("error_count")) is not int or raw["error_count"] != len(raw["errors"])):
        raise ValueError("模型返回的错误清单与错误计数不一致")
    pairs = [_pair(row, i) for i, row in enumerate(raw["matches"], 1)]
    summary = {k: raw[k] for k in ("candidate_file_count", "image_count", "view_count", "pairs_possible",
                                    "pairs_assessed", "pairs_returned", "pairs_above_threshold")}
    if (any(type(v) is not int or v < 0 for v in summary.values())
            or len(pairs) != summary["pairs_returned"] or not 0 <= raw["pairs_assessed"] <= raw["pairs_possible"]
            or not len(pairs) <= raw["pairs_above_threshold"] <= raw["pairs_assessed"]
            or raw["exhaustive"] != (raw["pairs_assessed"] == raw["pairs_possible"])
            or raw["truncated"] != (raw["pairs_above_threshold"] > len(pairs))):
        raise ValueError("模型返回的图对数量与检索范围不一致")
    seen = set()
    for pair in pairs:
        key = tuple(sorted((pair["path_a"], pair["path_b"])))
        if key[0] == key[1] or key in seen or pair["score"] < options.get("threshold", -1):
            raise ValueError("模型返回了自身、重复或低于阈值的图对")
        seen.add(key)
    skipped_total = dict(skipped or {})
    for reason, n in (raw.get("skipped") or {}).items():
        skipped_total[reason] = skipped_total.get(reason, 0) + n
    errors = list(enumeration_errors) + raw["errors"]
    count = len(enumeration_errors) + raw["error_count"]
    rejected = sum(err.get("operation") == "upload-format" for err in enumeration_errors)
    summary["candidate_file_count"] += rejected
    summary.update(errors=count, skipped=skipped_total, rejected_upload_files=rejected)
    for key in ("cache_hits", "cache_misses", "max_views_per_image"):
        if key in raw:
            if type(raw[key]) is not int or raw[key] < 0:
                raise ValueError("无效的特征缓存或裁块统计")
            summary[key] = raw[key]
    warnings = ["实验性基础模型：余弦分数不是重复概率，阈值需用代表性正负样例校准。",
                "候选窗口对应 EXIF 校正后的像素坐标 [x0,y0,x1,y1)，不是精确复用边界。",
                "完整检索仅覆盖本次生成的整图和固定裁块；不保证任意裁剪、极小嵌图或单图内部克隆均可检出。"]
    if not raw["exhaustive"]:
        warnings.append("仅部分图对参与检索，可能遗漏相似图片。")
    if raw["truncated"]:
        warnings.append("达到返回上限，调大 top_k 可查看更多命中。")
    if count:
        warnings.append("部分输入未能处理，详见 errors。")
    warnings += [str(item) for item in (raw.get("fallback"), *(raw.get("device_fallbacks") or [])) if item]
    return {"schema_version": 3, "tool": {"name": "image-duplicate-finder-skill", "version": VERSION},
            "generated_at": datetime.now(timezone.utc).isoformat(), "mode": "copynce",
            "algorithm": "copynce-descriptor-vits16-cosine", "backend": backend, "score_type": "raw_cosine",
            "device": raw.get("device"), "weights_sha256": raw.get("weights_sha256"), "mode_used": raw["mode_used"],
            "model_id": raw.get("model_id", "copynce-descriptor-vits16-224"),
            "pipeline": raw.get("pipeline"), "precision": raw.get("precision", "float32"),
            "safety_limits": raw.get("safety_limits"),
            "options": options, "inputs": list(map(str, inputs)), "complete": count == 0,
            "files_complete": count == 0,
            "exhaustive": raw["exhaustive"], "truncated": raw["truncated"],
            "all_matches_reported": count == 0 and raw["exhaustive"] and not raw["truncated"],
            "summary": summary, "warnings": warnings, "errors": errors, "matches": pairs, "preview": None}


def _enhanced_report(raw, backend, inputs, options, enumeration_errors, skipped):
    """Validate stage accounting before presenting classifier scores as verified matches."""
    if raw.get("profile") not in {"enhanced", "deep"}:
        raise ValueError("无效的增强流程标识")
    errors = raw.get("errors")
    if (not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors)
            or type(raw.get("error_count")) is not int or raw["error_count"] != len(errors)):
        raise ValueError("模型返回的错误清单与错误计数不一致")
    summary = {k: raw[k] for k in ("candidate_file_count", "image_count", "view_count", "pairs_possible",
                                  "pairs_assessed", "pairs_returned", "pairs_above_threshold")}
    if any(type(v) is not int or v < 0 for v in summary.values()):
        raise ValueError("无效的增强图对计数")
    if (not 0 <= summary["pairs_assessed"] <= summary["pairs_possible"]
            or not 0 <= summary["pairs_returned"] <= summary["pairs_above_threshold"] <= summary["pairs_assessed"]
            or raw.get("exhaustive") != (summary["pairs_assessed"] == summary["pairs_possible"])
            or raw.get("truncated") != (summary["pairs_above_threshold"] > summary["pairs_returned"])):
        raise ValueError("增强流程的核验范围或返回计数不一致")
    stages = raw.get("stage_totals")
    if not isinstance(stages, dict) or any(type(v) not in (int, bool) or v < 0 for v in stages.values()):
        raise ValueError("无效的增强阶段计数")
    if stages.get("matcher_verified") != summary["pairs_assessed"]:
        raise ValueError("Matcher核验数与图对计数不一致")
    pending = stages.get("matcher_pending")
    failed = stages.get("matcher_failed")
    planned = stages.get("planned_candidates")
    if (any(type(v) is not int or v < 0 for v in (pending, failed, planned))
            or planned != pending + failed + summary["pairs_assessed"]):
        raise ValueError("Matcher待核验清单与阶段计数不一致")
    budget_complete = raw.get("budget_complete")
    if (type(budget_complete) is not bool
            or budget_complete != (pending == 0 and not stages.get("candidate_limit_reached", False))):
        raise ValueError("复核预算完成标记不一致")
    threshold = options.get("matcher_threshold", raw.get("matcher_threshold", .5))
    if type(threshold) not in (float, int) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("无效的Matcher分类阈值")
    pairs, seen = [], set()
    scalar_fields = ("descriptor_cosine", "local_cosine", "sn_a_to_b", "sn_b_to_a", "matcher_score")
    for index, row in enumerate(raw["matches"], 1):
        pair = _pair(row, index)
        for field in scalar_fields:
            value = row.get(field)
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError("增强图对缺少独立的有限分数")
            if field in {"descriptor_cosine", "local_cosine"} and not -1 <= value <= 1:
                raise ValueError("无效的Descriptor余弦分数")
            pair[field] = value
        if not threshold <= pair["matcher_score"] <= 1 or pair["score"] != pair["matcher_score"]:
            raise ValueError("未经有效Matcher核验的图对不能作为增强命中")
        key = tuple(sorted((pair["path_a"], pair["path_b"])))
        if key[0] == key[1] or key in seen:
            raise ValueError("返回自身或重复图对")
        seen.add(key)
        for field in ("matcher_whole", "matcher_local", "matcher_comparisons", "verification_views",
                      "candidate_channels", "candidate_directions", "matcher_evidence", "matcher_profile",
                      "matcher_a_to_b", "matcher_b_to_a", "matcher_verified", "matcher_direction",
                      "matcher_view_policy", "box_source", "candidate_tier", "scheduling_query",
                      "matcher_cache_hit", "matcher_cached_device"):
            if field in row:
                pair[field] = row[field]
        pairs.append(pair)
    if len(pairs) != summary["pairs_returned"]:
        raise ValueError("增强返回计数与清单不一致")
    skipped_total = dict(skipped or {})
    for reason, count in (raw.get("skipped") or {}).items():
        skipped_total[reason] = skipped_total.get(reason, 0) + count
    all_errors = list(enumeration_errors) + errors
    summary.update(errors=len(all_errors), skipped=skipped_total)
    for field in ("cache_hits", "cache_misses", "max_views_per_image"):
        if field in raw:
            summary[field] = raw[field]
    files_complete = not any(e.get("operation") not in {"matcher"} for e in all_errors)
    complete = not all_errors and budget_complete
    warnings = ["实验性基础模型：Matcher分类分数尚未经过领域概率校准，需人工复核。",
                "SN仅修正全局Descriptor的查询侧背景偏置；不会改变单个查询的参考排名，也不修正Matcher分数。",
                "局部视图、候选合并及预算调度属于Skill扩展；本流程不等于完整论文复现。",
                "窗口对应EXIF校正后的比较区域，不是精确复制边界；不保证任意裁剪或极小嵌图均被召回。"]
    if not raw["exhaustive"]:
        warnings.append("未对所有可能图对完成Matcher核验；候选范围之外仍可能有遗漏。")
    if pending:
        warnings.append(f"尚有{pending}个计划候选未核验：预算耗尽，本次增强任务未完成。")
    if raw["truncated"]:
        warnings.append("已核验命中超过返回上限；调大top_k可查看更多结果。")
    if all_errors:
        warnings.append("部分处理失败，详见errors。")
    warnings.extend(str(x) for x in raw.get("device_fallbacks", []))
    warnings.extend(str(x) for x in raw.get("matcher_device_fallbacks", []))
    return {"schema_version": 4, "tool": {"name": "image-duplicate-finder-skill", "version": VERSION},
            "generated_at": datetime.now(timezone.utc).isoformat(), "mode": "copynce", "backend": backend,
            "algorithm": "copynce-descriptor-official-sn-matcher", "profile": raw["profile"],
            "score_type": "classifier_sigmoid", "score_types": {"descriptor_cosine": "raw_cosine",
                "local_cosine": "max_view_raw_cosine", "sn_a_to_b": "official_query_bias_subtracted",
                "sn_b_to_a": "official_query_bias_subtracted", "matcher_score": "classifier_sigmoid"},
            "weights_sha256": raw.get("weights_sha256"), "matcher_weights_sha256": raw.get("matcher_weights_sha256"),
            "sn_index_fingerprint": raw.get("sn_index_fingerprint"),
            "sn_parameters": {"profile": "official-sn-code", "alpha": 1., "start": 1, "end": 5},
            "sn": raw.get("sn", {}), "matcher_threshold_calibrated": False,
            "matcher_aggregation": raw.get("matcher_aggregation", "bidirectional-max"),
            "matcher_cache_enabled": raw.get("matcher_cache_enabled", False),
            "matcher_cache_devices": raw.get("matcher_cache_devices", []),
            "matcher_cache_policy": raw.get("matcher_cache_policy"),
            "device": raw.get("device"), "stage_devices": raw.get("stage_devices", {
                "descriptor": raw.get("device"), "sn": "cpu", "matcher": raw.get("matcher_device")}),
            "precision": raw.get("precision", "float32"), "pipeline": raw.get("pipeline"),
            "mode_used": raw["mode_used"], "options": options, "inputs": list(map(str, inputs)),
            "files_complete": files_complete, "budget_complete": budget_complete, "complete": complete,
            "exhaustive": raw["exhaustive"], "truncated": raw["truncated"],
            "all_matches_reported": complete and raw["exhaustive"] and not raw["truncated"],
            "stage_totals": stages, "safety_limits": raw.get("safety_limits"),
            "summary": summary, "warnings": warnings, "errors": all_errors, "matches": pairs, "preview": None}


def write_html_preview(directory, report, records):
    rows = []
    for pair in report["matches"][:12]:
        pictures = []
        try:
            for side in ("a", "b"):
                record = records[pair[f"path_{side}"]]
                assert_unchanged(record, record.path.lstat())
                w, h = pair[f"size_{side}"]
                x0, y0, x1, y1 = pair[f"box_{side}"]
                uri = html.escape(record.path.as_uri(), quote=True)
                pictures.append(f'<div class="pic"><img src="{uri}" alt="{side}">'
                                f'<svg viewBox="0 0 {w} {h}"><rect x="{x0}" y="{y0}" '
                                f'width="{x1-x0}" height="{y1-y0}"/></svg></div>')
        except (KeyError, OSError) as err:
            report["warnings"].append(f"{pair['id']} 预览不可用：{err}")
            continue
        label = "Matcher" if report.get("schema_version") == 4 else "cosine"
        rows.append(f"<p>{pair['id']} · {label} {pair['score']:.4f}</p>" + "".join(pictures))
    if rows:
        style = (".pic{position:relative;display:inline-block;width:46%;margin-right:1%;vertical-align:middle}"
                 "img{width:100%;display:block;image-orientation:from-image}svg{position:absolute;inset:0;"
                 "width:100%;height:100%}rect{fill:none;stroke:red;stroke-width:3;vector-effect:non-scaling-stroke}")
        with (directory / "similarity-preview.html").open("x", encoding="utf-8") as f:
            f.write('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
                    'content="default-src &apos;none&apos;; img-src file:; style-src &apos;unsafe-inline&apos;">'
                    '<title>CopyNCE</title><style>' + style + "</style>" + "\n".join(rows))
        report["preview"] = {"file": "similarity-preview.html", "pairs_shown": len(rows)}


def write_preview(directory, report, records):
    if not records or not report["matches"]:
        return
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        write_html_preview(directory, report, records)
        return
    panel_w, panel_h = 280, 200
    rows = []
    for pair in report["matches"][:12]:
        panels = []
        try:
            for side in ("a", "b"):
                record = records[pair[f"path_{side}"]]
                with checked_stream(record) as stream, Image.open(stream) as source:
                    if source.width * source.height > report["options"].get("max_pixels", 40_000_000):
                        raise ValueError("原图超过预览像素上限")
                    image = ImageOps.exif_transpose(source)
                    if list(image.size) != pair[f"size_{side}"]:
                        raise ValueError("原图尺寸已改变")
                    w, h = image.size
                    image.thumbnail((panel_w - 10, panel_h - 8))
                    panel = Image.new("RGB", (panel_w, panel_h), "#eeeeee")
                    ox, oy = (panel_w-image.width)//2, (panel_h-image.height)//2
                    panel.paste(image.convert("RGB"), (ox, oy))
                    x0, y0, x1, y1 = pair[f"box_{side}"]
                    rect = (ox+x0*image.width//w, oy+y0*image.height//h,
                            ox+max(x0*image.width//w, (x1*image.width-1)//w),
                            oy+max(y0*image.height//h, (y1*image.height-1)//h))
                    ImageDraw.Draw(panel).rectangle(rect, outline="#eb4128", width=3)
                    panels.append(panel)
        except (KeyError, OSError, ValueError, Image.DecompressionBombError) as err:
            report["warnings"].append(f"{pair['id']} 预览不可用：{err}")
            continue
        rows.append((pair, panels))
    if rows:
        out = Image.new("RGB", (2*panel_w, len(rows)*(panel_h+20)), "white")
        for i, (pair, panels) in enumerate(rows):
            top = i*(panel_h+20)
            label = "Matcher" if report.get("schema_version") == 4 else "cosine"
            ImageDraw.Draw(out).text((4, top+2), f"{pair['id']}   {label} {pair['score']:.4f}   {pair['match_type']}",
                                     fill="black")
            for col, panel in enumerate(panels):
                out.paste(panel, (col*panel_w, top+20))
        with (directory / "similarity-preview.png").open("xb") as f:
            out.save(f, format="PNG")
        report["preview"] = {"file": "similarity-preview.png", "pairs_shown": len(rows)}


def report_lines(report, include_paths=True):
    s = report["summary"]
    yield (f"CopyNCE | {report['backend']} | {s['image_count']} 张图，比较 {s['pairs_assessed']}/"
           f"{s['pairs_possible']} 个图对，返回 {s['pairs_returned']}/{s['pairs_above_threshold']} 个已检索命中")
    if report.get("schema_version") == 4:
        stages = report["stage_totals"]
        yield (f"Matcher分类阈值 {report['options'].get('matcher_threshold', .5)}；质量档位 {report['profile']}；"
               f"计划候选 {stages['planned_candidates']}；待核验 {stages['matcher_pending']}；错误 {s['errors']}。")
        yield (f"复用已核验缓存 {stages.get('matcher_cache_hits', 0)} 对；"
               f"本次新增核验 {stages.get('matcher_new_verified', stages['matcher_verified'])} 对。")
    else:
        yield f"余弦阈值 {report['options']['threshold']}；检索方式 {report['mode_used']}；读取错误 {s['errors']}。"
    yield (f"文件处理完整：{report['files_complete']}；检索覆盖全部规定图对：{report['exhaustive']}；"
           f"结果截断：{report['truncated']}。")
    for warning in report["warnings"]:
        yield display_text(warning)
    for pair in report["matches"]:
        if include_paths:
            yield (f"{pair['id']} | {pair['score']:.5f} | {pair['match_type']}\n"
                   + "\n".join(f"  {display_text(pair['path_'+side])}  {pair['box_'+side]}" for side in ("a", "b")))
    for err in report["errors"]:
        if include_paths:
            yield f"错误 {display_text(err.get('path', ''))}  {display_text(err.get('message', ''))}"


def write_report(directory: Path, report, records):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_preview(directory, report, records)
    with (directory / "duplicates.json").open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=2, allow_nan=False)
        f.write("\n")
    with (directory / "duplicates.csv").open("x", encoding="utf-8-sig", newline="") as f:
        fields = ("id", "score", "match_type", "path_a", "path_b", "size_a", "size_b", "box_a", "box_b")
        enhanced = report.get("schema_version") == 4
        if enhanced:
            fields += ("descriptor_cosine", "local_cosine", "sn_a_to_b", "sn_b_to_a", "matcher_score", "matcher_cache_hit")
        writer = csv.writer(f)
        writer.writerow((*fields, "threshold"))
        for pair in report["matches"]:
            values = [pair.get(key) for key in fields]
            safe = ["'"+v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                    for v in values]
            writer.writerow([*safe, report["options"].get("matcher_threshold", .5) if enhanced
                             else report["options"]["threshold"]])
    with (directory / "duplicates.md").open("x", encoding="utf-8") as f:
        f.write("# CopyNCE 图片复用候选\n\n")
        for line in report_lines(report):
            f.write("<pre>"+html.escape(line)+"</pre>\n\n")
        if report["preview"]:
            f.write(f"[查看匹配窗口预览]({report['preview']['file']})\n")
