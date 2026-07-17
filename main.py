"""Baha Baraj Doluluk Paneli için bağımlılıksız yerel web sunucusu."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import date
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from baha_assets import ICON_192, ICON_512


HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "templates" / "index.html"

LOGIN_URL = "https://giris.epias.com.tr/cas/v1/tickets"
ACTIVE_FULLNESS_URL = (
    "https://seffaflik.epias.com.tr/electricity-service/v1/dams/data/active-fullness"
)
WEB_MANIFEST = {
    "id": "/",
    "name": "Baha Baraj Doluluk Paneli",
    "short_name": "Baha Baraj",
    "description": "Baha Baraj Doluluk Paneli — EPİAŞ verileri",
    "lang": "tr",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#ffffff",
    "theme_color": "#07539a",
    "icons": [
        {
            "src": "/icons/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ],
}
ICON_ROUTES = {
    "/favicon.ico": ICON_192,
    "/apple-touch-icon.png": ICON_192,
    "/apple-touch-icon-precomposed.png": ICON_192,
    "/icons/icon-192.png": ICON_192,
    "/icons/icon-512.png": ICON_512,
}

SESSION_COOKIE = "epias_session"
SESSION_IDLE_SECONDS = 30 * 60
SESSION_MAX_SECONDS = 115 * 60
MAX_REQUEST_BYTES = 16 * 1024
MAX_LOGIN_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60

SESSIONS: dict[str, dict[str, object]] = {}
LOGIN_FAILURES: dict[str, list[float]] = {}
STATE_LOCK = threading.Lock()

# Bazı VS Code çalışma ortamlarında HTTP(S)_PROXY, kullanılmayan
# 127.0.0.1:9 adresine ayarlanıyor. Bu durumda tarayıcı internete çıkabilirken
# Python'un EPİAŞ isteği reddediliyor. Yalnızca bu özel bozuk proxy'yi atlıyoruz;
# Render veya gerçek bir kurumsal proxy ayarı kullanılmaya devam eder.
DIRECT_OPENER = build_opener(ProxyHandler({}))


def epias_open(request: Request, timeout: int):
    proxy_values = [
        os.getenv(name, "")
        for name in (
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
        )
    ]
    for proxy in proxy_values:
        try:
            parsed_proxy = urlparse(proxy)
            is_dead_local_proxy = (
                parsed_proxy.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed_proxy.port == 9
            )
        except ValueError:
            is_dead_local_proxy = False
        if is_dead_local_proxy:
            return DIRECT_OPENER.open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


class EpiasError(Exception):
    """Kullanıcıya gösterilebilecek EPİAŞ bağlantı hatası."""


class EpiasAuthenticationError(EpiasError):
    """EPİAŞ kullanıcı adı veya şifresi doğrulanamadı."""


class EpiasSessionError(EpiasError):
    """EPİAŞ oturumu artık kullanılamıyor."""


def fetch_tgt(username: str, password: str) -> str:
    """Kimlik bilgilerini EPİAŞ'a iletir ve geçici TGT döndürür."""
    body = urlencode({"username": username, "password": password}).encode("utf-8")

    request = Request(
        LOGIN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/plain",
            "User-Agent": "Baha-Baraj/1.0",
        },
        method="POST",
    )
    try:
        with epias_open(request, timeout=20) as response:
            tgt = response.read().decode("utf-8").strip()
    except HTTPError as exc:
        if exc.code in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise EpiasAuthenticationError(
                "Kullanıcı adı veya şifre doğrulanamadı."
            ) from exc
        raise EpiasError(f"EPİAŞ oturumu açılamadı (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError) as exc:
        raise EpiasError("EPİAŞ'a şu anda erişilemiyor. Lütfen tekrar deneyin.") from exc

    if not tgt.startswith("TGT-"):
        raise EpiasAuthenticationError(
            "Kullanıcı adı veya şifre doğrulanamadı."
        )

    return tgt


def create_session(tgt: str) -> str:
    """TGT'yi sunucu belleğinde rastgele bir oturum kimliğiyle saklar."""
    now = time.time()
    token = secrets.token_urlsafe(32)
    with STATE_LOCK:
        expired_tokens = [
            key
            for key, session in SESSIONS.items()
            if now >= float(session["expires_at"])
            or now - float(session["last_seen"]) >= SESSION_IDLE_SECONDS
        ]
        for expired_token in expired_tokens:
            SESSIONS.pop(expired_token, None)
        SESSIONS[token] = {
            "tgt": tgt,
            "created_at": now,
            "last_seen": now,
            "expires_at": now + SESSION_MAX_SECONDS,
        }
    return token


def get_session_tgt(token: str | None) -> str | None:
    """Geçerli oturumun TGT değerini döndürür ve son kullanım zamanını yeniler."""
    if not token:
        return None

    now = time.time()
    with STATE_LOCK:
        session = SESSIONS.get(token)
        if not session:
            return None
        if (
            now >= float(session["expires_at"])
            or now - float(session["last_seen"]) >= SESSION_IDLE_SECONDS
        ):
            SESSIONS.pop(token, None)
            return None
        session["last_seen"] = now
        return str(session["tgt"])


def delete_session(token: str | None) -> None:
    if not token:
        return
    with STATE_LOCK:
        SESSIONS.pop(token, None)


def login_is_blocked(client_id: str) -> bool:
    """Aynı istemciden kısa sürede yapılan başarısız girişleri sınırlar."""
    cutoff = time.time() - LOGIN_FAILURE_WINDOW_SECONDS
    with STATE_LOCK:
        recent = [attempt for attempt in LOGIN_FAILURES.get(client_id, []) if attempt >= cutoff]
        if recent:
            LOGIN_FAILURES[client_id] = recent
        else:
            LOGIN_FAILURES.pop(client_id, None)
        return len(recent) >= MAX_LOGIN_FAILURES


def record_login_failure(client_id: str) -> None:
    cutoff = time.time() - LOGIN_FAILURE_WINDOW_SECONDS
    with STATE_LOCK:
        recent = [attempt for attempt in LOGIN_FAILURES.get(client_id, []) if attempt >= cutoff]
        recent.append(time.time())
        LOGIN_FAILURES[client_id] = recent


def clear_login_failures(client_id: str) -> None:
    with STATE_LOCK:
        LOGIN_FAILURES.pop(client_id, None)


def get_active_fullness(selected_date: str, tgt: str) -> dict:
    """Aktif doluluk listesini EPİAŞ'tan alır ve seçilen tarihle doğrular."""
    try:
        selected = date.fromisoformat(selected_date)
    except ValueError as exc:
        raise EpiasError("Geçerli bir tarih seçin.") from exc

    # EPİAŞ sayfalama şeması `number` alanını kullanır; ilk sayfa 1'dir.
    payload = json.dumps({"page": {"number": 1, "size": 500}}).encode("utf-8")
    request = Request(
        ACTIVE_FULLNESS_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "TGT": tgt,
            "User-Agent": "Baha-Baraj/1.0",
        },
        method="POST",
    )
    try:
        with epias_open(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            # EPİAŞ hata kodunu (ör. AUTH009) güvenli biçimde iletiyoruz.
            # TGT'nin kendisini hiçbir zaman istemciye göndermiyoruz.
            detail = ""
            try:
                error_body = exc.read(4096).decode("utf-8", "replace")
                error_payload = json.loads(error_body)
                errors = error_payload.get("errors") or []
                if errors and isinstance(errors[0], dict):
                    error_code = str(errors[0].get("errorCode") or "").strip()
                    error_message = str(errors[0].get("errorMessage") or "").strip()
                    detail = ": ".join(part for part in (error_code, error_message) if part)
            except (json.JSONDecodeError, UnicodeError, AttributeError, TypeError):
                pass
            message = "EPİAŞ oturumu doğrulanamadı"
            if detail:
                message += f" ({detail})"
            raise EpiasSessionError(
                message + ". Lütfen tekrar giriş yapın."
            ) from exc
        raise EpiasError(f"EPİAŞ verisi alınamadı (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EpiasError("EPİAŞ verisi okunamadı. Lütfen tekrar deneyin.") from exc

    items = data.get("items", data.get("body", {}).get("items", []))
    normalized = [
        {
            "dam": item.get("dam") or item.get("damName") or "—",
            "basin": item.get("basin") or item.get("basinName") or "—",
            "activeFullnessAmount": item.get("activeFullnessAmount"),
            "date": item.get("date", ""),
        }
        for item in items
    ]
    available_dates = sorted({row["date"][:10] for row in normalized if row["date"]})
    if available_dates and selected.isoformat() not in available_dates:
        raise EpiasError(
            "EPİAŞ baraj verisi geçmişe dönük sunulmaz. Kullanılabilir veri tarihi: "
            + ", ".join(available_dates)
        )

    return {"items": normalized, "availableDates": available_dates}


class DashboardHandler(BaseHTTPRequestHandler):
    def send_bytes(
        self,
        status: int,
        content: bytes,
        content_type: str,
        cache_control: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def send_json(
        self, status: int, payload: dict, headers: dict[str, str] | None = None
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def read_json(self) -> dict:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Geçersiz istek uzunluğu.") from exc

        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("Geçersiz istek içeriği.")

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Geçerli bir JSON içeriği gönderin.") from exc

        if not isinstance(payload, dict):
            raise ValueError("Geçerli bir JSON nesnesi gönderin.")
        return payload

    def session_token(self) -> str | None:
        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        try:
            cookies = SimpleCookie(raw_cookie)
        except CookieError:
            return None
        session_cookie = cookies.get(SESSION_COOKIE)
        return session_cookie.value if session_cookie else None

    def session_cookie_header(self, token: str, max_age: int) -> str:
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "
            f"SameSite=Strict; Max-Age={max_age}"
        )
        forwarded_protocol = self.headers.get("X-Forwarded-Proto", "")
        if forwarded_protocol.split(",", 1)[0].strip().lower() == "https":
            cookie += "; Secure"
        return cookie

    def client_id(self) -> str:
        forwarded_for = self.headers.get("X-Forwarded-For", "")
        return forwarded_for.split(",", 1)[0].strip() or self.client_address[0]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/manifest.webmanifest":
            content = json.dumps(WEB_MANIFEST, ensure_ascii=False).encode("utf-8")
            self.send_bytes(
                HTTPStatus.OK,
                content,
                "application/manifest+json; charset=utf-8",
                "no-cache",
            )
            return

        if parsed.path in ICON_ROUTES:
            self.send_bytes(
                HTTPStatus.OK,
                ICON_ROUTES[parsed.path],
                "image/png",
                "public, max-age=86400",
            )
            return

        if parsed.path == "/api/session":
            authenticated = get_session_tgt(self.session_token()) is not None
            self.send_json(HTTPStatus.OK, {"authenticated": authenticated})
            return

        if parsed.path == "/api/active-fullness":
            token = self.session_token()
            tgt = get_session_tgt(token)
            if not tgt:
                self.send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "Oturum sona erdi. Lütfen tekrar giriş yapın."},
                )
                return

            selected_date = parse_qs(parsed.query).get("date", [date.today().isoformat()])[0]
            try:
                self.send_json(
                    HTTPStatus.OK, get_active_fullness(selected_date, tgt)
                )
            except EpiasSessionError as exc:
                delete_session(token)
                self.send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": str(exc)},
                    {"Set-Cookie": self.session_cookie_header("", 0)},
                )
            except EpiasError as exc:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if parsed.path in ("/", "/index.html"):
            content = INDEX_FILE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            client_id = self.client_id()
            if login_is_blocked(client_id):
                self.send_json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "Çok fazla başarısız deneme yapıldı. 10 dakika sonra tekrar deneyin."},
                    {"Retry-After": str(LOGIN_FAILURE_WINDOW_SECONDS)},
                )
                return

            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            username = payload.get("username")
            password = payload.get("password")
            if (
                not isinstance(username, str)
                or not isinstance(password, str)
                or not username.strip()
                or not password
                or len(username) > 320
                or len(password) > 1024
            ):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Kullanıcı adı ve şifreyi eksiksiz girin."},
                )
                return

            try:
                tgt = fetch_tgt(username.strip(), password)
            except EpiasAuthenticationError as exc:
                record_login_failure(client_id)
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            except EpiasError as exc:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return

            clear_login_failures(client_id)
            token = create_session(tgt)
            self.send_json(
                HTTPStatus.OK,
                {"authenticated": True},
                {
                    "Set-Cookie": self.session_cookie_header(
                        token, SESSION_MAX_SECONDS
                    )
                },
            )
            return

        if parsed.path == "/api/logout":
            delete_session(self.session_token())
            self.send_json(
                HTTPStatus.OK,
                {"authenticated": False},
                {"Set-Cookie": self.session_cookie_header("", 0)},
            )
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Sayfa bulunamadı."})

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    browser_host = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
    print(f"Panel hazır: http://{browser_host}:{PORT}")
    if browser_host != HOST:
        print(f"Dinleme adresi: {HOST}:{PORT} (0.0.0.0 tarayıcı adresi değildir)")
    print("Durdurmak için Ctrl+C tuşlarına basın.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu durduruldu.")
    finally:
        server.server_close()
