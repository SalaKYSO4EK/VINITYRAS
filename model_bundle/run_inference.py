"""Process a backend request using the local scientific PDF pipeline."""
import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
from tempfile import TemporaryDirectory

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


class InputPDFError(ValueError):
    """No uploaded article could be read as a PDF."""


def absolute(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"{label}. Требуется абсолютный путь.")
    return path.resolve()


def request_data(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Поддерживается request.json с целым schema_version = 1.")
    if not isinstance(data.get("job_id"), str) or not data["job_id"].strip():
        raise ValueError("Поле job_id должно содержать непустой текст.")
    criteria = data.get("criteria", "")
    if not isinstance(criteria, str):
        raise ValueError("Поле criteria должно содержать текст.")
    data["criteria"] = criteria.strip()
    if not isinstance(data.get("files"), list) or not data["files"]:
        raise ValueError("Поле files должно содержать хотя бы один PDF.")
    seen = set()
    pairs = []
    for item in data["files"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("name", "path")):
            raise ValueError("Для каждого PDF нужны строки name и path.")
        name = item["name"]
        win = PureWindowsPath(name)
        if (win.name != name or win.suffix.lower() != ".pdf" or not win.stem.strip(". ")
                or win.is_reserved() or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
            raise ValueError(f"Некорректное имя источника PDF: {name!r}")
        if win.stem.casefold() in seen:
            raise ValueError(f"Повторяющийся publication_id в задании: {win.stem}")
        seen.add(win.stem.casefold())
        pairs.append((name, absolute(item["path"], name)))
    return data, pairs


def stage_inputs(pairs, directory, warnings):
    from pypdf import PdfReader
    from article_analyzer.job_progress import report_phase

    paths, rejected = [], []
    directory.mkdir()
    for index, (name, path) in enumerate(pairs, 1):
        report_phase("read_pdfs", pdf_index=index, pdf_total=len(pairs), file=name)
        dest = directory / name
        try:
            shutil.copyfile(path, dest)
            with dest.open("rb") as stream:
                if not len(PdfReader(stream).pages):
                    raise ValueError("PDF не содержит страниц")
        except Exception as exc:
            warnings.append(f"{name}. Не удалось прочитать PDF. {exc}")
            rejected.append(name)
        else:
            paths.append(dest)
    if not paths:
        raise InputPDFError("Нет доступных для обработки PDF. " + " ".join(warnings))
    return paths, rejected


def solar_profile(compiled, criteria):
    from article_analyzer.efficiency_export import EFFICIENCY

    if EFFICIENCY.search(criteria):
        return True
    if re.search(r"\b(?:PCE|V_?oc|J_?sc|fill[ _]factor|photovoltaic|perovskite|solar)\b|солнечн|фотовольта|перовскит", criteria, re.I):
        return True
    if compiled["device_fields"] or any(m in {"Voc", "Jsc", "Jsc_EQE"} for m in compiled["metrics"]):
        return True
    candidates = {f["name"].casefold() for g in compiled["plan"]["extraction"]["groups"] for f in g["fields"]}
    return bool(candidates & {"pce", "power_conversion_efficiency", "ff", "fill_factor", "absorber_material_id"})


def export_solar(paths, compiled, work, export, warnings, *, rejected, comparisons=()):
    from article_analyzer.pdf_lab import run_documents

    result = run_documents(paths, compiled, work, rejected=rejected)
    validation = (work / "check-submission.txt").read_text("utf-8")
    if result["submission"]["exit_code"]:
        raise ValueError("Проверка структуры солнечных CSV не пройдена. " + validation)
    warnings.extend(line for line in validation.splitlines() if line.startswith("внимание:"))
    for problem in result["errors"]:
        warnings.append(f"{problem.get('file') or 'NuExtract'}. {problem['phase']}. {problem['error']}")
    layout_failed = {error["file"] for error in result["errors"] if error["phase"] == "layout"}
    for doc in result["documents"]:
        name = doc["file"]
        if name in rejected or name in layout_failed:
            continue
        for metric, entry in doc["coverage"].items():
            if not entry["records"] or entry.get("unresolved"):
                warnings.append(f"{name}, {metric}. Уверенно привязано {entry['records']}, "
                                f"неоднозначных ячеек {entry.get('unresolved', 0)}.")
        if doc.get("method_warning"):
            warnings.append(f"{name}. Условия измерений. {doc['method_warning']}")
        if compiled["title"] and not doc.get("title", {}).get("title"):
            warnings.append(f"{name}. Название статьи не подтверждено.")
        if compiled["device_fields"] and not any(r.get("fields") for r in doc.get("device_attributes", [])):
            warnings.append(f"{name}. Нет подтверждённых запрошенных свойств образца.")
        if not (doc["records"] or doc.get("title", {}).get("title") or doc.get("device_attributes")
                or doc.get("image_only_pages")):
            warnings.append(f"{name}. По заданным критериям научных записей не извлечено.")
        for row in doc["records"]:
            row.update(source_file=name, source_page=row["page"])
    # The frozen validator read these exact tables. Only strip the UTF-8 BOM for the backend.
    for csv_path in (work / "csv").glob("*.csv"):
        (export / csv_path.name).write_bytes(csv_path.read_bytes().removeprefix(b"\xef\xbb\xbf"))
    shutil.copyfile(work / "check-submission.txt", export / "check-submission.txt")
    if comparisons:
        from article_analyzer.efficiency_export import write_comparison
        result["comparisons"] = {metric: write_comparison(export, result["documents"], metric, paths, warnings)
                                 for metric in comparisons}
    for name in ("device-journal.jsonl", "device-responses.json", "title-responses.json",
                 "nuextract-devices.log", "nuextract-title-worker.log", "nuextract.log"):
        if (work / name).is_file():
            shutil.copyfile(work / name, export / name)
    return result


def file_outcomes(pairs, rejected, result, *, title_requested=False):
    layout_failed = {error["file"] for error in result["errors"] if error["phase"] == "layout"}
    documents = {doc["file"]: doc for doc in result["documents"]}
    confirmed = {doc["file"] for doc in result["documents"]
                 if any(not row.get("ambiguity") for row in doc["records"])
                 or any(row.get("fields") for row in doc.get("device_attributes", []))
                 or (title_requested and doc.get("title", {}).get("title"))}
    custom = result.get("custom", {})
    confirmed.update(custom.get("confirmed_sources", []))
    custom_limited = set(custom.get("limited_sources", []))
    outcomes = []
    for name, _ in pairs:
        doc = documents.get(name, {})
        if name in rejected:
            status, reason = "unreadable", "Не удалось прочитать PDF. Проверьте, что он открывается и не защищён паролем."
        elif name in layout_failed:
            status, reason = "processing_failed", "PDF открыт, но возникла ошибка при разборе страниц. Подробности в замечаниях."
        elif name in custom_limited:
            status, reason = "processing_limited", "Часть пользовательских критериев не удалось проверить. Подробности в замечаниях."
        elif doc.get("image_only_pages"):
            pages = ", ".join(map(str, doc["image_only_pages"][:12]))
            if len(doc["image_only_pages"]) > 12:
                pages += " и другие"
            status, reason = "processing_limited", f"Страницы с изображениями без извлекаемого текста: {pages}. OCR недоступен."
        elif title_requested and doc.get("title", {}).get("status") == "processing_failed":
            status, reason = "processing_limited", "Не удалось проверить название статьи. Подробности в замечаниях."
        elif name in confirmed:
            status, reason = "found", "Из PDF извлечены подтверждённые данные."
        else:
            status, reason = "no_findings", "PDF прочитан. Подтверждённых научных данных по критериям не найдено."
        outcomes.append({"file": name, "status": status, "reason": reason})
    return outcomes


def custom_plan(compiled, solar):
    if not solar:
        return compiled["plan"]
    from article_analyzer.pdf_lab import submission_groups

    plan = compiled["plan"]
    extraction = plan["extraction"]
    base = {field["name"].casefold() for group in submission_groups() for field in group["fields"]}
    base.update({"condition", "conditions", "source", "sources", "sample", "sample_label", "efficiency_status"})
    extras = {name for name in compiled["other_fields"] if name.casefold() not in base}
    groups = [group for group in extraction["groups"]
              if any(field["name"] in extras for field in group["fields"])]
    templates = extraction["nuextract_template"]
    return {**plan, "extraction": {"groups": groups,
                                  "nuextract_template": {group["name"]: templates[group["name"]] for group in groups}}}


def export_custom(paths, plan, work, export, warnings):
    from article_analyzer.inference_generic import run_generic

    work.mkdir()
    result = run_generic(paths, plan, work, warnings)
    occupied = {p.name.casefold() for p in export.iterdir()}
    for group, alias in result["groups"].items():
        target = alias
        while f"{target}.csv".casefold() in occupied:
            target = "custom_" + target
        name = f"{target}.csv"
        occupied.add(name.casefold())
        (work / f"{alias}.csv").rename(export / name)
        result["groups"][group] = target
    return result


def export_dual_qwen(paths, criteria, export, warnings, rejected, config):
    from functools import partial
    from article_analyzer.dual_qwen import run as compare
    from article_analyzer.dual_worker import available_worker, send_pdf
    from article_analyzer.qwen_api import QwenApi, QwenApiError

    settings = json.loads(config.read_text(encoding="utf-8-sig"))
    local_address = os.getenv("NEXUS_LOCAL_QWEN_API", settings["local_api"]).strip()
    local = QwenApi(local_address, "nexus-qwen3-4b", 180).complete
    remote_config = QwenApi.configured(BASE / "config/qwen-api.json")
    if not remote_config or "qwen3-4b" not in remote_config.model.casefold():
        raise QwenApiError("Для второго прохода укажите API и идентификатор Qwen3-4B в qwen-api.json.")
    if remote_config.base_url.rstrip("/") == local_address.rstrip("/"):
        raise QwenApiError("Для двух проходов настройте отдельный адрес API второго ноутбука.")
    remote_worker = None
    address = None if os.getenv("NEXUS_FAST_TEXT_REVIEW") == "1" else settings.get("remote_worker")
    if address == "auto":
        address = available_worker(remote_config.base_url)
        if not address:
            warnings.append("Второй ноутбук получил текст страниц. Чтобы передавать PDF целиком, запустите на нём обработчик на порту 8091.")
    if address:
        token = os.getenv("NEXUS_DUAL_WORKER_TOKEN", "")
        if not token and (BASE/"config/dual-worker.token").is_file():
            token = (BASE/"config/dual-worker.token").read_text(encoding="utf-8-sig").strip()
        if len(token) < 24:
            raise QwenApiError("В окружении NEXUS отсутствует токен LAN-воркера NEXUS_DUAL_WORKER_TOKEN.")
        remote_worker = partial(send_pdf,address,token,600)
    rows = compare(paths,criteria,export,local,remote_config.complete,remote_worker,warnings=warnings,rejected=rejected)
    report = export / "report.json"
    summary = json.loads(report.read_bytes())
    summary["file_outcomes"].extend({"file": filename, "status": "unreadable",
        "reason": "Не удалось прочитать PDF. Проверьте, что файл открывается и доступен без пароля."}
        for filename in rejected)
    report.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    return rows


def publish_export(export, out):
    published = []
    try:
        for path in sorted(export.iterdir()):
            target = out / path.name
            path.rename(target)
            published.append(target)
    except OSError:
        for target in published:
            target.unlink(missing_ok=True)
        raise
    return published


def run(request, out, *, criteria_source=None):
    from article_analyzer.criteria import InterpretationError
    from article_analyzer.job_progress import report_phase
    from article_analyzer.efficiency_export import REQUEST_METRICS, comparison_metrics
    from article_analyzer.pdf_lab import CONFIG, DEVICE_FIELDS, compile_criteria, default_criteria, submission_groups
    from tools.interpret_criteria import save_json

    data, pairs = request_data(request)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f"Каталог --out должен быть пустым: {out}")
    lock = out / ".inference.lock"
    with lock.open("x"):
        pass
    try:
        with TemporaryDirectory(prefix="pdf-inference-", dir=out.parent) as directory:
            work = Path(directory)
            export = work / "export"
            export.mkdir()
            warnings = []
            report_phase("read_pdfs")
            paths, rejected = stage_inputs(pairs, work / "pdf", warnings)
            criteria_text = criteria_source.text() if criteria_source is not None else data["criteria"]
            config = BASE / "config/dual-qwen.json"
            if config.is_file() and os.getenv("NEXUS_LEGACY_INFERENCE") != "1":
                rows = export_dual_qwen(paths, criteria_text, export, warnings, rejected, config)
                publish_export(export, out)
                print(json.dumps({"job_id":data["job_id"],"profile":"dual-qwen","rows":rows},ensure_ascii=False))
                return
            use_default = criteria_source is None and not criteria_text
            comparisons = comparison_metrics(criteria_text)
            if use_default:
                report_phase("criteria_default")
                compiled = default_criteria(work)
            else:
                criteria = work / "criteria.txt"
                criteria.write_text(data["criteria"], encoding="utf-8")
                try:
                    report_phase("criteria")
                    compiled = compile_criteria(criteria, work, source=criteria_source)
                except InterpretationError as error:
                    if not comparisons:
                        raise
                    warnings.append(f"Qwen не сформировала схему. Применён профиль явно запрошенного сравнения. {error}")
                    compiled = {"plan": {"mode": "metric_comparison_fallback",
                                         "extraction": {"groups": [], "nuextract_template": {}}},
                                "metrics": comparisons, "other_fields": []}
            plan = compiled["plan"]
            solar = use_default or bool(comparisons) or solar_profile(compiled, criteria_text)
            additional = None if use_default else custom_plan(compiled, solar)
            has_additional = bool(additional and additional["extraction"]["groups"])
            if not use_default:
                compiled["base_tables"] = submission_groups()
                if comparisons:
                    compiled["table_extensions"] = {
                        "publications": [REQUEST_METRICS[metric][2] for metric in comparisons],
                        "comparisons": [REQUEST_METRICS[metric][1] for metric in comparisons],
                    }
                save_json(work / "criteria-plan.json", compiled)
            for error in plan.get("page_errors", []):
                warnings.append(f"Разбор критериев, {error['phase']}. {error['error']}")
            warnings.extend(str(q) for q in plan.get("questions", []))
            for name in plan.get("coverage", {}).get("explicit_identifiers_not_in_fields", []):
                warnings.append(f"В схеме не найден явно перечисленный идентификатор {name}.")
            base = {**compiled, "metrics": list(dict.fromkeys([*CONFIG["criterion_fields"], *compiled["metrics"]])),
                    "title": True, "device_fields": list(DEVICE_FIELDS)}
            result = export_solar(paths, base, work / "solar", export, warnings, rejected=rejected,
                                  comparisons=comparisons)
            if has_additional:
                report_phase("extract_custom")
                result["custom"] = export_custom(paths, additional, work / "custom", export, warnings)
            report_phase("export")
            save_json(export / "records.json", result)
            for name in ("criteria-plan.json", "qwen-raw.json"):
                shutil.copyfile(work / name, export / name)
            save_json(export / "report.json", {"warnings": list(dict.fromkeys(warnings)),
                                               "file_outcomes": file_outcomes(
                                                   pairs, rejected, result, title_requested=compiled.get("title", False))})
            published = publish_export(export, out)
            print(json.dumps({"job_id": data["job_id"], "profile": "solar" if solar else "generic",
                              "csv": sorted(p.name for p in published if p.suffix == ".csv")}, ensure_ascii=False))
    finally:
        lock.unlink(missing_ok=True)


def report_qwen_api_error(error):
    # QwenApiError carries a fixed user-facing message from the API transport.
    message = " ".join(str(error).split())
    print(f"QWEN_API_ERROR: {message}", file=sys.stderr)
    return 3


def report_pdf_input_error(error):
    print(f"PDF_INPUT_ERROR: {error}", file=sys.stderr)
    return 4


def main():
    from article_analyzer.qwen_api import QwenApiError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    os.environ["PYTHONUTF8"] = "1"
    try:
        from article_analyzer.inference_job import contain_workers
        contain_workers()
        run(absolute(args.request, "--request"), absolute(args.out, "--out"))
    except QwenApiError as error:
        return report_qwen_api_error(error)
    except InputPDFError as error:
        return report_pdf_input_error(error)
    except Exception as exc:
        print(f"Задание не выполнено. {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

