#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
from pathlib import Path


def main() -> int:
    log_path = Path(os.environ["FAKE_CODEX_LOG"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("ARGV " + " ".join(sys.argv[1:]) + "\n")
        handle.flush()
        index_path = os.environ.get("CODEX_HERDER_SESSION_INDEX")
        fake_id = os.environ.get("FAKE_CODEX_ID", "fake-codex-id")
        fake_thread = os.environ.get("FAKE_CODEX_THREAD", "fake-thread")
        if index_path:
            idx = Path(index_path)
            idx.parent.mkdir(parents=True, exist_ok=True)
            with idx.open("a", encoding="utf-8") as session_index:
                session_index.write(json.dumps({"id": fake_id, "thread_name": fake_thread, "updated_at": "2026-04-04T00:00:00Z"}) + "\n")
        print("FAKE CODEX START", flush=True)
        for line in sys.stdin:
            handle.write("STDIN " + line)
            handle.flush()
            print("ECHO " + line.rstrip("\n"), flush=True)
            if line.strip() == "/exit":
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
