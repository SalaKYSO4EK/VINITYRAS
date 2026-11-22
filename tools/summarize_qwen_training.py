"""Measure scientific cell choice and strict JSON separately for a LoRA run."""
import argparse
import json
import re
from pathlib import Path

CELL_ID = re.compile(r"\bC\d+_\d+\b")


def count(rows):
    cells = strict = 0
    for row in rows:
        raw = row["raw"].strip()
        expected = row["expected"]
        mentioned = CELL_ID.findall(raw)
        cells += sorted(mentioned) == sorted(expected)
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or set(data) != {"ids"}:
            continue
        received = data["ids"]
        if isinstance(received,list) and all(isinstance(item,str) for item in received):
            strict += sorted(received) == sorted(expected)
    return {"examples":len(rows),"correct_cells":cells,"correct_cells_and_json":strict}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",type=Path)
    args=parser.parse_args()
    summary={}
    report=json.loads((args.directory/"run.json").read_bytes())
    for split in ("validation","holdout"):
        summary[split]={version:count(report[f"{split}_{version}"]) for version in ("base","lora")
                        if f"{split}_{version}" in report}
    steps=report["steps"]
    summary["training"]={"steps":len(steps),"seconds":round(sum(row["seconds"] for row in steps),3),
                         "loss":[row["loss"] for row in steps],"trainable_parameters":report["trainable_parameters"]}
    summary["deployable_improvement"] = all(
        set(versions)=={"base","lora"} and versions["lora"]["correct_cells"] > versions["base"]["correct_cells"]
        for versions in (summary["validation"],summary["holdout"]))
    (args.directory/"metrics.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),"utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
