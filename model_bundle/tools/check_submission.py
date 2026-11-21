"""
Проверка набора данных перед сдачей (предметная область 1).

Что проверяется:
  1) в папке есть все 7 таблиц, и колонки в них как в шаблоне;
  2) значения из словарей допустимы (material_class, layer_function, aggregation и т. д.);
  3) ссылки между таблицами сходятся: device_id, measurement_id, material_id, duplicate_of, record_ids;
  4) каждый source_fragment находится на указанной странице PDF.

Запуск:
  python check_submission.py ПАПКА_С_CSV ПАПКА_С_PDF
  например: python check_submission.py ..\\dev_gold ..\\dev

Для пункта 4 нужен пакет pypdf:  python -m pip install pypdf
Без него проверяются только пункты 1–3.
"""
import csv
import os
import re
import sys

TABLES = {
    "publications": "publication_id title year DOI authors in_scope exclusion_reason",
    "materials": "publication_id material_id material_name_raw formula_normalized material_class dimensionality additives bandgap_value bandgap_unit bandgap_method note",
    "devices": "publication_id device_id absorber_material_id device_label device_role device_type architecture substrate_type active_area area_unit area_definition encapsulation note",
    "layers": "publication_id layer_id device_id layer_order layer_function layer_material layer_thickness thickness_unit deposition_method annealing_temperature annealing_time atmosphere note",
    "measurements": "publication_id measurement_id device_id measurement_type spectrum light_intensity intensity_unit scan_direction scan_rate mask_used certified certification_lab temperature stability_protocol stress_conditions note",
    "results": "publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit result_origin cited_publication_id source_page source_section source_table source_figure source_fragment extraction_status duplicate_of note",
    "issues": "publication_id issue_id record_ids check_name description source_page source_fragment",
}
VOCAB = {
    ("materials", "material_class"): {"perovskite", "CIGS", "CdTe", "kesterite", "Sb2Se3", "thin-film Si", "other"},
    ("materials", "dimensionality"): {"3D", "2D", "2D-3D"},
    ("materials", "bandgap_method"): {"Tauc", "EQE", "PL", "cited"},
    ("devices", "device_role"): {"control", "target", "record"},
    ("devices", "device_type"): {"cell", "minimodule", "module"},
    ("devices", "architecture"): {"n-i-p", "p-i-n", "substrate", "superstrate"},
    ("devices", "substrate_type"): {"rigid", "flexible"},
    ("devices", "area_definition"): {"designated illumination", "aperture", "total"},
    ("layers", "layer_function"): {"front_contact", "ETL", "absorber", "passivation", "HTL", "interlayer", "recombination", "back_contact"},
    ("measurements", "measurement_type"): {"J-V", "MPP tracking", "EQE", "stability"},
    ("measurements", "scan_direction"): {"reverse", "forward", "average"},
    ("measurements", "stability_protocol"): {"ISOS-D", "ISOS-L", "ISOS-T", "other"},
    ("measurements", "mask_used"): {"true", "false"},
    ("measurements", "certified"): {"true", "false"},
    ("publications", "in_scope"): {"true", "false"},
    ("results", "aggregation"): {"champion", "mean", "median"},
    ("results", "result_origin"): {"primary", "cited"},
    ("results", "extraction_status"): {"extracted", "present_not_extractable", "absent"},
}
NUMERIC = {("results", "metric_value"), ("results", "normalized_value"), ("results", "source_page"),
           ("layers", "layer_order"), ("devices", "active_area"), ("materials", "bandgap_value")}


def norm(s):
    s = (s.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("−", "-").replace("–", "-"))
    return re.sub(r"[^a-z0-9.%]+", "", s.lower())


