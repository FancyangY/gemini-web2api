"""Check a built Docker image without making requests to Gemini."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    image = sys.argv[1] if len(sys.argv) > 1 else "gemini-web2api:smoke"
    name = "gemini-smoke-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    api_key = "smoke-test-key"
    opener = build_opener(ProxyHandler({}))

    with tempfile.TemporaryDirectory() as directory:
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8")
        )
        config["api_keys"] = [api_key]
        config_path = Path(directory) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        # Allow the container's unprivileged user to read this test-only config.
        config_path.chmod(0o644)

        def start():
            docker(
                "run", "--detach", "--name", name,
                "--publish", "127.0.0.1::8081",
                "--mount", f"type=bind,source={config_path},target=/app/config.json,readonly",
                "--mount", f"type=volume,source={volume},target=/data",
                image,
            )
            port = docker("port", name, "8081/tcp").rsplit(":", 1)[1]
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    with opener.open(base_url + "/", timeout=2) as response:
                        assert json.load(response)["status"] == "ok"
                    return base_url
                except (URLError, OSError):
                    time.sleep(0.5)
            raise RuntimeError("Container did not become ready within 30 seconds")

        def request(base_url, path, payload=None):
            req = Request(
                base_url + path,
                data=None if payload is None else json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            with opener.open(req, timeout=5) as response:
                return json.load(response)

        try:
            docker("volume", "create", volume)
            base_url = start()
            assert docker("exec", name, "id", "-u") == "10001"
            docker("exec", name, "python", "-c", "import httpx")
            try:
                opener.open(base_url + "/v1/models", timeout=5).close()
            except HTTPError as exc:
                assert exc.code == 401, exc.code
            else:
                raise AssertionError("API accepted an unauthenticated request")
            assert request(base_url, "/v1/models")["data"]
            messages = [{"role": "system", "content": "Docker persistence check"}]
            cached = request(base_url, "/v1/caches", {"messages": messages, "ttl_seconds": 300})
            cache_id = cached["id"]

            # Recreate the container to ensure the data survives replacement.
            docker("rm", "--force", name)
            base_url = start()
            assert request(base_url, "/v1/caches/" + cache_id)["messages"] == messages
            print("Docker smoke checks passed: startup, non-root user, streaming dependency, auth, models, persistent cache.")
        except Exception:
            subprocess.run(["docker", "logs", name], check=False)
            raise
        finally:
            subprocess.run(["docker", "rm", "--force", name], check=False)
            subprocess.run(["docker", "volume", "rm", volume], check=False)


if __name__ == "__main__":
    main()
