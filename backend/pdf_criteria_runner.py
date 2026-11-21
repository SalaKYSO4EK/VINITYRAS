"""Let the packaged extraction pipeline read the uploaded criteria page by page."""

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch


class CriteriaPDFError(ValueError):
    pass


def read_criteria(document, name, selection):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    from article_analyzer.criteria import Source
    from tools.interpret_criteria import selected_pages

    if not document.is_file():
        raise CriteriaPDFError("Не удалось открыть PDF с критериями.")
    try:
        with document.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            stream.seek(0)
            pdf = PdfReader(stream)
            if pdf.is_encrypted and not pdf.decrypt(""):
                raise CriteriaPDFError("PDF с критериями защищён паролем.")
            count = len(pdf.pages)
            try:
                numbers = selected_pages(selection, count)
            except ValueError as error:
                raise CriteriaPDFError(f"Проверьте диапазон страниц. В PDF с критериями {count} страниц.") from error
            pages = {number: (pdf.pages[number - 1].extract_text() or "").strip() for number in numbers}
    except (OSError, PdfReadError) as error:
        raise CriteriaPDFError("Не удалось прочитать PDF с критериями. Проверьте, что файл открывается.") from error

    source_pages = {number: text for number, text in pages.items() if text}
    if not source_pages:
        raise CriteriaPDFError("В выбранных страницах нет читаемого текста. Выберите другие страницы или вставьте критерии текстом.")
    return Source(name, digest.hexdigest(), source_pages), sorted(pages.keys() - source_pages.keys())


def run_with_pdf(runner_path, request_path, out):
    sys.path.insert(0, str(runner_path.parent))
    from article_analyzer import pdf_lab
    from article_analyzer.inference_job import contain_workers
    from tools.interpret_criteria import save_json

    request = json.loads(request_path.read_text(encoding="utf-8"))
    criteria = request["criteria_pdf"]
    document = Path(criteria["path"]).resolve()
    if document != request_path.parent / "criteria.pdf":
        raise CriteriaPDFError("PDF с критериями не найден в задании.")
    source, skipped = read_criteria(document, criteria["name"], criteria.get("pages"))

    spec = importlib.util.spec_from_file_location("nexus_packaged_runner", runner_path)
    if spec is None or spec.loader is None:
        raise CriteriaPDFError("Не удалось открыть скрипт запуска пакета модели.")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    detect_profile = runner.solar_profile
    contain_workers()
    # The package compiles criteria through this reader in its per-job process.
    with patch.object(pdf_lab, "read_source", return_value=source), patch.object(
        runner, "solar_profile", side_effect=lambda compiled, _text: detect_profile(compiled, source.text())
    ):
        runner.run(request_path, out)

    if skipped:
        report = out / "report.json"
        data = json.loads(report.read_text(encoding="utf-8"))
        data["warnings"].append("Страницы критериев без текста пропущены. Номера страниц: " + ", ".join(map(str, skipped)) + ".")
        save_json(report, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        run_with_pdf(args.runner.resolve(), args.request.resolve(), args.out.resolve())
    except CriteriaPDFError as error:
        args.request.with_name("criteria-error.txt").write_text(str(error), encoding="utf-8")
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
