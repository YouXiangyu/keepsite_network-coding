"""Explicit, hash-locked OCR preparation. Never called by the application."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

MANIFEST = Path(__file__).resolve().parents[1] / "backend/ocr-models.json"
MAX_MODEL_BYTES = 96 * 1024 * 1024


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Model download redirected away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def publish_dictionary(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Existing dictionary differs; not overwritten")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".ocr-dictionary-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare(destination, execute=False):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    print(json.dumps({"destination": str(destination), "execute": execute,
                      "models": manifest["models"]}, ensure_ascii=False, indent=2))
    if not execute:
        return
    destination.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(HTTPSRedirect())
    for model in manifest["models"].values():
        path = destination / model["filename"]
        if path.exists():
            with path.open("rb") as stream:
                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_hash != model["sha256"]:
                raise ValueError(f"Existing model differs; not overwritten: {path.name}")
            continue
        descriptor, temporary = tempfile.mkstemp(prefix=".ocr-download-", dir=destination)
        try:
            digest, size = hashlib.sha256(), 0
            request = urllib.request.Request(model["url"], headers={"User-Agent": "MemoVerse-OCR-Setup/1.0"})
            with os.fdopen(descriptor, "wb") as output, opener.open(request, timeout=30) as response:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_MODEL_BYTES:
                        raise ValueError("Model exceeds download size limit")
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != model["sha256"]:
                raise ValueError("Model SHA-256 mismatch")
            # An exclusive hard-link publication cannot replace a concurrent output.
            os.link(temporary, path)
            print(f"Verified {path.name}: {size} bytes")
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Export the pinned model's embedded dictionary so the library can never
    # enter its missing-dictionary download path at application runtime.
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(str(destination / manifest["models"]["rec"]["filename"]),
                                   sess_options=options, providers=["CPUExecutionProvider"])
    characters = session.get_modelmeta().custom_metadata_map.get("character", "")
    if not characters or len(characters.splitlines()) < 100:
        raise ValueError("Pinned recognition model lacks its embedded dictionary")
    raw = ("\n".join(characters.splitlines()) + "\n").encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != manifest["dictionary"]["sha256"]:
        raise ValueError("Embedded dictionary differs from fixed manifest")
    publish_dictionary(destination / manifest["dictionary"]["filename"], raw)
    print(json.dumps({"dictionarySha256": hashlib.sha256(raw).hexdigest(),
                      "characters": len(characters.splitlines()), "dictionaryBytes": len(raw)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    prepare(args.destination.resolve(), args.execute)
