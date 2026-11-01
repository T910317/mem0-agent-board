"""Bearer transport and wake-budget regressions; only local servers and fake tokens."""
import datetime as dt
import importlib.util
import ipaddress
import json
import ssl
import threading
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-mcp-bearer"
RESULT = [{"id": "local-message", "from": "user"}]
PAYLOAD = {"jsonrpc": "2.0", "id": 1, "result": {
    "content": [{"type": "text", "text": json.dumps(RESULT)}]}}


def load_module(relative):
    spec = importlib.util.spec_from_file_location("isolated_transport", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def transport(request):
    name = request.param
    if name.startswith("relay"):
        module = load_module("relay/relay.py")
        module.httpx = None if name == "relay-urllib" else httpx
        call = lambda url: module.rpc(url, TOKEN, "inbox", {})
    elif name == "client":
        module = load_module("client/rt_client.py")
        call = lambda url: module.call((url, TOKEN), "inbox", {})
    else:
        module = load_module("client/roundtable_skills_sync.py")
        call = lambda url: module.get(url, TOKEN)
    yield SimpleNamespace(name=name, module=module, call=call)
    if getattr(module, "_client", None) is not None:
        module._client.close()


@contextmanager
def local_server(tls=None):
    state = SimpleNamespace(seen=[], location="", code=302, response="json")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.seen.append((self.path, self.command, self.headers.get("Authorization"), body))
            if self.path == "/redirect":
                self.send_response(state.code)
                self.send_header("Location", state.location)
                raw = b""
            else:
                payload = PAYLOAD
                if state.response == "error":
                    payload = {"result": {"isError": True, "content": [{"text": "test error"}]}}
                raw = json.dumps(payload).encode()
                if state.response == "sse":
                    raw = b"event: message\ndata: " + raw + b"\n\n"
                self.send_response(401 if state.response == "unauthorized" else 200)
                self.send_header("Content-Type", "text/event-stream" if state.response == "sse" else "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_POST = respond
        do_GET = respond

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls is not None:
        srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    state.url = f"{'https' if tls else 'http'}://127.0.0.1:{srv.server_port}"
    thread = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def tls_contexts(tmp_path_factory):
    # Generate a disposable self-signed localhost certificate; never load user keys.
    directory = tmp_path_factory.mktemp("local-tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    return server_context, ssl.create_default_context(cafile=str(cert_path))


@pytest.mark.parametrize("transport", ["relay-urllib", "relay-httpx", "client", "skills"], indirect=True)
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["relative", "same-origin", "cross-origin", "scheme-relative", "downgrade"])
def test_bearer_requests_reject_redirects(transport, code, destination, tls_contexts, monkeypatch):
    server_tls, client_tls = tls_contexts
    if destination == "downgrade":
        monkeypatch.setattr(ssl, "_create_default_https_context", lambda **kwargs: client_tls)
        if transport.name == "relay-httpx":
            transport.module._client = httpx.Client(verify=client_tls, trust_env=False)
    with local_server(server_tls if destination == "downgrade" else None) as source, local_server() as target:
        source.code = code
        source.location = {
            "relative": "/destination",
            "same-origin": source.url + "/destination",
            "cross-origin": target.url + "/destination",
            "scheme-relative": target.url.removeprefix("http:") + "/destination",
            "downgrade": target.url + "/destination",
        }[destination]
        error_type = httpx.HTTPStatusError if transport.name == "relay-httpx" else urllib.error.HTTPError
        with pytest.raises(error_type) as caught:
            transport.call(source.url + "/redirect")
        error = caught.value
        assert (error.response.status_code if transport.name == "relay-httpx" else error.code) == code
        assert len(source.seen) == 1
        assert source.seen[0][2] == "Bearer " + TOKEN
        assert target.seen == []


@pytest.mark.parametrize("transport", ["relay-urllib", "relay-httpx", "client", "skills"], indirect=True)
@pytest.mark.parametrize("response", ["json", "sse", "unauthorized", "error"])
def test_direct_authenticated_requests_keep_behavior(transport, response):
    with local_server() as source:
        source.response = response
        error_type = httpx.HTTPStatusError if transport.name == "relay-httpx" else urllib.error.HTTPError
        if response == "unauthorized":
            with pytest.raises(error_type):
                transport.call(source.url + "/mcp")
        elif response == "error" and transport.name != "skills":
            with pytest.raises(RuntimeError):
                transport.call(source.url + "/mcp")
        else:
            result = transport.call(source.url + "/mcp")
            assert isinstance(result, bytes) if transport.name == "skills" else result == RESULT
        assert len(source.seen) == 1
        assert source.seen[0][2] == "Bearer " + TOKEN
        if transport.name != "skills":
            assert json.loads(source.seen[0][3])["params"] == {"name": "inbox", "arguments": {}}


@pytest.mark.parametrize("legacy_setting", [None, "relay-test", "qa,relay-test"])
@pytest.mark.parametrize("session", ["relay-test", "qa", "ordinary", ""])
def test_sender_session_cannot_bypass_wake_budget(tmp_path, legacy_setting, session):
    relay = load_module("relay/relay.py")
    relay.log = lambda message: None
    env = {"RT_URL": "http://unused.invalid/mcp", "RT_TOKEN": TOKEN, "RELAY_TOKEN": "fake-relay",
           "RELAY_DATA": str(tmp_path), "MAX_PER_HOUR": "3", "GRACE_SECONDS": "0"}
    if legacy_setting is not None:
        env["TEST_SESSIONS"] = legacy_setting
    cfg = relay.Config(env)
    r = relay.Relay(cfg)
    target = {"label": "recipient", "session": "local", "is_codex": False}
    messages = [{"id": str(i), "from": "claude", "from_session": session, "needs_action": True}
                for i in range(20)]
    assert r._absorb(target, messages, 10000) == 3
    assert len(r.queue) == len(r.wakes["recipient"]) == 3
    assert r._absorb(target, messages, 10060) == 0
    # The server-authenticated user identity keeps its documented exemption.
    messages.append({"id": "user-message", "from": "user", "from_session": session})
    assert r._absorb(target, messages, 10060) == 1
    assert len(r.wakes["recipient"]) == 3
    r.save()
    reloaded = relay.Relay(cfg)
    assert reloaded._absorb(target, messages, 10061) == 0
    assert reloaded._absorb(target, messages, 13601) == 3
