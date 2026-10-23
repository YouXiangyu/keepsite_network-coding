"""Bounded CPU OCR and conservative movie-ticket fields; never saves full text."""
from datetime import datetime
from functools import lru_cache
import hashlib
from importlib import metadata
from io import BytesIO
import json
import os
from pathlib import Path
import re
import threading
import unicodedata

from PIL import Image, ImageOps

MANIFEST = json.loads(Path(__file__).with_name("ocr-models.json").read_text(encoding="utf-8"))
DEPENDENCIES = {"rapidocr": "3.10.0", "onnxruntime": "1.31.0"}
MIN_CONFIDENCE = 0.85
_engine = None
_engine_directory = None
_lock = threading.Lock()


class OCRError(Exception):
    """A safe, non-document-bearing OCR failure."""


@lru_cache(maxsize=8)
def _verify_files(directory, stamps):
    for item in [*MANIFEST["models"].values(), MANIFEST["dictionary"]]:
        with (Path(directory) / item["filename"]).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise OCRError("OCR 模型校验失败。")
    return True


def model_directory():
    value = os.environ.get("MEMOVERSE_OCR_MODEL_DIR")
    if not value:
        raise OCRError("尚未配置 OCR 模型。")
    directory = Path(value).resolve(strict=True)
    stamps = tuple((path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                   for item in [*MANIFEST["models"].values(), MANIFEST["dictionary"]]
                   for path in [directory / item["filename"]])
    _verify_files(str(directory), stamps)
    return directory


def availability():
    try:
        for package, version in DEPENDENCIES.items():
            if metadata.version(package) != version:
                raise OCRError("OCR 依赖版本与固定配置不一致。")
        model_directory()
        return {"available": True, "reason": None}
    except (OSError, metadata.PackageNotFoundError, OCRError):
        return {"available": False, "reason": "OCR 模型或固定依赖尚未就绪。"}


def _load_engine(directory):
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import EngineType, ModelType, OCRVersion
    import cv2

    cv2.setNumThreads(1)
    params = {
        "Global.use_cls": False, "Global.use_det": True, "Global.use_rec": True,
        "Global.log_level": "critical", "Global.max_side_len": 1280,
        "Global.text_score": MIN_CONFIDENCE, "Global.return_word_box": False,
        "Global.return_single_char_box": False, "Global.model_root_dir": str(directory),
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        "EngineConfig.onnxruntime.use_cuda": False, "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_cann": False, "EngineConfig.onnxruntime.use_coreml": False,
        "Det.limit_type": "max", "Det.limit_side_len": 960, "Det.max_candidates": 200,
        "Rec.rec_batch_num": 1,
        "Rec.rec_keys_path": str(directory / MANIFEST["dictionary"]["filename"]),
    }
    for name in ("Det", "Rec"):
        params.update({f"{name}.engine_type": EngineType.ONNXRUNTIME,
                       f"{name}.model_type": ModelType.TINY,
                       f"{name}.ocr_version": OCRVersion.PPOCRV6,
                       f"{name}.model_path": str(directory / MANIFEST["models"][name.lower()]["filename"])})
    return RapidOCR(params=params)


def _clean(value):
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_fields(lines):
    """Accept labelled/structural evidence; ambiguous or low-confidence fields stay absent."""
    text = [_clean(value) for value, confidence in lines if confidence >= MIN_CONFIDENCE and len(value) <= 400]
    found = {key: set() for key in ("title", "cinema", "hall", "seat", "showtime")}
    patterns = {
        "title": r"^(?:片名|影片(?:名称)?|电影名称|Movie|Film|Title)\s*[:：]\s*(.{1,120})$",
        "cinema": r"^(?:影院|影城|Cinema|Theatre|Theater)\s*[:：]\s*(.{1,200})$",
        "hall": r"^(?:影厅|Hall|Screen)\s*[:：]\s*(.{1,40})$",
        "seat": r"^(?:座位|Seat)\s*[:：]\s*(.{1,40})$",
    }
    for line in text:
        for key, pattern in patterns.items():
            if match := re.fullmatch(pattern, line, re.IGNORECASE):
                value = match[1].strip()
                if key == "seat" and (seat := re.fullmatch(r"(\d{1,2})\s*排\s*(\d{1,3})\s*(?:座|号)", value)):
                    value = f"{int(seat[1])}排{int(seat[2])}座"
                found[key].add(value)
        if match := re.fullmatch(r"《([^《》]{1,120})》", line):
            found["title"].add(match[1].strip())
        if re.fullmatch(r"[^:：]{2,80}(?:电影院|影城|影院)", line):
            found["cinema"].add(line)
        if match := re.search(r"(?<!\w)((?:(?:IMAX|杜比|VIP)\s*)?\d{1,2}\s*(?:号)?厅)(?!\w)", line, re.IGNORECASE):
            found["hall"].add(re.sub(r"\s+", "", match[1]))
        if match := re.search(r"(?<!\d)(\d{1,2})\s*排\s*(\d{1,3})\s*(?:座|号)(?!\d)", line):
            found["seat"].add(f"{int(match[1])}排{int(match[2])}座")
        # A bare date may be a purchase/printing date whose label OCR split into
        # another box. Require explicit evidence in this same recognized line.
        if re.search(r"购票|订单|打印|购买|purchase|print|order", line, re.IGNORECASE):
            continue
        if not re.match(r"^(?:放映(?:时间)?|开场(?:时间)?|场次|Showtime|Show time|Showing|Show|Starts?)\s*[:：]\s*", line, re.IGNORECASE):
            continue
        for match in re.finditer(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?[ T]*(\d{1,2})[:：](\d{2})(?!\d)", line):
            try:
                found["showtime"].add(datetime(*map(int, match.groups())).isoformat(timespec="minutes"))
            except ValueError:
                pass
    return {key: next(iter(values)) for key, values in found.items() if len(values) == 1}


def recognize(raw: bytes):
    """Called only in the worker's terminable OCR process; returns five fields at most."""
    global _engine, _engine_directory
    try:
        if not raw or len(raw) > 12 * 1024 * 1024:
            raise OCRError("OCR 图片超出限制。")
        directory = model_directory()
        with _lock:
            if _engine is None or _engine_directory != directory:
                if not availability()["available"]:
                    raise OCRError("OCR 尚未就绪。")
                _engine = _load_engine(directory)
                _engine_directory = directory
            with Image.open(BytesIO(raw)) as source:
                if source.width * source.height > 40_000_000:
                    raise OCRError("OCR 图片尺寸超出限制。")
                picture = ImageOps.exif_transpose(source).convert("RGB")
                picture.thumbnail((1280, 1280))
            with picture:
                result = _engine(picture)
            if result.txts is None or result.scores is None:
                return {}
            return parse_fields(zip(result.txts[:200], result.scores[:200]))
    except OCRError:
        raise
    except Exception:
        # Library errors may contain decoded text or image paths: never forward them.
        raise OCRError("OCR 识别失败，请稍后重试或手动填写。") from None
