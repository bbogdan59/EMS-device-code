from urllib.parse import urlsplit
import httpx


class API:
    def __init__(self, base_url, credentials=None, *, transport=None):
        url = urlsplit(base_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("HTTPS required except loopback development")
        if not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("platform_url must be an origin without credentials/path/query")
        self.origin = base_url.rstrip("/")
        self.credentials = credentials
        self.client = httpx.Client(base_url=self.origin, timeout=10, follow_redirects=False,
                                   trust_env=False, transport=transport)

    def call(self, method, path, payload=None, *, authenticated=True):
        headers = {}
        if authenticated:
            if not self.credentials:
                raise ValueError("Device not claimed")
            headers["Authorization"] = "Bearer " + self.credentials["device_id"] + "." + self.credentials["credential_secret"]
        response = self.client.request(method, "/api/v1" + path, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    def enroll(self, payload):
        return self.call("POST", "/devices/enroll", payload, authenticated=False)

    def rotate_credential(self):
        """Authenticates with the CURRENT secret; the server revokes it
        immediately and returns the new one once. See state.py for how a
        lost/ambiguous response is made safe rather than silently retried."""
        return self.call("POST", "/devices/credentials/rotate", {})

    def close(self):
        self.client.close()
