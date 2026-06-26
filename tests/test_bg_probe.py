"""bg_probe：端口推断与本地 TCP 探测（跨平台）。"""

from __future__ import annotations

import socket
import threading

from smartclaw.agent.bg_probe import infer_listen_for_probe, probe_local_listen


def test_infer_flask_primary_url_hint():
    t = infer_listen_for_probe("flask run --host 0.0.0.0 --port 5500")
    assert t == ("127.0.0.1", 5500)


def test_infer_uvicorn_bind():
    r = infer_listen_for_probe(
        "uvicorn app.main:factory --factory --bind 192.168.1.22:8899 --reload"
    )
    assert r == ("192.168.1.22", 8899)


def test_infer_uvicorn_port_only():
    r = infer_listen_for_probe("uvicorn app:foo --host 127.0.0.1 --port 7788")
    assert r == ("127.0.0.1", 7788)


def test_infer_gunicorn():
    cmd = "/opt/bin/gunicorn -b 0.0.0.0:3333 app:app"
    assert infer_listen_for_probe(cmd) == ("127.0.0.1", 3333)


def test_probe_local_listen_tcp_ok(monkeypatch):
    monkeypatch.setenv("SMARTCLAW_BG_PROBE_SECONDS", "3")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stopped = threading.Event()

    def _srv() -> None:
        while not stopped.is_set():
            try:
                listener.settimeout(0.25)
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            else:
                try:
                    conn.close()
                except OSError:
                    pass
        try:
            listener.close()
        except OSError:
            pass

    threading.Thread(target=_srv, daemon=True).start()
    try:
        cmd = f"python -m uvicorn demo:app --port {port}"
        ok, tries, tgt, detail, elapsed = probe_local_listen(cmd)
        assert ok is True
        assert tries >= 1
        assert elapsed < 10
        assert "127.0.0.1" in tgt or "::1" in tgt
        assert detail == "" or detail.startswith(("http=", "http_err="))
    finally:
        stopped.set()


def test_budget_zero(monkeypatch):
    monkeypatch.setenv("SMARTCLAW_BG_PROBE_SECONDS", "0")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    cmd = f"python -m uvicorn demo:app --port {port}"
    ok, *_ = probe_local_listen(cmd)
    assert ok is False
