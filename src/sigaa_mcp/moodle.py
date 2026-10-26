"""Cliente da API de web services do Moodle (a mesma do app Moodle Mobile).

O Moodle do IFSC tem o serviço mobile habilitado: com usuário e senha, o
endpoint /login/token.php devolve um token, e todas as consultas usam
/webservice/rest/server.php. Só o token é salvo; a senha não.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from . import __version__
from .client import STATE_DIR

MOODLE_URL = os.environ.get("MOODLE_URL", "https://moodle.ifsc.edu.br").rstrip("/")
TOKEN_FILE = STATE_DIR / "moodle.json"
SERVICE = "moodle_mobile_app"


class MoodleAuthError(Exception):
    """Sem token ou token inválido/expirado."""


class MoodleError(Exception):
    """Erro devolvido pela API do Moodle."""


def _flatten(params: dict, prefix: str = "") -> dict[str, str]:
    """{'courseids': [1, 2]} -> {'courseids[0]': '1', 'courseids[1]': '2'},
    o formato que a API REST do Moodle espera."""
    out: dict[str, str] = {}
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            out.update(_flatten(dict(enumerate(value)), name))
        elif isinstance(value, bool):
            out[name] = "1" if value else "0"
        elif value is not None:
            out[name] = str(value)
    return out


class MoodleClient:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self.token: str | None = os.environ.get("MOODLE_TOKEN") or None
        self._site: dict | None = None
        if not self.token:
            try:
                self.token = json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("token")
            except (OSError, ValueError):
                self.token = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=MOODLE_URL,
                timeout=60,
                follow_redirects=True,
                headers={"User-Agent": f"sigaa-ifsc-mcp/{__version__}"},
            )
        return self._http

    def save_token(self, token: str) -> None:
        self.token = token.strip()
        self._site = None
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({"token": self.token, "saved_at": time.time()}), encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

    async def login(self, username: str, password: str) -> dict:
        """Troca usuário e senha por um token (login/token.php) e o salva."""
        resp = await self._client().post(
            "/login/token.php", data={"username": username, "password": password, "service": SERVICE}
        )
        data = resp.json()
        if "token" not in data:
            raise MoodleAuthError(data.get("error") or "Login no Moodle falhou.")
        self.save_token(data["token"])
        return await self.site_info()

    async def call(self, function: str, **params) -> object:
        if not self.token:
            raise MoodleAuthError("Nenhum token do Moodle configurado.")
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json", **_flatten(params)}
        resp = await self._client().post("/webservice/rest/server.php", data=data)
        try:
            result = resp.json()
        except ValueError as e:
            raise MoodleError(f"Resposta inesperada do Moodle ({resp.status_code}).") from e
        if isinstance(result, dict) and "exception" in result:
            code = result.get("errorcode", "")
            if code == "invalidtoken":
                raise MoodleAuthError("O token do Moodle é inválido ou expirou.")
            raise MoodleError(f"{result.get('message') or code} ({function})")
        return result

    async def site_info(self) -> dict:
        if self._site is None:
            self._site = await self.call("core_webservice_get_site_info")
        return self._site

    async def download(self, fileurl: str) -> tuple[bytes, str]:
        """Baixa um arquivo de pluginfile.php usando o token."""
        if not self.token:
            raise MoodleAuthError("Nenhum token do Moodle configurado.")
        sep = "&" if "?" in fileurl else "?"
        resp = await self._client().get(f"{fileurl}{sep}token={self.token}")
        ctype = resp.headers.get("content-type", "")
        if resp.status_code >= 400:
            raise MoodleError(f"Moodle respondeu {resp.status_code} ao baixar o arquivo.")
        if "json" in ctype:
            err = resp.json()
            if isinstance(err, dict) and err.get("errorcode") == "invalidtoken":
                raise MoodleAuthError("O token do Moodle é inválido ou expirou.")
            raise MoodleError(str(err.get("error") if isinstance(err, dict) else err))
        return resp.content, ctype

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()
