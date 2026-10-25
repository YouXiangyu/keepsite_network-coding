"""Redis Streams delivery with durable SQL leases and terminable Qwen recognition."""
from contextlib import closing
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from pydantic import ValidationError
from redis.exceptions import RedisError, ResponseError

from . import processing_repository as repository
from .media import MAX_IMAGE_BYTES, image_info
from .object_storage import S3ObjectStore, StorageError
from .store import now_ms
from .ticket_recognition import RecognitionError, validate_fields

log = logging.getLogger(__name__)
RECOGNITION_TIMEOUT_SECONDS = 45
# Checked at stage/stream-return boundaries; synchronous S3 body reads are not
# forcibly interruptible by this budget. Only the recognition child has a hard timeout.
ATTEMPT_TIMEOUT_SECONDS = 85
TEMP_RETENTION_SECONDS = 24 * 60 * 60
TEMP_NAME = re.compile(r"^[a-f0-9]{32}\.image$")


class ProcessingInterrupted(Exception):
    pass


class ProcessingFailed(Exception):
    pass


def _recognition_child(raw, output):
    try:
        try:
            from .ticket_recognition import recognize
            result = {"fields": recognize(raw)}
        except RecognitionError as error:
            result = {"error": error.code}
        except BaseException:
            result = {"error": "invalid_response"}
        try:
            output.send(result)
        except (OSError, EOFError):
            pass  # The owner may already have stopped or timed out this attempt.
    finally:
        output.close()


def run_isolated_recognition(raw, *, stopping=lambda: False, timeout=RECOGNITION_TIMEOUT_SECONDS, target=None):
    """Every attempt owns its process and pipe; timeout/stop terminates the process."""
    if stopping():
        raise ProcessingInterrupted()
    context = multiprocessing.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=target or _recognition_child, args=(raw, outgoing), daemon=True)
    started = False
    try:
        process.start()
        started = True
        outgoing.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stopping():
                raise ProcessingInterrupted()
            if incoming.poll(0.05):
                try:
                    message = incoming.recv()
                except EOFError:
                    raise ProcessingFailed() from None
                if not isinstance(message, dict):
                    raise ProcessingFailed()
                if "error" in message:
                    raise RecognitionError(message["error"])
                return validate_fields(message.get("fields"))
            if not process.is_alive():
                raise ProcessingFailed()
        raise ProcessingFailed()
    finally:
        outgoing.close()
        incoming.close()
        if started:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()


class ProcessingQueue:
    GROUP = "workers"

    def __init__(self, client, server_id, consumer):
        self.client, self.consumer = client, consumer
        self.stream = "keepsite:processing:" + server_id

    def ensure(self):
        try:
            self.client.xgroup_create(self.stream, self.GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def publish(self, identifier):
        self.client.xadd(self.stream, {"jobId": identifier}, maxlen=10_000, approximate=True)

    def receive(self):
        claimed = self.client.xautoclaim(self.stream, self.GROUP, self.consumer,
                                        min_idle_time=repository.LEASE_MS, start_id="0-0", count=1)
        messages = claimed[1]
        if not messages:
            result = self.client.xreadgroup(self.GROUP, self.consumer, {self.stream: ">"}, count=1, block=100)
            messages = result[0][1] if result else []
        if not messages:
            return None
        message_id, fields = messages[0]
        identifier = fields.get(b"jobId", fields.get("jobId"))
        try:
            if isinstance(identifier, bytes):
                identifier = identifier.decode("ascii")
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, AttributeError):
            self.acknowledge(message_id)
            return None
        return message_id, identifier

    def acknowledge(self, message_id):
        self.client.xack(self.stream, self.GROUP, message_id)
        self.client.xdel(self.stream, message_id)


