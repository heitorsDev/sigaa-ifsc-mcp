"""Cliente HTTP do SIGAA do IFSC baseado num cookie JSESSIONID.

O login nunca é feito aqui: o formulário de login do SIGAA tem CAPTCHA
(Cloudflare Turnstile). O usuário loga no navegador e entrega o cookie da
sessão, que este cliente usa e mantém vivo.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup

from . import __version__

HOST = "sig.ifsc.edu.br"
BASE_URL = f"https://{HOST}"
PORTAL_PATH = "/sigaa/portais/discente/discente.jsf"
TURMAS_PATH = "/sigaa/portais/discente/turmas.jsf"

STATE_DIR = Path(os.environ.get("SIGAA_STATE_DIR", Path.home() / ".sigaa-mcp"))
SESSION_FILE = STATE_DIR / "session.json"

_LOGGED_OUT_MARKERS = ("expirada.jsp", "verTelaLogin", "logar.do")


class SessionExpired(Exception):
    """Não há sessão válida: o usuário precisa logar e fornecer um novo cookie."""


class SigaaError(Exception):
    """A página do SIGAA não tinha o formato esperado."""


@dataclass
class Form:
    action: str
    values: dict[str, str]


@dataclass
class Page:
    url: str
    status: int
    html: str
    _soup: BeautifulSoup | None = None

    @property
    def soup(self) -> BeautifulSoup:
        if self._soup is None:
            self._soup = BeautifulSoup(self.html, "html.parser")
        return self._soup

    def parse_jsfcljs(self, onclick: str) -> Form:
        """Converte o `onclick` de um commandLink JSF (função jsfcljs) no
        formulário POST equivalente: campos do <form> + parâmetros do link."""
        m = re.search(r"getElementById\('([^']+)'\)", onclick)
        if not m:
            raise SigaaError("Link JSF sem formulário.")
        form_el = self.soup.find(id=m.group(1))
        if form_el is None or not form_el.get("action"):
            raise SigaaError(f"Formulário {m.group(1)} não encontrado na página.")
        values: dict[str, str] = {}
        for inp in form_el.find_all("input"):
            if inp.get("type") == "submit" or not inp.get("name"):
                continue
            values[inp["name"]] = inp.get("value", "")
        params = re.search(r",\s*\{(.*?)\}\s*,", onclick, re.S)
        if params:
            for k, v in re.findall(r"'([^']*)'\s*:\s*'([^']*)'", params.group(1)):
                values[k] = v
        return Form(urljoin(self.url, form_el["action"]), values)


def _normalize_cookie(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    m = re.search(r"JSESSIONID=([^;\s]+)", value)
    return m.group(1) if m else value


def _filename(resp: httpx.Response) -> str | None:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(
        r'filename="?([^";]+)', cd, re.I
    )
    if not m:
        return None
    name = unquote(m.group(1).strip().strip("'\""))
    # Muitos servidores mandam o nome em ISO-8859-1 dentro do cabeçalho.
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return Path(name).name or None


def _decode(resp: httpx.Response) -> str:
    charset = resp.charset_encoding
    if not charset:
        m = re.search(rb"charset=[\"']?([\w-]+)", resp.content[:2048], re.I)
        charset = m.group(1).decode() if m else "iso-8859-1"
    return resp.content.decode(charset, errors="replace")


class SigaaClient:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self.jsessionid: str | None = None
        self.last_ok: float | None = None
        self.current: Page | None = None
        self._load()

    # ---- sessão ----------------------------------------------------------

    def _load(self) -> None:
        env = os.environ.get("SIGAA_JSESSIONID")
        if env:
            self.jsessionid = _normalize_cookie(env)
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            self.jsessionid = data.get("jsessionid")
        except (OSError, ValueError):
            self.jsessionid = None

    def _save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(
            json.dumps({"jsessionid": self.jsessionid, "saved_at": time.time()}),
            encoding="utf-8",
        )
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                follow_redirects=True,
                timeout=30,
                headers={
                    "User-Agent": f"sigaa-ifsc-mcp/{__version__}",
                    "Accept-Language": "pt-BR,pt;q=0.9",
                },
            )
        self._http.cookies.clear()
        if self.jsessionid:
            self._http.cookies.set("JSESSIONID", self.jsessionid, domain=HOST, path="/sigaa")
        return self._http

    async def set_cookie(self, value: str) -> Page:
        """Troca o cookie da sessão, valida no portal e salva em disco."""
        previous, self.jsessionid = self.jsessionid, _normalize_cookie(value)
        try:
            page = await self.get(PORTAL_PATH)
        except Exception:
            self.jsessionid = previous
            raise
        self._save()
        return page

    def clear(self) -> None:
        self.jsessionid = None
        self.last_ok = None
        try:
            SESSION_FILE.unlink()
        except OSError:
            pass

    # ---- HTTP ------------------------------------------------------------

    def _check(self, resp: httpx.Response, track: bool = True) -> Page:
        urls = [str(r.headers.get("location", "")) for r in resp.history] + [str(resp.url)]
        html = _decode(resp)
        if any(m in u for u in urls for m in _LOGGED_OUT_MARKERS) or 'name="user.senha"' in html:
            self.last_ok = None
            self.current = None
            raise SessionExpired("A sessão do SIGAA expirou ou é inválida.")
        if resp.status_code >= 400:
            raise SigaaError(f"SIGAA respondeu {resp.status_code} em {resp.url}.")
        if "cf-turnstile" in html or "challenges.cloudflare.com/turnstile" in html:
            # A área pública (/sigaa/public) fica atrás do CAPTCHA, mesmo logado.
            raise SigaaError(
                "Esta página do SIGAA exige CAPTCHA (Cloudflare Turnstile); o servidor MCP não a acessa. "
                "Abra-a no navegador."
            )
        if "Comportamento Inesperado" in html:
            raise SigaaError("SIGAA: 'Comportamento Inesperado!' (estado JSF inválido).")
        self.last_ok = time.time()
        page = Page(str(resp.url), resp.status_code, html)
        if track:
            self.current = page
        return page

    def _require_session(self) -> httpx.AsyncClient:
        if not self.jsessionid:
            raise SessionExpired("Nenhum cookie de sessão configurado.")
        return self._client()

    async def get(self, path: str, track: bool = True) -> Page:
        return self._check(await self._require_session().get(path), track)

    async def post(self, form: Form) -> Page:
        return self._check(await self._require_session().post(form.action, data=form.values))

    async def post_multipart(
        self, form: Form, files: dict[str, tuple[str, bytes, str]]
    ) -> Page:
        """POST multipart/form-data: `files` mapeia o nome do campo para
        (nome do arquivo, conteúdo, content-type)."""
        http = self._require_session()
        return self._check(await http.post(form.action, data=form.values, files=files))

    async def fetch(
        self, url: str = "", form: Form | None = None
    ) -> Page | tuple[str, bytes, str]:
        """GET (ou POST com `form`) que pode resultar numa página ou num arquivo.
        Páginas viram `Page`; arquivos, (nome, conteúdo, content-type)."""
        http = self._require_session()
        resp = await (http.post(form.action, data=form.values) if form else http.get(url))
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype or (not ctype and resp.content.lstrip()[:1] == b"<"):
            return self._check(resp)
        if resp.status_code >= 400:
            raise SigaaError(f"SIGAA respondeu {resp.status_code} ao baixar o arquivo.")
        self.last_ok = time.time()
        return _filename(resp) or "arquivo", resp.content, ctype

    async def download(self, url: str = "", form: Form | None = None) -> tuple[str, bytes, str]:
        """Baixa um arquivo por GET (ou POST com `form`).
        Retorna (nome do arquivo, conteúdo, content-type)."""
        result = await self.fetch(url, form)
        if isinstance(result, Page):
            raise SigaaError("O SIGAA devolveu uma página em vez do arquivo.")
        return result

    async def keepalive(self) -> bool:
        """Acessa o portal para a sessão não expirar por inatividade."""
        if not self.jsessionid:
            return False
        async with self.lock:
            try:
                await self.get(PORTAL_PATH, track=False)
                return True
            except (SessionExpired, SigaaError, httpx.HTTPError):
                return False

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()
