"""Entrypoints shared by the independent Silver notebooks."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
from tempfile import mkdtemp

import pandas as pd

from bronze_review import open_bronze
from silver_controls import reconcile
from silver_inputs import load_inputs
from silver_model import (Model, apply_mdm, normalize_channels, normalize_finance, normalize_fulfillment,
                          normalize_orders, normalize_planning_and_bank)


SETTINGS = {
    "dealer_currency": "INR",
    "mdm_effective_from": "2026-11-01",
    "ar_cutoff": "2026-12-10",
    "validate_fixture_baseline": True,
}
STAGES = ("orders", "mdm", "fulfillment", "finance", "channels", "validate")


def settings_from_file(path="silver_settings.json"):
    path = Path(path)
    return {**SETTINGS, **(json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {})}


def build(stage="validate", settings=None, bronze=None):
    """Rebuild up to the requested stage using inputs independently of notebook kernels."""
    cfg = {**SETTINGS, **(settings or {})}
    data, keys, selected, deliveries = load_inputs(bronze if bronze is not None else open_bronze(), cfg)
    model = Model(data, keys, cfg)
    model.input_selection = {
        "manifests": selected, "kafka_keys": [row["bronze_key"] for row in deliveries],
    }
    stages = [
        normalize_orders, apply_mdm, normalize_fulfillment, normalize_finance,
        lambda m: (normalize_channels(m, deliveries), normalize_planning_and_bank(m)),
    ]
    end = STAGES.index(stage)
    for transform in stages[:end + 1]:
        transform(model)
    model.controls, model.calculated_ar = reconcile(model) if stage == "validate" else ([], [])
    model.stage = stage
    return model


def frame(model, table):
    return pd.DataFrame(model.table(table).values())


def input_frame(model):
    return pd.DataFrame([
        {"source": source, "manifest": manifest}
        for source, manifest in model.input_selection["manifests"].items()
    ])


def publish(model, root="silver_output"):
    """Create an immutable local snapshot. Decimal values are written as JSON strings."""
    if model.stage != "validate":
        raise ValueError("Run all stages and controls before publishing")
    if any(check["status"] == "FAIL" for check in model.controls) or any(
        row["severity"] == "error" for row in model.exceptions
    ):
        raise ValueError("Resolve failed controls and error-level exceptions before publishing")

    def encode(row):
        return (json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")

    outputs = {name: list(rows.values()) for name, rows in model.tables.items()}
    outputs.update(source_observation=model.lineage, quality_exception=model.exceptions,
                   reconciliation_control=model.controls, calculated_ar_balance=model.calculated_ar)
    payloads = {name: b"".join(encode(row) for row in rows) for name, rows in sorted(outputs.items())}
    files = {f"{name}.jsonl": {"rows": len(outputs[name]), "sha256": sha256(payload).hexdigest()}
             for name, payload in payloads.items()}
    provenance = {"format_version": 1, "inputs": model.input_selection,
                  "rules": {k: v for k, v in model.settings.items() if k not in {"manifests", "kafka_keys"}},
                  "coverage": model.coverage, "files": files}
    run_id = sha256(encode(provenance)).hexdigest()
    manifest = encode({"model_run_id": run_id, **provenance})
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / run_id
    def existing_snapshot():
        if ((target / "manifest.json").read_bytes() != manifest
                or any(sha256((target / name).read_bytes()).hexdigest() != info["sha256"]
                       for name, info in files.items())):
            raise ValueError(f"Existing snapshot is inconsistent: {target}")
        return target

    if target.exists():
        return existing_snapshot()
    staging = Path(mkdtemp(prefix=".silver-", dir=root))
    try:
        for name, payload in payloads.items():
            (staging / f"{name}.jsonl").write_bytes(payload)
        (staging / "manifest.json").write_bytes(manifest)
        try:
            staging.rename(target)
        except OSError:
            if not target.exists():
                raise
            return existing_snapshot()
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target
