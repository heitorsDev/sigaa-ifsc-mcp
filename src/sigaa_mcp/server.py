"""Servidor MCP do SIGAA do IFSC."""

from __future__ import annotations

import asyncio
import functools
import json
import mimetypes
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from playwright.async_api import Error as PlaywrightError

from . import courses, documents, pages, parsers
from .browser_login import capture_jsessionid
from .client import BASE_URL, HOST, PORTAL_PATH, SessionExpired, SigaaClient, SigaaError

KEEPALIVE_MIN = float(os.environ.get("SIGAA_KEEPALIVE_MIN", "10"))
MAX_CHARS = int(os.environ.get("SIGAA_MAX_CHARS", "20000"))
AUTO_LOGIN = os.environ.get("SIGAA_AUTO_LOGIN", "1") != "0"
DOWNLOAD_DIR = Path(os.environ.get("SIGAA_DOWNLOAD_DIR", Path.home() / "Downloads" / "SIGAA")).expanduser()
MAX_UPLOAD_MB = float(os.environ.get("SIGAA_MAX_UPLOAD_MB", "50"))

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
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "tarefas": parsers.strip_forms(parsers.parse_homeworks(page))})


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


# ---- arquivos ---------------------------------------------------------------


def _file_result(name: str, data: bytes, ctype: str, folder: Path) -> str:
    """Salva o arquivo e, se for PDF, devolve também o texto dele."""
    path = documents.save(name, data, ctype, folder)
    info = _json({"arquivo": str(path), "tamanho_bytes": len(data), "tipo": ctype})
    if documents.is_pdf(data, ctype):
        info += "

" + documents.pdf_text(data, MAX_CHARS)
    return info


@tool
async def arquivos(turma: str) -> str:
    """Arquivos publicados na seção 'Arquivos' de uma turma (título e descrição)."""
    course, page = await courses.open_section(client, turma, "Arquivos")
    files = [{k: v for k, v in f.items() if k not in ("form", "key")} for f in parsers.parse_files(page)]
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "arquivos": files})


@tool
async def topicos(turma: str) -> str:
    """Tópicos de aula da turma (página principal): título, período, conteúdo e
    materiais de cada aula (arquivos, tarefas e links)."""
    course, page = await courses.open_course(client, turma)
    topics = parsers.parse_topics(page)
    for t in topics:
        t["materiais"] = [{k: v for k, v in m.items() if k not in ("form", "key")} for m in t["materiais"]]
    return _json({"turma": f'{course["codigo"]} - {course["nome"]}', "topicos": topics})


@tool
async def baixar_arquivo(turma: str, arquivo: str, pasta: str = "") -> str:
    """Baixa um arquivo da seção 'Arquivos' de uma turma (pelo título, trecho
    dele ou id) e salva no computador. Padrão: ~/Downloads/SIGAA/<código da turma>.
    Retorna o caminho do arquivo salvo."""
    q = pages.norm(arquivo)

    def match(files: list[dict]) -> list[dict]:
        return [f for f in files if f["id"] == arquivo or pages.norm(f["titulo"]) == q] or [
            f for f in files if q in pages.norm(f["titulo"])
        ]

    course, page = await courses.open_section(client, turma, "Arquivos")
    files = parsers.parse_files(page)
    found = match(files)
    if not found:
        # Materiais anexados aos tópicos de aula nem sempre aparecem em Arquivos.
        _, main = await courses.open_course(client, turma)
        topic_files = [
            {**m, "titulo": m["nome"]}
            for t in parsers.parse_topics(main)
            for m in t["materiais"]
            if m["tipo"] == "arquivo"
        ]
        found = match(topic_files)
        files += topic_files
    if not found:
        names = "; ".join(f["titulo"] for f in files) or "nenhum"
        raise SigaaError(f'Arquivo "{arquivo}" não encontrado. Arquivos: {names}.')
    f = found[0]
    # O POST JSF é o que o navegador faz; o GET em /sigaa/verFoto (usado pelo
    # sigaa-api) fica como alternativa caso o estado JSF esteja velho.
    attempts = []
    if f["form"]:
        attempts.append(lambda: client.download(form=f["form"]))
    if f["id"] and f["key"]:
        attempts.append(lambda: client.download(f"/sigaa/verFoto?idArquivo={f['id']}&key={f['key']}"))
    if not attempts:
        raise SigaaError("Este arquivo não tem link de download.")
    for i, attempt in enumerate(attempts):
        try:
            name, data, ctype = await attempt()
            break
        except SigaaError:
            if i == len(attempts) - 1:
                raise
    if name == "arquivo":
        name = f["titulo"]
    folder = Path(pasta).expanduser() if pasta else DOWNLOAD_DIR / course["codigo"]
    return _file_result(name, data, ctype, folder)


