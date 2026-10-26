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
# Cursos de antes de 2026 ficaram em outro site do IFSC.
MOODLE_LEGACY_URL = os.environ.get("MOODLE_LEGACY_URL", "https://moodle4.ifsc.edu.br").rstrip("/")
SITES = {"atual": MOODLE_URL, "antigo": MOODLE_LEGACY_URL}
TOKEN_FILE = STATE_DIR / "moodle.json"
SERVICE = "moodle_mobile_app"


def _load_tokens() -> dict[str, str]:
    """Tokens salvos por site. Aceita o formato antigo {"token": ...}."""
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if "sites" in data:
        return {url: v.get("token") for url, v in data["sites"].items() if v.get("token")}
    return {MOODLE_URL: data["token"]} if data.get("token") else {}


def _save_tokens(tokens: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    TOKEN_FILE.write_text(
        json.dumps({"sites": {url: {"token": t, "saved_at": now} for url, t in tokens.items()}}),
        encoding="utf-8",
    )
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


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
    def __init__(self, url: str = MOODLE_URL) -> None:
        self.url = url.rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._site: dict | None = None
        env = os.environ.get("MOODLE_TOKEN") if self.url == MOODLE_URL else None
        self.token: str | None = env or _load_tokens().get(self.url)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.url,
                timeout=120,
                follow_redirects=True,
                headers={"User-Agent": f"sigaa-ifsc-mcp/{__version__}"},
            )
        return self._http

    def save_token(self, token: str) -> None:
        self.token = token.strip()
        self._site = None
        tokens = _load_tokens()
        tokens[self.url] = self.token
        _save_tokens(tokens)

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

    async def upload_draft(self, files: list[tuple[str, bytes, str]]) -> int:
        """Envia arquivos para a área de rascunho do usuário (webservice/upload.php)
        e devolve o itemid, usado depois em mod_assign_save_submission etc."""
        if not self.token:
            raise MoodleAuthError("Nenhum token do Moodle configurado.")
        itemid = 0
        for name, data, ctype in files:
            resp = await self._client().post(
                "/webservice/upload.php",
                data={"token": self.token, "filearea": "draft", "itemid": str(itemid)},
                files={"file_1": (name, data, ctype)},
            )
            try:
                result = resp.json()
            except ValueError as e:
                raise MoodleError(f"Upload falhou ({resp.status_code}).") from e
            if isinstance(result, dict):
                if result.get("errorcode") == "invalidtoken":
                    raise MoodleAuthError("O token do Moodle é inválido ou expirou.")
                raise MoodleError(result.get("error") or str(result))
            itemid = int(result[0]["itemid"])
        return itemid

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()
