"""Server-side encode queue for clips the browser can't compress itself.

Gradio's component server functions run inside its async request handler, so
a long encode can't run in one: the page calls start/status/cancel, and a
single worker thread runs the encodes one at a time (2 vCPUs: concurrent
x264 jobs only slow each other down). This replaces the Gradio queue's
concurrency limit and max size for the server path.
"""
import collections
import secrets
import threading
import time

from compressor import CompressionCancelled

MAX_QUEUED = 8
# Finished job records (not their files) are forgotten after this long.
JOB_RECORD_TTL_SECONDS = 3600


class QueueFull(Exception):
    pass


class JobQueue:
    def __init__(self, compressor):
        self._compressor = compressor
        self._jobs = {}
        self._waiting = collections.deque()
        self._cv = threading.Condition()
        threading.Thread(target=self._worker, name="encode-worker", daemon=True).start()

    def submit(self, input_path, settings):
        """Queue an encode; settings are compress() keyword arguments. Returns the job id."""
        with self._cv:
            self._forget_old()
            if len(self._waiting) >= MAX_QUEUED:
                raise QueueFull(f"The server queue is full ({MAX_QUEUED} waiting). Try again in a minute.")
            # Unguessable: the id is all a client needs to poll, cancel or
            # download, and job ids name the output directories.
            job_id = secrets.token_hex(16)
            self._jobs[job_id] = {
                "state": "queued", "progress": 0.0, "desc": "", "output": None,
                "error": None, "input": input_path, "settings": settings, "finished": None,
            }
            self._waiting.append(job_id)
            self._cv.notify()
            return job_id

    def status(self, job_id):
        with self._cv:
            job = self._jobs.get(job_id)
            if not job:
                return None
            out = {k: job[k] for k in ("state", "progress", "desc", "output", "error")}
            if job["state"] == "queued":
                out["position"] = self._waiting.index(job_id) + 1
            return out

    def cancel(self, job_id):
        with self._cv:
            job = self._jobs.get(job_id)
            if not job or job["state"] not in ("queued", "running"):
                return
            if job["state"] == "queued":
                self._waiting.remove(job_id)
                self._finish(job, "cancelled")
                return
        # Running: kill ffmpeg outside the lock; the worker records the outcome.
        self._compressor.cancel(job_id)

    def _finish(self, job, state, **fields):
        job.update(state=state, finished=time.time(), **fields)

    def _forget_old(self):
        cutoff = time.time() - JOB_RECORD_TTL_SECONDS
        for job_id in [j for j, job in self._jobs.items() if job["finished"] and job["finished"] < cutoff]:
            del self._jobs[job_id]

    def _worker(self):
        while True:
            with self._cv:
                while not self._waiting:
                    self._cv.wait()
                job_id = self._waiting.popleft()
                job = self._jobs[job_id]
                job["state"] = "running"

            def progress(fraction, desc=""):
                job["progress"], job["desc"] = float(fraction), desc

            try:
                output = self._compressor.compress(
                    job_id=job_id, input_path=job["input"], progress_callback=progress, **job["settings"],
                )
                with self._cv:
                    self._finish(job, "done", output=output, progress=1.0)
            except CompressionCancelled:
                with self._cv:
                    self._finish(job, "cancelled")
            except Exception as e:  # surfaced to the page as the job's error
                with self._cv:
                    self._finish(job, "error", error=str(e))