# ---- navegação genérica -----------------------------------------------------


def _current():
    if client.current is None:
        raise SigaaError("Nenhuma página aberta. Use abrir_pagina, secao_turma ou outra ferramenta antes.")
    return client.current


@tool
async def pagina_atual() -> str:
    """Texto da última página do SIGAA aberta por qualquer ferramenta."""
    page = _current()
    return f"URL: {page.url}\n\n" + parsers.page_text(page, MAX_CHARS)


@tool
async def links_pagina(filtro: str = "") -> str:
    """Links da página atual, com índice para usar em `clicar`. Links JSF (que
    o SIGAA usa para quase tudo) também aparecem. `filtro` filtra pelo texto."""
    q = pages.norm(filtro)
    rows = [
        f'[{link["indice"]}] {link["texto"]}'
        for link in pages.links(_current())
        if not q or q in pages.norm(link["texto"])
    ]
    return "\n".join(rows) or "Nenhum link encontrado."


@tool
async def clicar(texto: str = "", indice: int = -1) -> str:
    """Segue um link da página atual, pelo texto (trecho, sem acentos) ou pelo
    índice de `links_pagina`. Se o link for um arquivo, ele é salvo em
    ~/Downloads/SIGAA e o caminho é retornado."""
    if indice < 0 and not texto:
        return "Informe `texto` ou `indice`."
    link = pages.find_link(_current(), texto, indice)
    result = await client.fetch(link["url"] or "", link["form"])
    if isinstance(result, tuple):
        name, data, ctype = result
        return _file_result(name, data, ctype, DOWNLOAD_DIR)
    return f'Clicado: "{link["texto"]}"\nURL: {result.url}\n\n' + parsers.page_text(result, MAX_CHARS)


@tool
async def formularios_pagina() -> str:
    """Formulários da página atual: id, campos (nome, rótulo, tipo, valor,
    opções), campos de arquivo e botões. Use com `enviar_formulario`."""
    return _json(pages.forms(_current()))


def _read_upload(path_str: str) -> tuple[str, bytes, str]:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise SigaaError(f"Arquivo não encontrado: {path}")
    size = path.stat().st_size
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise SigaaError(f"{path.name} tem {size / 1e6:.1f} MB; o limite configurado é {MAX_UPLOAD_MB:g} MB.")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, path.read_bytes(), ctype


async def _submit(page, form_id: str, campos: dict, arquivos: dict, botao: str, confirmar: bool) -> str:
    form, file_names, changed = pages.build_form(page, form_id, campos, botao or None)
    uploads: dict[str, tuple[str, bytes, str]] = {}
    for key, path in arquivos.items():
        if key in file_names:
            name = key
        elif len(file_names) == 1:
            name = file_names[0]
        else:
            raise SigaaError(f'Campo de arquivo "{key}" não existe. Campos: {", ".join(file_names) or "nenhum"}.')
        uploads[name] = _read_upload(path)

    if not confirmar:
        return _json(
            {
                "previa": True,
                "aviso": (
                    "NADA FOI ENVIADO. Mostre este resumo ao usuário e só chame de novo com "
                    "confirmar=true depois que ele aprovar explicitamente."
                ),
                "formulario": form_id,
                "pagina": page.url,
                "campos_alterados": changed,
                "arquivos": {k: f"{v[0]} ({len(v[1])} bytes)" for k, v in uploads.items()},
            }
        )

    multipart = bool(uploads) or any(f["id"] == form_id and f["multipart"] for f in pages.forms(page))
    if multipart:
        # Campos de arquivo sem arquivo vão vazios, como num navegador.
        for name in file_names:
            uploads.setdefault(name, ("", b"", "application/octet-stream"))
        result = await client.post_multipart(form, uploads)
    else:
        result = await client.post(form)
    return _json(pages.messages(result)) + "\n\n" + parsers.page_text(result, MAX_CHARS)