def main(sub, pdfdir=None):
    errors, warns = [], []
    data = {}
    for t, cols in TABLES.items():
        p = os.path.join(sub, t + ".csv")
        if not os.path.exists(p):
            errors.append(f"нет файла {t}.csv")
            data[t] = []
            continue
        with open(p, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            miss = [c for c in cols.split() if c not in (rd.fieldnames or [])]
            if miss:
                errors.append(f"{t}.csv: нет колонок {', '.join(miss)}")
            data[t] = list(rd)

    for (t, c), allowed in VOCAB.items():
        for i, r in enumerate(data[t], 2):
            v = (r.get(c) or "").strip()
            if v and v not in allowed:
                errors.append(f"{t}.csv, строка {i}: {c} = «{v}» — нет в списке допустимых")
    for (t, c) in NUMERIC:
        for i, r in enumerate(data[t], 2):
            v = (r.get(c) or "").strip()
            if v:
                try:
                    float(v)
                except ValueError:
                    errors.append(f"{t}.csv, строка {i}: {c} = «{v}» — не число")

    key = lambda r, c: (r["publication_id"], r[c])
    pubs = {r["publication_id"] for r in data["publications"]}
    ids = {t: {key(r, c) for r in data[t]} for t, c in
           [("materials", "material_id"), ("devices", "device_id"), ("measurements", "measurement_id"), ("results", "result_id")]}
    for t in ("materials", "devices", "layers", "measurements", "results", "issues"):
        for i, r in enumerate(data[t], 2):
            if r["publication_id"] not in pubs:
                errors.append(f"{t}.csv, строка {i}: публикации {r['publication_id']} нет в publications.csv")

    def ref(t, c, target, many=False):
        for i, r in enumerate(data[t], 2):
            vals = [(r.get(c) or "").strip()]
            if many:
                vals = [x.strip() for x in vals[0].split(";")]
            for v in vals:
                if v and (r["publication_id"], v) not in ids[target]:
                    errors.append(f"{t}.csv, строка {i}: {c} = {v} — такой записи нет в {target}.csv")

    ref("devices", "absorber_material_id", "materials")
    ref("layers", "device_id", "devices")
    ref("measurements", "device_id", "devices", many=True)
    ref("results", "device_id", "devices")
    ref("results", "measurement_id", "measurements")
    ref("results", "material_id", "materials")
    ref("results", "duplicate_of", "results")
    ref("issues", "record_ids", "results", many=True)

    seen = set()
    for i, r in enumerate(data["results"], 2):
        k = key(r, "result_id")
        if k in seen:
            errors.append(f"results.csv, строка {i}: result_id {r['result_id']} повторяется")
        seen.add(k)
        if r["result_origin"] == "primary" and not r["device_id"] and r["extraction_status"] == "extracted":
            warns.append(f"results.csv, строка {i}: собственный результат без device_id")
        if r["result_origin"] == "cited" and not r["cited_publication_id"]:
            warns.append(f"results.csv, строка {i}: цитируемое значение без cited_publication_id")
        if r["extraction_status"] == "extracted" and not (r["source_page"] and (r["source_fragment"] or r["source_table"] or r["source_figure"])):
            warns.append(f"results.csv, строка {i}: не указано место извлечения")
    for r in data["publications"]:
        if r["in_scope"] == "false" and not r["exclusion_reason"]:
            warns.append(f"publications.csv: {r['publication_id']} исключена без exclusion_reason")

    checked = bad = 0
    if pdfdir:
        try:
            from pypdf import PdfReader
        except ImportError:
            warns.append("pypdf не установлен — цитаты не проверялись (python -m pip install pypdf)")
            PdfReader = None
        if PdfReader:
            cache = {}
            for t in ("results", "issues"):
                for i, r in enumerate(data[t], 2):
                    frag, page = (r.get("source_fragment") or "").strip(), (r.get("source_page") or "").strip()
                    if not frag or not page or re.match(r"^(Table|TABLE|Supplementary)", frag):
                        continue
                    pid = r["publication_id"]
                    path = os.path.join(pdfdir, pid + ".pdf")
                    if not os.path.exists(path):
                        continue
                    if pid not in cache:
                        try:
                            cache[pid] = [norm(pg.extract_text() or "") for pg in PdfReader(path).pages]
                        except Exception as e:
                            warns.append(f"{pid}.pdf не читается: {e}")
                            cache[pid] = []
                    pages = cache[pid]
                    try:
                        n = int(float(page))
                    except ValueError:
                        continue
                    checked += 1
                    nf = norm(frag)
                    if not (1 <= n <= len(pages)) or nf not in pages[n - 1]:
                        bad += 1
                        other = [k + 1 for k, txt in enumerate(pages) if nf in txt]
                        hint = f" (найдено на с. {', '.join(map(str, other))})" if other else ""
                        warns.append(f"{t}.csv, строка {i}: цитата не найдена на с. {page} файла {pid}.pdf{hint}: «{frag[:70]}»")

    for e in errors:
        print("ОШИБКА:", e)
    for w in warns:
        print("внимание:", w)
    print(f"\nтаблиц: {sum(1 for t in data if data[t])}, результатов: {len(data['results'])}, "
          f"ошибок: {len(errors)}, предупреждений: {len(warns)}" + (f", проверено цитат: {checked}, не найдено: {bad}" if checked else ""))
    print("Ошибки нужно исправить до сдачи. Предупреждения — проверить вручную: извлечение текста из PDF иногда расходится с видимым текстом.")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
