"""Driver for the raw Lucene stage. The Lucene itself is in lucene_raw/Search.java.

Lucene is a Java library with no server, so something has to bridge it.
PyLucene needs a JCC build that teaches nothing about Lucene, so instead a
small JVM process speaks JSON over loopback HTTP.

That bridge costs ~0.3-0.8 ms per query, which is stated in the results table
rather than hidden: this is the only engine here whose measured latency
includes a transport the engine itself does not require.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from typing import Iterable

import requests

from core.common import Doc, Hit, IndexStats, Query, write_jsonl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "lucene_raw")
LIB_DIR = os.path.join(RAW_DIR, "lib")
INDEX_DIR = os.path.join(RAW_DIR, "index")
SRC = os.path.join(RAW_DIR, "Search.java")
JAVA_FLAGS = ["--enable-native-access=ALL-UNNAMED"]
PORT = int(os.environ["LUCENE_PORT"]) if os.environ.get("LUCENE_PORT") else 0


def free_port() -> int:
    """A fixed port is a trap: a JVM left over from an earlier run still serves
    the old segments, so a stale index would be queried silently."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ensure_jars() -> None:
    if not os.path.isdir(LIB_DIR) or not os.listdir(LIB_DIR):
        subprocess.run([os.path.join(RAW_DIR, "fetch_jars.sh")], check=True)


def _java(*args: str) -> list[str]:
    return ["java", *JAVA_FLAGS, "-cp", f"{LIB_DIR}/*", SRC, *args]


class LuceneEngine:
    name = "lucene"

    def __init__(self, port: int = 0):
        if not shutil.which("java"):
            raise RuntimeError("java not on PATH; Lucene 9.11 needs Java 17+")
        ensure_jars()
        self.port = port or PORT or free_port()
        self.url = f"http://localhost:{self.port}/search"
        self.proc: subprocess.Popen | None = None
        self.session = requests.Session()

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        tmp = os.path.join(RAW_DIR, "_index_input.jsonl")
        write_jsonl(tmp, (d.to_json() for d in docs))
        try:
            proc = subprocess.run(_java("index", tmp, INDEX_DIR), capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"lucene index failed:\n{proc.stderr[-2000:]}")
            # JVM logs go to stderr; stdout is the JSON.
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            os.remove(tmp)

        self.start()
        return IndexStats(
            docs=payload["docs"],
            build_s=payload["build_s"],
            size_bytes=payload["size_bytes"],
            notes=payload.get("notes", ""),
        )

    def start(self) -> None:
        """Restart so the server opens a reader over the new index: a
        DirectoryReader is a point-in-time snapshot of the segments."""
        self.stop()
        self.proc = subprocess.Popen(
            _java("serve", INDEX_DIR, str(self.port)),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        atexit.register(self.stop)
        health = f"http://localhost:{self.port}/health"
        for _ in range(100):
            try:
                if self.session.get(health, timeout=1).ok:
                    return
            except requests.RequestException:
                time.sleep(0.2)
        raise RuntimeError("lucene server did not come up; run the java command by hand")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _post(self, payload: dict) -> dict:
        if self.proc is None:  # --skip-index never called index(), so no JVM yet
            self.start()
        resp = self.session.post(self.url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data

    @staticmethod
    def _payload(q: Query) -> dict:
        return {
            "kind": q.kind,
            "text": q.text,
            "must": list(q.must),
            "should": list(q.should),
            "must_not": list(q.must_not),
            "date_from": q.date_from,
            "facet_field": q.facet_field,
            "sort_field": q.sort_field,
            "offset": q.offset,
            "limit": q.limit,
            "boosts": [[f, w] for f, w in q.boosts],
        }

    def search(self, q: Query) -> list[Hit]:
        data = self._post(self._payload(q))
        return [
            Hit(
                id=h["id"],
                score=float(h.get("score", 0.0)),
                title=h.get("title", ""),
                highlight=h.get("highlight", ""),
            )
            for h in data.get("hits", [])
        ]

    def count(self, q: Query) -> int:
        return int(self._post({**self._payload(q), "count_only": True})["total"])

    def facet(self, q: Query, top: int = 10) -> list[tuple[str, int]]:
        data = self._post({**self._payload(q), "kind": "facet"})
        return [(v, int(c)) for v, c in data.get("facets", [])][:top]

    def close(self) -> None:
        self.stop()
        self.session.close()