@tool
async def enviar_formulario(
    formulario: str,
    campos: dict[str, str] | None = None,
    arquivos: dict[str, str] | None = None,
    botao: str = "",
    confirmar: bool = False,
) -> str:
    """Preenche e envia um formulário da página atual (veja `formularios_pagina`).

    - `campos`: {nome ou rótulo do campo: valor}; checkbox aceita true/false.
    - `arquivos`: {nome do campo de arquivo: caminho local}.
    - `botao`: nome ou texto do botão (padrão: o primeiro).
    - Com `confirmar=false` (padrão) só mostra uma prévia e NÃO envia nada.
      Só use `confirmar=true` depois que o usuário aprovar o envio.
    Retorna as mensagens de sucesso/erro do SIGAA e a página resultante."""
    return await _submit(_current(), formulario, campos or {}, arquivos or {}, botao, confirmar)


@tool
async def enviar_tarefa(
    turma: str, tarefa: str, arquivo: str = "", comentario: str = "", confirmar: bool = False
) -> str:
    """Entrega uma tarefa da turma, com arquivo (caminho local) e/ou comentário.

    Com `confirmar=false` (padrão) abre o formulário de envio e mostra uma
    prévia, sem enviar nada. Só use `confirmar=true` depois que o usuário
    aprovar a entrega."""
    course, page = await courses.open_section(client, turma, "Tarefas")
    tasks = parsers.parse_homeworks(page)
    q = pages.norm(tarefa)
    found = [t for t in tasks if pages.norm(t["titulo"]) == q] or [t for t in tasks if q in pages.norm(t["titulo"])]
    if not found:
        names = "; ".join(t["titulo"] for t in tasks) or "nenhuma"
        raise SigaaError(f'Tarefa "{tarefa}" não encontrada. Tarefas: {names}.')
    task = found[0]
    if task["form"] is None:
        raise SigaaError(f'A tarefa "{task["titulo"]}" não está aberta para envio.')
    send_page = await client.post(task["form"])
    errors = pages.messages(send_page)["erros"]
    if errors:
        # Ex.: "Esta tarefa só estará disponível no período de ..."
        raise SigaaError("SIGAA: " + " ".join(errors))

    candidates = [
        f for f in pages.forms(send_page) if f["arquivos"] or any(c["tipo"] == "textarea" for c in f["campos"])
    ]
    if not candidates:
        raise SigaaError("Não achei o formulário de envio na página da tarefa. Use formularios_pagina para inspecionar.")
    target = candidates[0]
    campos = {}
    if comentario:
        area = next((c for c in target["campos"] if c["tipo"] == "textarea"), None)
        if area is None:
            raise SigaaError("O formulário desta tarefa não tem campo de comentário.")
        campos[area["nome"]] = comentario
    uploads = {}
    if arquivo:
        if not target["arquivos"]:
            raise SigaaError("O formulário desta tarefa não aceita arquivo.")
        uploads[target["arquivos"][0]["nome"]] = arquivo
    botao = next((b["texto"] for b in target["botoes"] if "enviar" in pages.norm(b["texto"])), "")
    header = f'Turma: {course["codigo"]} - {course["nome"]}\nTarefa: {task["titulo"]} ({task["periodo"]})\n'
    return header + await _submit(send_page, target["id"], campos, uploads, botao, confirmar)


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
