"""Servidor MCP do SIGAA do IFSC."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from playwright.async_api import Error as PlaywrightError

from . import courses, parsers
from .browser_login import capture_jsessionid
from .client import BASE_URL, HOST, PORTAL_PATH, SessionExpired, SigaaClient, SigaaError

KEEPALIVE_MIN = float(os.environ.get("SIGAA_KEEPALIVE_MIN", "10"))
MAX_CHARS = int(os.environ.get("SIGAA_MAX_CHARS", "20000"))
AUTO_LOGIN = os.environ.get("SIGAA_AUTO_LOGIN", "1") != "0"

client = SigaaClient()

HOW_TO_LOGIN = (
    "Para reconectar, chame `login_navegador` (abre uma janela para o usuário entrar "
    "no SIGAA) ou `definir_cookie` com o JSESSIONID de um login feito no navegador."
)


async def _keepalive_loop() -> None:
    while True:
        await asyncio.sleep(KEEPALIVE_MIN * 60)
        await client.keepalive()


@asynccontextmanager
async def lifespan(_server):
    task = asyncio.create_task(_keepalive_loop()) if KEEPALIVE_MIN > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()
        await client.aclose()


mcp = FastMCP("sigaa-ifsc", lifespan=lifespan)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


async def _login_via_browser() -> str:
    """Abre a janela de login, espera o usuário entrar e salva o cookie.
    Retorna o nome do aluno logado."""
    try:
        cookie = await capture_jsessionid()
    except PlaywrightError as e:
        raise SessionExpired(
            f"Não foi possível abrir o navegador ({e}). Fora do Windows, rode "
            "`uvx --from playwright playwright install chromium`."
        ) from e
    if not cookie:
        raise SessionExpired("Login não concluído (janela fechada ou tempo esgotado).")
    page = await client.set_cookie(cookie)
    return parsers.parse_profile(page).get("nome") or "?"


def tool(fn=None, *, auto_login: bool = True):
    """Registra a ferramenta, serializa o acesso ao SIGAA e traduz erros.

    Com `auto_login`, uma sessão expirada abre a janela de login e, depois que
    o usuário entra, a ferramenta é executada de novo."""

    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            async with client.lock:
                try:
                    try:
                        return await fn(*args, **kwargs)
                    except SessionExpired:
                        if not (auto_login and AUTO_LOGIN):
                            raise
                        await _login_via_browser()
                        return await fn(*args, **kwargs)
                except SessionExpired as e:
                    return f"{e} {HOW_TO_LOGIN}"
                except SigaaError as e:
                    return str(e)
                except httpx.HTTPError as e:
                    return f"Erro de rede ao acessar o SIGAA: {e!r}"

        return mcp.tool()(wrapper)

    return decorate(fn) if fn else decorate


# ---- sessão ---------------------------------------------------------------


@tool(auto_login=False)
async def definir_cookie(cookie: str) -> str:
    """Configura a sessão do SIGAA a partir do cookie JSESSIONID de um login feito
    pelo usuário no navegador. Aceita só o valor ou 'JSESSIONID=valor'."""
    page = await client.set_cookie(cookie)
    profile = parsers.parse_profile(page)
    return f"Sessão válida e salva. Logado como {profile.get('nome') or '?'}."


@tool(auto_login=False)
async def login_navegador() -> str:
    """Abre uma janela do navegador na tela de login do SIGAA. O usuário faz o
    login (com o CAPTCHA) e o cookie da sessão é capturado automaticamente."""
    return f"Sessão capturada e salva. Logado como {await _login_via_browser()}."


@tool(auto_login=False)
async def status() -> str:
    """Verifica se a sessão do SIGAA é válida e mostra o aluno logado."""
    profile = parsers.parse_profile(await client.get(PORTAL_PATH))
    keepalive = f"a cada {KEEPALIVE_MIN:g} min" if KEEPALIVE_MIN > 0 else "desativado"
    return f"Sessão válida: {profile.get('nome') or '?'} ({profile.get('periodo_atual') or '?'}). Keepalive {keepalive}."


@tool(auto_login=False)
async def esquecer_sessao() -> str:
    """Apaga o cookie de sessão salvo neste computador."""
    client.clear()
    return "Cookie de sessão apagado."


# ---- portal ---------------------------------------------------------------


@tool
async def perfil() -> str:
    """Dados do aluno no portal do discente: nome, matrícula, curso, status,
    período atual etc."""
    return _json(parsers.parse_profile(await client.get(PORTAL_PATH)))


@tool
async def atividades() -> str:
    """Atividades listadas no portal (tarefas, questionários e avaliações
    próximas), com data, disciplina e se já foram concluídas."""
    return _json(parsers.parse_activities(await client.get(PORTAL_PATH)))


@tool
async def turmas(todos_periodos: bool = False) -> str:
    """Turmas do aluno (código, nome, horário, período). Por padrão só o período
    mais recente; `todos_periodos=True` lista todos."""
    return _json(parsers.strip_forms(await courses.list_courses(client, todos_periodos)))


# ---- turma virtual --------------------------------------------------------


@tool
async def notas(turma: str) -> str:
    """Notas de uma turma do semestre (código ou trecho do nome), por unidade e
    avaliação, com a situação quando disponível."""
    course, page = await courses.open_section(client, turma, "Ver Notas")
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "notas": parsers.parse_grades(page)})


@tool
async def frequencia(turma: str) -> str:
    """Frequência de uma turma: faltas por aula, total e máximo permitido."""
    course, page = await courses.open_section(client, turma, "Frequência")
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', **parsers.parse_absences(page)})


@tool
async def tarefas(turma: str) -> str:
    """Tarefas de uma turma: título, período de entrega, se vale nota, se já
    foi enviada e descrição."""
    course, page = await courses.open_section(client, turma, "Tarefas")
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "tarefas": parsers.parse_homeworks(page)})


@tool
async def avaliacoes(turma: str) -> str:
    """Calendário de avaliações (provas) de uma turma."""
    course, page = await courses.open_course(client, turma)
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "avaliacoes": parsers.parse_exam_calendar(page)})


@tool
async def noticias(turma: str, com_conteudo: bool = True, limite: int = 5) -> str:
    """Notícias publicadas na turma. Com `com_conteudo`, abre as `limite` mais
    recentes para trazer o texto completo."""
    course, page = await courses.open_section(client, turma, "Notícias")
    items = parsers.parse_news_list(page)
    if com_conteudo:
        for i, item in enumerate(items[: max(limite, 0)]):
            if item["form"] is None:
                continue
            try:
                detail = await client.post(item["form"])
            except SigaaError:
                # O formulário JSF pode ter ficado velho; reabre a lista e tenta de novo.
                _, page = await courses.open_section(client, turma, "Notícias")
                fresh = parsers.parse_news_list(page)
                if i >= len(fresh) or fresh[i]["form"] is None:
                    continue
                detail = await client.post(fresh[i]["form"])
            try:
                item.update(parsers.parse_news_detail(detail))
            except SigaaError:
                pass
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "noticias": parsers.strip_forms(items)})


@tool
async def secao_turma(turma: str, secao: str = "Principal") -> str:
    """Abre qualquer seção do menu da turma virtual e devolve o texto da página.
    Exemplos: 'Principal', 'Arquivos', 'Plano de Ensino', 'Participantes',
    'Questionários', 'Fóruns', 'Conteúdo/Página web'. Uma seção inexistente
    retorna a lista das seções disponíveis."""
    course, page = await courses.open_section(client, turma, secao)
    menu = ", ".join(parsers.course_menu(page))
    return f'Turma: {course["codigo"]} - {course["nome"]}\nSeções: {menu}\n\n' + parsers.page_text(page, MAX_CHARS)


@tool
async def abrir_pagina(caminho: str = PORTAL_PATH) -> str:
    """Abre uma página do SIGAA por GET (ex.: '/sigaa/portais/discente/discente.jsf')
    e devolve o texto. Só funciona para páginas com URL fixa; a maior parte da
    navegação do SIGAA é por formulários JSF."""
    url = urljoin(BASE_URL + "/sigaa/", caminho)
    if urlparse(url).hostname != HOST:
        return f"Só é permitido acessar {HOST}."
    page = await client.get(url)
    return f"URL: {page.url}\n\n" + parsers.page_text(page, MAX_CHARS)


# ---- CLI ------------------------------------------------------------------


async def _cli_cookie(value: str) -> None:
    try:
        page = await client.set_cookie(value)
        print(f"Sessão salva. Logado como {parsers.parse_profile(page).get('nome') or '?'}.")
    finally:
        await client.aclose()


async def _cli_login() -> None:
    cookie = await capture_jsessionid()
    if not cookie:
        print("Login não concluído.")
        return
    await _cli_cookie(cookie)


async def _cli_keepalive() -> None:
    interval = KEEPALIVE_MIN if KEEPALIVE_MIN > 0 else 10
    if not client.jsessionid:
        print("Nenhum cookie salvo. Rode `sigaa-mcp cookie <valor>` primeiro.")
        return
    print(f"Mantendo a sessão viva a cada {interval:g} min. Ctrl+C para sair.")
    try:
        while True:
            ok = await client.keepalive()
            print("ok" if ok else "sessão expirada ou erro de rede", flush=True)
            await asyncio.sleep(interval * 60)
    finally:
        await client.aclose()


USAGE = """uso: sigaa-mcp [comando]

  (sem comando)     roda o servidor MCP (stdio)
  cookie <valor>    salva o JSESSIONID de um login feito no navegador
  login             abre o navegador para logar e captura o cookie
  keepalive         mantém a sessão salva ativa enquanto estiver rodando
  install-browser   baixa o Chromium usado pelo login por janela (fora do Windows)
"""


def main() -> None:
    args = sys.argv[1:]
    try:
        if not args:
            mcp.run()
        elif args[0] == "cookie" and len(args) == 2:
            asyncio.run(_cli_cookie(args[1]))
        elif args[0] == "login":
            asyncio.run(_cli_login())
        elif args[0] == "keepalive":
            asyncio.run(_cli_keepalive())
        elif args[0] == "install-browser":
            import subprocess

            sys.exit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]))
        else:
            print(USAGE)
            sys.exit(2)
    except (SessionExpired, SigaaError) as e:
        print(e)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
