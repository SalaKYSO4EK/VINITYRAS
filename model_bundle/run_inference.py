"""Process a backend request using the local, frozen scientific PDF pipeline."""
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


def absolute(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"{label}. Требуется абсолютный путь.")
    return path.resolve()


def request_data(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Поддерживается request.json с целым schema_version = 1.")
    for key in ("criteria", "job_id"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Поле {key} должно содержать непустой текст.")
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

    paths = []
    directory.mkdir()
    for name, path in pairs:
        dest = directory / name
        try:
            shutil.copyfile(path, dest)
            with dest.open("rb") as stream:
                if not len(PdfReader(stream).pages):
                    raise ValueError("PDF не содержит страниц")
        except Exception as exc:
            warnings.append(f"{name}. Не удалось прочитать PDF. {exc}")
        else:
            paths.append(dest)
    if not paths:
        raise ValueError("Нет доступных для обработки PDF. " + " ".join(warnings))
    return paths


def solar_profile(compiled, criteria):
    if re.search(r"\b(?:PCE|V_?oc|J_?sc|fill[ _]factor|photovoltaic|perovskite|solar)\b|солнечн|фотовольта|перовскит", criteria, re.I):
        return True
    if compiled["device_fields"] or any(m in {"Voc", "Jsc", "Jsc_EQE"} for m in compiled["metrics"]):
        return True
    candidates = {f["name"].casefold() for g in compiled["plan"]["extraction"]["groups"] for f in g["fields"]}
    return bool(candidates & {"pce", "power_conversion_efficiency", "ff", "fill_factor", "absorber_material_id"})


def export_solar(paths, compiled, work, export, warnings):
    from article_analyzer.pdf_lab import run_documents
    from tools.interpret_criteria import save_json

    result = run_documents(paths, compiled, work)
    validation = (work / "check-submission.txt").read_text("utf-8")
    if result["submission"]["exit_code"]:
        raise ValueError("Проверка структуры солнечных CSV не пройдена. " + validation)
    warnings.extend(line for line in validation.splitlines() if line.startswith("внимание:"))
    for problem in result["errors"]:
        warnings.append(f"{problem.get('file') or 'NuExtract'}. {problem['phase']}. {problem['error']}")
    if all(any(e["file"] == doc["file"] and e["phase"] == "layout" for e in result["errors"])
           for doc in result["documents"]):
        raise ValueError("Разбор всех PDF завершился ошибкой. " + " ".join(warnings))
    if compiled["other_fields"] or compiled["unsupported_metrics"]:
        warnings.append("В солнечном маршруте нет отдельного извлечения полей: "
                        + ", ".join([*compiled["other_fields"], *compiled["unsupported_metrics"]]))
    for doc in result["documents"]:
        name = doc["file"]
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
        if not (doc["records"] or doc.get("title", {}).get("title") or doc.get("device_attributes")):
            warnings.append(f"{name}. По заданным критериям научных записей не извлечено.")
        for row in doc["records"]:
            row.update(source_file=name, source_page=row["page"])
    # The frozen validator read these exact tables. Only strip the UTF-8 BOM for the backend.
    for csv_path in (work / "csv").glob("*.csv"):
        (export / csv_path.name).write_bytes(csv_path.read_bytes().removeprefix(b"\xef\xbb\xbf"))
    shutil.copyfile(work / "check-submission.txt", export / "check-submission.txt")
    save_json(export / "records.json", result)
    for name in ("device-journal.jsonl", "device-responses.json", "title-responses.json",
                 "nuextract-devices.log", "nuextract.log"):
        if (work / name).is_file():
            shutil.copyfile(work / name, export / name)


def run(request, out):
    from article_analyzer.pdf_lab import compile_criteria
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
            paths = stage_inputs(pairs, work / "pdf", warnings)
            criteria = work / "criteria.txt"
            criteria.write_text(data["criteria"], encoding="utf-8")
            compiled = compile_criteria(criteria, work)
            plan = compiled["plan"]
            for error in plan.get("page_errors", []):
                warnings.append(f"Разбор критериев, {error['phase']}. {error['error']}")
            warnings.extend(str(q) for q in plan.get("questions", []))
            for name in plan.get("coverage", {}).get("explicit_identifiers_not_in_fields", []):
                warnings.append(f"В схеме не найден явно перечисленный идентификатор {name}.")
            solar = solar_profile(compiled, data["criteria"])
            if solar:
                export_solar(paths, compiled, work / "solar", export, warnings)
            else:
                from article_analyzer.inference_generic import run_generic
                result = run_generic(paths, plan, export, warnings)
                save_json(export / "records.json", result)
            for name in ("criteria-plan.json", "qwen-raw.json"):
                shutil.copyfile(work / name, export / name)
            save_json(export / "report.json", {"warnings": list(dict.fromkeys(warnings))})
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
            print(json.dumps({"job_id": data["job_id"], "profile": "solar" if solar else "generic",
                              "csv": sorted(p.name for p in published if p.suffix == ".csv")}, ensure_ascii=False))
    finally:
        lock.unlink(missing_ok=True)


def main():
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
    except Exception as exc:
        print(f"Задание не выполнено. {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