class ProcessingWorker:
    def __init__(self, store, shared_state, *, provider=None, recognition_runner=None, recognition_status=None):
        self.store = store
        self.stopping, self.waking = threading.Event(), threading.Event()
        self.owner = str(uuid.uuid4())
        self.provider = provider
        self.recognition_runner = recognition_runner or run_isolated_recognition
        self.thread = threading.Thread(target=self._loop, name="keepsite-ticket-processing", daemon=True)
        self.queue = None
        self._scanner = None
        self._resources_closed = False
        reason = None
        if not isinstance(store.storage, S3ObjectStore):
            reason = "需要先配置私有云存储，才能运行电影票处理。"
        elif shared_state.client is None:
            reason = "Redis 处理队列尚未配置。"
        else:
            if recognition_status is None:
                from .ticket_recognition import availability
                recognition_status = availability()
            if not recognition_status["available"]:
                reason = recognition_status.get("reason") or "电影票识别接口尚未就绪。"
        if reason is None:
            if self.provider is None:
                from .tmdb_posters import configured_provider
                self.provider = configured_provider()
            status = self.provider.availability()
            if not status["available"]:
                reason = status.get("reason") or "海报来源暂不可用。"
        if reason is None:
            with store.transaction() as db:
                server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
            self.queue = ProcessingQueue(shared_state.client, server, self.owner)
        store.processing_enabled = self.queue is not None
        store.processing_unavailable_reason = reason
        store.processing_wake = self.waking.set

    def start(self):
        if self.queue is not None or repository.has_active(self.store):
            self.thread.start()

    def stop(self):
        self.stopping.set()
        self.waking.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=25)
        if self.thread.is_alive():
            log.warning("Ticket processing is finishing a bounded network request")
        else:
            self._close_resources()

    def _close_resources(self):
        if self._resources_closed:
            return
        self._resources_closed = True
        if self._scanner is not None:
            self._scanner.close()
            self._scanner = None
        if self.provider is not None:
            self.provider.close()

    def _loop(self):
        try:
            while not self.stopping.is_set():
                try:
                    if self.run_once():
                        continue
                except (RedisError, sqlite3.Error):
                    log.warning("Ticket processing delivery temporarily unavailable; durable intentions retained")
                self.waking.wait(1)
                self.waking.clear()
        finally:
            self._close_resources()

    def reconcile(self):
        self.queue.ensure()
        for identifier in repository.due_jobs(self.store):
            if self.stopping.is_set():
                break
            self.queue.publish(identifier)
            repository.mark_enqueued(self.store, identifier)

    def run_once(self):
        if self.stopping.is_set():
            return False
        repository.expire_jobs(self.store)
        self.cleanup_temporary()
        if self.queue is None:
            return False
        self.reconcile()
        message = self.queue.receive()
        if message is None:
            return False
        message_id, identifier = message
        job = repository.claim(self.store, identifier, self.owner)
        if job is None:
            self.queue.acknowledge(message_id)
            return True
        completed = False
        try:
            prepared = self._prepare(job)
            if prepared is False:
                completed = repository.fail(self.store, job, interrupted=True)
            else:
                # Publishing never repeats recognition/download/upload. Its artifact is
                # durable, and a later lease can independently verify and publish.
                for attempt in range(3):
                    try:
                        completed = repository.finish(self.store, job, prepared)
                        break
                    except sqlite3.Error:
                        if attempt == 2:
                            raise
                        self.stopping.wait(0.1 * (attempt + 1))
                if not completed:
                    completed = repository.fail(self.store, job, interrupted=True)
        except ProcessingInterrupted:
            completed = repository.fail(self.store, job, interrupted=True)
        except RecognitionError as error:
            completed = repository.fail(self.store, job, retryable=error.retryable)
        except (ProcessingFailed, StorageError, OSError, ValidationError):
            completed = repository.fail(self.store, job)
        except sqlite3.Error:
            # Leave the message unacknowledged and lease intact if SQL publication
            # is unavailable. Restart/reconciliation will reclaim it after expiry.
            log.warning("Ticket processing publication deferred; prepared result retained")
        except Exception:
            # Providers can raise transport-specific exceptions. Never persist or
            # log their raw messages, URLs or decoded ticket text.
            completed = repository.fail(self.store, job)
        if completed:
            self.queue.acknowledge(message_id)
        return True

    def _prepare(self, job):
        deadline = time.monotonic() + ATTEMPT_TIMEOUT_SECONDS

        def stopping():
            return self.stopping.is_set() or time.monotonic() >= deadline or not repository.is_current(self.store, job)

        def check():
            if stopping():
                raise ProcessingInterrupted()

        check()
        prepared = repository.artifact(self.store, job)
        if prepared is not None and self._store_artifact(prepared, stopping):
            check()
            return prepared
        if job["ocr_fields"] is None:
            raw = self._original(job, stopping)
            check()
            candidates = self.recognition_runner(raw, stopping=stopping, timeout=min(RECOGNITION_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic())))
            check()
            fields = repository.save_ocr(self.store, job, candidates)
            if fields is None:
                return False
        else:
            fields = repository.matching_fields(self.store, job)
        check()
        if not fields or not fields["title"]:
            return None
        candidate = self.provider.find(fields["title"], fields["showtime"], stopping=stopping)
        check()
        if candidate is None:
            return None
        info = image_info(candidate.content)
        if info["mimeType"] != candidate.mime_type or info["width"] != candidate.width or info["height"] != candidate.height:
            raise ProcessingFailed()
        descriptor = {"id": info["assetId"], "sha256": info["sha256"], "path": "media/" + info["filename"],
                      "mimeType": info["mimeType"], "size": info["size"], "width": info["width"], "height": info["height"]}
        source = {"provider": candidate.provider, "sourcePage": candidate.source_page,
                  "sourceFile": candidate.source_file, "fetchedAt": candidate.fetched_at}
        if candidate.provider == "tmdb":
            source["termsUrl"] = "https://www.themoviedb.org/api-terms-of-use"
        else:
            source.update(licenseName=candidate.license_name, licenseUrl=candidate.license_url, author=candidate.author)
        from .posters import validate_source
        if not validate_source(source):
            raise ProcessingFailed()
        temp_name = uuid.uuid4().hex + ".image"
        path = self._temp_path(temp_name, create=True)
        with path.open("xb") as outgoing:
            outgoing.write(candidate.content)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        try:
            identifier = repository.prepare_artifact(self.store, job, descriptor, source, temp_name)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if identifier is None:
            path.unlink(missing_ok=True)
            return False
        prepared = repository.artifact(self.store, job)
        if prepared is None:
            path.unlink(missing_ok=True)
            return False
        if not self._store_artifact(prepared, stopping):
            raise ProcessingFailed()
        check()
        return prepared

    def _original(self, job, stopping):
        with self.store.transaction() as db:
            asset = dict(self.store.asset(db, job["user_id"], job["original_asset_id"]))
        result, digest = bytearray(), hashlib.sha256()
        with closing(self.store.storage.open(asset["object_key"])) as incoming:
            if incoming.size != asset["size"] or incoming.size > MAX_IMAGE_BYTES:
                raise ProcessingFailed()
            while chunk := incoming.read(min(512 * 1024, MAX_IMAGE_BYTES - len(result) + 1)):
                if stopping():
                    raise ProcessingInterrupted()
                result.extend(chunk)
                if len(result) > asset["size"] or len(result) > MAX_IMAGE_BYTES:
                    raise ProcessingFailed()
                digest.update(chunk)
        if len(result) != asset["size"] or digest.hexdigest() != asset["id"]:
            raise ProcessingFailed()
        return bytes(result)

    def _temp_path(self, name, *, create=False):
        if not TEMP_NAME.fullmatch(name):
            raise ProcessingFailed()
        folder = self._temp_folder()
        if create:
            folder.mkdir(exist_ok=True)
            folder = self._temp_folder()
        path = folder / name
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ProcessingFailed()
        return path

    def _temp_folder(self):
        folder = self.store.root / "tmp" / "processing"
        for current in (self.store.root / "tmp", folder):
            if (current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction())
                    or not current.resolve().is_relative_to(self.store.root)):
                raise ProcessingFailed()
        return folder

    def _record_cleanup(self, prepared, reason):
        try:
            with self.store.transaction(True, timeout=1) as db:
                db.execute("INSERT OR IGNORE INTO processing_cleanup VALUES (?,?,?,?)",
                           (prepared["temp_name"], prepared.get("id"), reason, now_ms()))
        except sqlite3.Error:
            log.warning("Processing temporary-file cleanup record deferred")

    def cleanup_temporary(self, *, maximum=200):
        """Rotate through one owned directory, retaining every active artifact."""
        try:
            folder = self._temp_folder()
        except (ProcessingFailed, OSError):
            log.warning("Refused unsafe processing temporary directory")
            return 0
        if not folder.is_dir():
            return 0
        if self._scanner is None:
            try:
                self._scanner = os.scandir(folder)
            except OSError:
                return 0
        removed = 0
        for _ in range(maximum):
            try:
                entry = next(self._scanner)
            except StopIteration:
                self._scanner.close()
                self._scanner = None
                break
            if self.stopping.is_set():
                break
            if not TEMP_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime > time.time() - TEMP_RETENTION_SECONDS:
                    continue
                with self.store.transaction() as db:
                    artifact = db.execute("SELECT a.id,j.status FROM processing_artifacts a JOIN processing_jobs j ON j.id=a.job_id WHERE a.temp_name=?", (entry.name,)).fetchone()
                    if artifact and artifact["status"] in repository.ACTIVE:
                        continue
                path = self._temp_path(entry.name)
                path.unlink(missing_ok=True)
                self._record_cleanup({"temp_name": entry.name, "id": artifact["id"] if artifact else None}, "terminal_expired" if artifact else "orphan_expired")
                removed += 1
            except ProcessingFailed:
                self._scanner.close()
                self._scanner = None
                log.warning("Refused changed processing temporary directory")
                return removed
            except OSError:
                log.warning("Processing temporary-file cleanup deferred")
        return removed

    def _store_artifact(self, prepared, stopping):
        descriptor = json.loads(prepared["descriptor"])
        path = self._temp_path(prepared["temp_name"])
        if stopping():
            raise ProcessingInterrupted()
        if self.store.storage.verify(descriptor["path"], descriptor["size"], descriptor["sha256"], descriptor["mimeType"], stopping=stopping):
            path.unlink(missing_ok=True)
            self._record_cleanup(prepared, "cloud_verified")
            return True
        if not path.is_file():
            return False
        with path.open("rb") as incoming:
            raw = incoming.read(MAX_IMAGE_BYTES + 1)
        if len(raw) != descriptor["size"] or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise ProcessingFailed()
        self.store.storage.put_bytes(descriptor["path"], raw, descriptor["sha256"], descriptor["mimeType"])
        if stopping():
            raise ProcessingInterrupted()
        if not self.store.storage.verify(descriptor["path"], descriptor["size"], descriptor["sha256"], descriptor["mimeType"], stopping=stopping):
            raise ProcessingFailed()
        path.unlink(missing_ok=True)
        self._record_cleanup(prepared, "cloud_verified")
        return True
