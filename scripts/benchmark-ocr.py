"""Reproducible synthetic OCR field, process-memory and API-contention measurement.

Never opens user images or an existing account database. Output must be a new
directory. The font is read locally and identified by hash, never redistributed.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import queue
import statistics
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TITLES = ["流浪地球", "星际穿越", "千与千寻", "哪吒之魔童降世", "寻梦环游记",
          "疯狂动物城", "长安三万里", "让子弹飞", "无间道", "大话西游",
          "Metropolis", "Nosferatu", "The General", "The Kid", "Sherlock Jr.",
          "City Lights", "Modern Times", "The Gold Rush", "Safety Last!", "The Phantom of the Opera"]
CONDITIONS = ("clean", "tilt", "low_contrast", "blur", "perspective", "missing_field")


def source_hashes():
    files = [*ROOT.joinpath("backend").glob("*.py"), *ROOT.joinpath("backend/services").glob("*.py"),
             *ROOT.joinpath("backend").glob("requirements*.txt")]
    files.extend(ROOT / path for path in ("backend/ocr-models.json", "scripts/benchmark-ocr.py",
                 "scripts/benchmark-capacity.py", "backend/tests/account_fixtures.py"))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def generate(destination, font_path):
    samples = []
    for index, title in enumerate(TITLES):
        english = index >= 10
        labels = ["Film", "Cinema", "Screen", "Seat", "Showtime"] if english else ["片名", "影院", "影厅", "座位", "放映时间"]
        fields = {"title": title, "cinema": "Aurora Cinema" if english else f"星光影城{index + 1}店",
                  "hall": str(index % 7 + 1) if english else f"{index % 7 + 1}号厅",
                  "seat": f"G{index + 1}" if english else f"{index % 9 + 1}排{index + 1}座",
                  "showtime": f"2026-11-{index + 1:02d}T{18 + index % 4:02d}:30"}
        for condition in CONDITIONS:
            expected = dict(fields)
            if condition == "missing_field":
                del expected[list(fields)[index % 5]]
            picture = Image.new("RGB", (1060, 700), "#f5f2e8")
            draw = ImageDraw.Draw(picture)
            font = ImageFont.truetype(str(font_path), 30 + index % 3 * 3)
            small = ImageFont.truetype(str(font_path), 22)
            draw.rectangle((18, 18, 1041, 681), outline="#555555", width=2)
            draw.text((55, 40), "MOVIE TICKET / 电影票 · 合成测试", font=small, fill="#555555")
            y = 112
            # Layout order varies, without changing the evidence required by the parser.
            order = list(fields)
            if index % 2:
                order[0], order[1] = order[1], order[0]
            for key in order:
                if key in expected:
                    value = expected[key].replace("T", " ") if key == "showtime" else expected[key]
                    draw.text((55 + index % 3 * 9, y), f"{labels[list(fields).index(key)]}: {value}", font=font, fill="#151515")
                y += 82
            draw.text((55, 590), "ORDER: TEST-0000   PRINT: 2026-10-22 09:00", font=small, fill="#555555")
            if condition == "tilt":
                picture = picture.rotate(4 if index % 2 else -4, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            elif condition == "low_contrast":
                picture = ImageEnhance.Contrast(picture).enhance(.24)
            elif condition == "blur":
                picture = picture.filter(ImageFilter.GaussianBlur(1.15))
            elif condition == "perspective":
                picture = picture.transform(picture.size, Image.Transform.PERSPECTIVE,
                                            (1, .055, -15, .015, 1, 0, .000035, .00006),
                                            Image.Resampling.BICUBIC, fillcolor="white")
            path = destination / f"ticket-{index:02d}-{condition}.png"
            picture.save(path)
            picture.close()
            samples.append({"path": path.name, "condition": condition,
                            "split": "development" if index in {0, 1, 10, 11} else "held_out", "expected": expected})
    # Negative controls exercise actual OCR box splitting rather than assuming
    # purchase/printing labels and their dates always survive in one text line.
    for index, label in enumerate(("购买时间", "PRINT", "", "放映时间")):
        picture = Image.new("RGB", (1060, 700), "white")
        draw = ImageDraw.Draw(picture)
        font = ImageFont.truetype(str(font_path), 36)
        draw.text((60, 150), label, font=font, fill="black")
        draw.text((60, 280), "2026-10-22 18:00", font=font, fill="black")
        path = destination / f"date-control-{index:02d}.png"
        picture.save(path)
        picture.close()
        samples.append({"path": path.name, "condition": "date_control", "split": "control", "expected": {}})
    for sample in samples:
        sample["sha256"] = hashlib.sha256((destination / sample["path"]).read_bytes()).hexdigest()
    (destination / "labels.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    return samples


def worker(messages, directory, models, samples):
    os.environ["MEMOVERSE_OCR_MODEL_DIR"] = models
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    import psutil
    process = psutil.Process()
    before_import = process.memory_info().rss
    started = time.perf_counter()
    from backend import ocr
    imported = time.perf_counter() - started
    idle_imported = process.memory_info().rss
    peak = [idle_imported]
    stopping = threading.Event()

    def sample_memory():
        while not stopping.wait(.02):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    results = []
    try:
        for number, sample in enumerate(samples):
            raw = (Path(directory) / sample["path"]).read_bytes()
            started = time.perf_counter()
            actual = ocr.recognize(raw)
            elapsed = time.perf_counter() - started
            results.append({**sample, "actual": actual, "seconds": elapsed})
            if number == 0:
                messages.put({"kind": "cold", "seconds": elapsed, "idleAfterFirstRss": process.memory_info().rss})
            if (number + 1) % 20 == 0:
                messages.put({"kind": "progress", "count": number + 1})
        info = process.memory_info()
        peak_bytes = max(peak[0], getattr(info, "peak_wset", 0))
        messages.put({"kind": "result", "results": results, "memory": {
            "beforeOcrImportRss": before_import, "afterOcrModuleImportRss": idle_imported,
            "afterInferenceRss": info.rss, "peakRss": peak_bytes,
            "peakMethod": "Windows PeakWorkingSetSize and 20ms RSS sampling" if os.name == "nt" else "20ms RSS sampling"},
            "moduleImportSeconds": imported})
    except Exception as error:
        messages.put({"kind": "error", "type": type(error).__name__})
    finally:
        stopping.set()
        sampler.join(timeout=1)


def percentiles(values):
    ordered = sorted(values)
    return {"count": len(values), "p50": statistics.median(ordered),
            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))], "max": max(ordered)}


def summarize(results):
    groups = defaultdict(list)
    for result in results:
        groups["all"].append(result)
        groups[result["split"]].append(result)
        groups[result["condition"]].append(result)
    summary = {}
    for group, rows in groups.items():
        counts = {}
        for field in ("title", "cinema", "hall", "seat", "showtime"):
            present = [row for row in rows if field in row["expected"]]
            absent = [row for row in rows if field not in row["expected"]]
            correct = sum(row["actual"].get(field) == row["expected"][field] for row in present)
            wrong = sum(field in row["actual"] and row["actual"][field] != row["expected"][field] for row in present)
            counts[field] = {"expectedPresent": len(present), "exact": correct,
                             "wrongNonempty": wrong, "missed": len(present) - correct - wrong,
                             "expectedAbsent": len(absent), "falseFill": sum(field in row["actual"] for row in absent)}
        summary[group] = {"samples": len(rows), "fields": counts}
    return summary


def run(args):
    measured_sources = source_hashes()
    destination = args.output.resolve()
    if os.name == "nt" and destination.drive.upper() != "D:":
        raise ValueError("Use D: for the benchmark")
    destination.mkdir(parents=True, exist_ok=False)
    samples = generate(destination, args.font)
    # Same synthetic 300-ticket, uncached snapshot workload before/during OCR.
    import importlib.util
    spec = importlib.util.spec_from_file_location("capacity_benchmark", ROOT / "scripts/benchmark-capacity.py")
    capacity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capacity)
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.object_storage import LocalObjectStore
    from backend.shared_state import RedisSharedState
    from backend.tests.account_fixtures import FakeAuthFactory, signed_in, ORIGIN
    workspace = destination / "synthetic-account"
    app = create_app(workspace, auth_provider=FakeAuthFactory(), object_store=LocalObjectStore(workspace),
                     shared_state=RedisSharedState(), run_recovery=False)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        assert client.get("/api/state").status_code == 200
        capacity._seed(app.state.store, 300, "shared-original")

        def request_state():
            started = time.perf_counter()
            response = client.get("/api/state")
            if response.status_code != 200 or len(response.json()["data"]["tickets"]) != 300:
                raise RuntimeError("Synthetic API measurement failed")
            return (time.perf_counter() - started) * 1000

        baseline = [request_state() for _ in range(30)]
        context = multiprocessing.get_context("spawn")
        messages = context.Queue()
        child = context.Process(target=worker, args=(messages, str(destination), str(args.models.resolve()), samples))
        concurrent, cold, result = [], None, None
        child.start()
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                concurrent.append(request_state())
                try:
                    message = messages.get(timeout=.08)
                except queue.Empty:
                    if not child.is_alive():
                        raise RuntimeError("OCR process exited without a result")
                    continue
                if message["kind"] == "result":
                    result = message
                    break
                if message["kind"] == "error":
                    raise RuntimeError(f"OCR failed: {message['type']}")
                if message["kind"] == "cold":
                    cold = message
                else:
                    print(json.dumps(message), flush=True)
            if result is None:
                raise TimeoutError("OCR measurement exceeded ten minutes")
        finally:
            child.join(timeout=3)
            if child.is_alive():
                child.terminate()
                child.join(timeout=3)
            messages.close()
    if source_hashes() != measured_sources:
        raise RuntimeError("Measured source changed during the run; no final report published")
    report = {"createdAt": datetime.now(timezone.utc).isoformat(), "environment": {
        "platform": platform.platform(), "python": sys.version, "cpuCount": os.cpu_count(),
        "processor": platform.processor(), "fontSha256": hashlib.sha256(args.font.read_bytes()).hexdigest(),
        "fontName": args.font.name, "modelManifest": json.loads((ROOT / "backend/ocr-models.json").read_text()),
        "labelsSha256": hashlib.sha256((destination / "labels.json").read_bytes()).hexdigest(),
        "installedVersions": {package: metadata.version(package) for package in (
            "rapidocr", "onnxruntime", "numpy", "opencv-python-headless", "Pillow", "psutil",
            "fastapi", "starlette", "pydantic", "redis", "httpx")},
        "gitHead": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "trackedWorkingTreeDirty": bool(subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=ROOT, text=True).strip()),
        "sourceSha256": measured_sources},
        "limitations": ["Synthetic labelled tickets only; not real-user photo accuracy.",
                        "Development and held-out sets use distinct titles, but share the synthetic layout generator.",
                        "Local Windows CPU; not a t3.small resource guarantee.",
                        "Only the OCR child process memory is measured; API/harness, Redis, OS and filesystem caches are additional.",
                        "Labelled/quoted fields are supported; unlabeled titles, handwriting, upside-down photos and arbitrary real-ticket layouts have not been validated.",
                        "API uses serial in-process ASGI requests, synthetic authentication/local storage and no Redis cache; no TLS, S3 or real network.",
                        "One persistent OCR child measures warm inference; production currently spawns per job, so cold cost applies there.",
                        "Cold means first inference in a fresh OCR process; the operating-system disk cache was not flushed.",
                        "API and OCR sampling run concurrently; samples and averages do not isolate scheduler or unrelated system effects."],
        "cold": cold, "memory": result["memory"], "moduleImportSeconds": result["moduleImportSeconds"],
        "warmSeconds": percentiles([row["seconds"] for row in result["results"][1:]]),
        "apiBaselineMilliseconds": percentiles(baseline), "apiDuringOcrMilliseconds": percentiles(concurrent),
        "accuracy": summarize(result["results"]), "results": result["results"]}
    path = destination / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("cold", "memory", "warmSeconds", "apiBaselineMilliseconds", "apiDuringOcrMilliseconds")}, indent=2))
    print(f"Report: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    run(parser.parse_args())
