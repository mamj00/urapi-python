"""Offline installed-wheel smoke test; only an ephemeral synthetic loopback server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import zipfile


def main() -> None:
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        allowed = {"urapi/__init__.py", "urapi/client.py", "urapi/operations.json", "urapi/py.typed", "urapi/wait_for_job.py"}
        assert all(name in allowed or name.startswith("urapi_sdk-0.0.1.dist-info/") for name in names)
        assert allowed.issubset(names)
        assert "Requires-Python: >=3.11" in archive.read("urapi_sdk-0.0.1.dist-info/METADATA").decode()
    seen = []
    polls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            seen.append((self.path, self.headers.get("Idempotency-Key"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id":"job_installed_fixture"}')
        def do_GET(self):
            polls.append((self.path, self.headers.get("Idempotency-Key"), self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            state = "queued" if len(polls) == 1 else "succeeded"
            self.wfile.write(json.dumps({"id": "job_installed_fixture", "state": state}).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="urapi-wheel-consumer-") as directory:
            target = Path(directory) / "installed"
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", "--target", str(target), str(wheel)], check=True, capture_output=True)
            code = """
import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import urapi
assert Path(urapi.__file__).is_relative_to(Path(sys.argv[1]))
assert len(urapi.OPERATION_IDS)==45
assert urapi.__version__=='0.0.1'
client=urapi.UrapiClient(base_url=sys.argv[2],api_key='ur_test_'+'a'*24+'.'+'b'*43,allow_http_loopback_for_tests=True)
result=client.request('createQR',{'body':{'text':'Installed fixture','format':'png','max_credits':'1.000'},'idempotency_key':'00000000-0000-4000-8000-000000000001'})
assert result.data['id']=='job_installed_fixture'
final=urapi.wait_for_job(client,result.data['id'],timeout_seconds=2,poll_interval_seconds=0.001,max_polls=2)
assert final.data['state']=='succeeded'
print(json.dumps({'installed_consumer':True,'operations':len(urapi.OPERATION_IDS),'status':result.status,'wait_state':final.data['state']}))
"""
            result = subprocess.run([sys.executable, "-I", "-c", code, str(target), f"http://127.0.0.1:{server.server_port}"], cwd=directory, check=True, capture_output=True, text=True, timeout=30)
            assert seen == [("/v1/qr", "00000000-0000-4000-8000-000000000001", {"text": "Installed fixture", "format": "png", "max_credits": "1.000"})]
            assert polls == [("/v1/jobs/job_installed_fixture", None, "Bearer ur_test_" + "a"*24 + "." + "b"*43)]*2
            print(result.stdout.strip())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
