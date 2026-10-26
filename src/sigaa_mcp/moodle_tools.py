"""Ferramentas MCP do Moodle (API oficial do app mobile).

Todas aceitam `site`: "atual" (moodle.ifsc.edu.br) ou "antigo"
(moodle4.ifsc.edu.br, cursos de antes de 2026). Ferramentas que mudam dados
(entregar tarefa, postar em fórum, mandar mensagem) só agem com
`confirmar=true`; sem isso devolvem uma prévia.

Importado no fim de server.py, depois que `tool`, `_json` e `_file_result`
já existem.
"""

from __future__ import annotations

import mimetypes
import re
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from . import pages, parsers
from .moodle import SITES, MoodleClient, MoodleError
from .server import DOWNLOAD_DIR, MAX_CHARS, MAX_UPLOAD_MB, _file_result, _json, tool

_clients: dict[str, MoodleClient] = {}


def moodle(site: str = "atual") -> MoodleClient:
    key = site if site in SITES else "atual"
    if key not in _clients:
        _clients[key] = MoodleClient(SITES[key])
    return _clients[key]


async def close_all() -> None:
    for c in _clients.values():
        await c.aclose()


# ---- utilidades -------------------------------------------------------------


def html_text(html_str: str | None) -> str:
    if not html_str:
        return ""
    return "\n".join(parsers.lines(BeautifulSoup(html_str, "html.parser")))


def html_links(html_str: str | None) -> list[str]:
    """Links e vídeos embutidos (Drive, YouTube...) em descrições e rótulos."""
    if not html_str:
        return []
    soup = BeautifulSoup(html_str, "html.parser")
    urls = [a.get("href") for a in soup.find_all("a")] + [f.get("src") for f in soup.find_all(["iframe", "video", "source"])]
    return list(dict.fromkeys(u for u in urls if u and u.startswith("http")))


def ts(value: int | None) -> str:
    return datetime.fromtimestamp(value).strftime("%d/%m/%Y %H:%M") if value else ""


def _sigaa_ref(course: dict) -> dict:
    """Cursos criados a partir do SIGAA: idnumber 'COMPONENTE<id da turma>' e
    shortname '<código> - <nome>'."""
    m = re.match(r"COMPONENTE(\d+)$", course.get("idnumber") or "")
    code = re.match(r"([A-Z]{2,}\d+)\s+-\s+", course.get("shortname") or "")
    return {
        "id_turma_sigaa": m.group(1) if m else None,
        "codigo_componente": code.group(1) if code else None,
    }


async def user_id(site: str) -> int:
    return (await moodle(site).site_info())["userid"]


async def courses(site: str, classification: str = "all") -> list[dict]:
    data = await moodle(site).call(
        "core_course_get_enrolled_courses_by_timeline_classification", classification=classification, limit=0
    )
    return data.get("courses", [])


async def find_course(site: str, query: str) -> dict:
    """Curso pelo id, código do componente (do SIGAA), nome curto ou trecho."""
    all_courses = await courses(site)
    q = pages.norm(query)
    for match in (
        lambda c: str(c["id"]) == query or (c.get("idnumber") or "") == f"COMPONENTE{query}",
        lambda c: pages.norm(c.get("shortname", "")) == q or pages.norm(c.get("fullname", "")) == q,
        lambda c: q in pages.norm(f'{c.get("shortname", "")} {c.get("fullname", "")}'),
    ):
        found = [c for c in all_courses if match(c)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise MoodleError(f'"{query}" corresponde a vários cursos: ' + "; ".join(c["fullname"] for c in found))
    raise MoodleError(f'Curso "{query}" não encontrado no Moodle ({site}). Use moodle_cursos para listar.')


def pick(items: list[dict], query: str, *keys: str) -> dict:
    q = pages.norm(query)
    for match in (
        lambda i: str(i.get("id")) == query or any(pages.norm(str(i.get(k, ""))) == q for k in keys),
        lambda i: any(q in pages.norm(str(i.get(k, ""))) for k in keys),
    ):
        found = [i for i in items if match(i)]
        if found:
            return found[0]
    names = "; ".join(str(i.get(keys[0])) for i in items[:40]) or "nenhum"
    raise MoodleError(f'"{query}" não encontrado. Opções: {names}.')


def _cut(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n[... truncado em {MAX_CHARS} caracteres]"


def _preview(action: str, details: dict) -> str:
    return _json(
        {
            "previa": True,
            "aviso": (
                "NADA FOI ENVIADO. Mostre este resumo ao usuário e só chame de novo com "
                "confirmar=true depois que ele aprovar explicitamente."
            ),
            "acao": action,
            **details,
        }
    )


# ---- conta e cursos ---------------------------------------------------------


@tool(auto_login=False)
async def moodle_status(site: str = "atual") -> str:
    """Verifica o token do Moodle e mostra usuário, site e versão."""
    info = await moodle(site).site_info()
    return f"Moodle OK: {info.get('fullname')} em {info.get('siteurl')} (Moodle {info.get('release', '?')})."


@tool(auto_login=False)
async def moodle_cursos(classificacao: str = "todos", site: str = "atual") -> str:
    """Cursos inscritos no Moodle, com progresso, datas e a ligação com a turma
    do SIGAA (id_turma_sigaa, codigo_componente). `classificacao`: todos,
    em_andamento, passados ou futuros."""
    kind = {"todos": "all", "em_andamento": "inprogress", "passados": "past", "futuros": "future"}.get(
        classificacao, "all"
    )
    return _json(
        [
            {
                "id": c["id"],
                "nome_curto": c.get("shortname"),
                "nome": c.get("fullname"),
                "categoria": c.get("coursecategory"),
                "progresso": c.get("progress") if c.get("hasprogress") else None,
                "inicio": ts(c.get("startdate")),
                "fim": ts(c.get("enddate")),
                **_sigaa_ref(c),
            }
            for c in await courses(site, kind)
        ]
    )


@tool(auto_login=False)
async def moodle_buscar_cursos(texto: str, site: str = "atual") -> str:
    """Busca cursos do Moodle pelo nome (todos os câmpus, não só os inscritos)."""
    data = await moodle(site).call(
        "core_course_search_courses", criterianame="search", criteriavalue=texto, perpage=50
    )
    return _json(
        [
            {"id": c["id"], "nome": c.get("fullname"), "nome_curto": c.get("shortname"), "categoria": c.get("categoryname")}
            for c in data.get("courses", [])
        ]
    )


@tool(auto_login=False)
async def moodle_progresso(curso: str, site: str = "atual") -> str:
    """Conclusão das atividades de um curso (o que já foi marcado como feito)."""
    course = await find_course(site, curso)
    try:
        data = await moodle(site).call(
            "core_completion_get_activities_completion_status", courseid=course["id"], userid=await user_id(site)
        )
    except MoodleError as e:
        return _json({"curso": course["fullname"], "progresso": course.get("progress"), "detalhe": str(e)})
    return _json(
        {
            "curso": course["fullname"],
            "progresso": course.get("progress"),
            "atividades": [
                {"id": s.get("cmid"), "tipo": s.get("modname"), "concluida": s.get("state") in (1, 2), "quando": ts(s.get("timecompleted"))}
                for s in data.get("statuses", [])
            ],
        }
    )


@tool(auto_login=False)
async def moodle_emblemas(site: str = "atual") -> str:
    """Emblemas (badges) conquistados no Moodle."""
    data = await moodle(site).call("core_badges_get_user_badges", userid=await user_id(site))
    return _json(
        [
            {"nome": b.get("name"), "descricao": b.get("description"), "emitido": ts(b.get("dateissued")), "curso": b.get("courseid")}
            for b in data.get("badges", [])
        ]
    )


# ---- conteúdo ---------------------------------------------------------------


async def _contents(site: str, course: dict) -> list[dict]:
    return await moodle(site).call("core_course_get_contents", courseid=course["id"])


@tool(auto_login=False)
async def moodle_conteudo(curso: str, site: str = "atual") -> str:
    """Conteúdo de um curso: seções e itens (arquivos, pastas, páginas, links,
    rótulos, fóruns...), com arquivos de cada item e links/vídeos embutidos nas
    descrições. `curso`: id, código do componente, nome curto ou trecho."""
    course = await find_course(site, curso)
    out = []
    for s in await _contents(site, course):
        items = []
        for m in s.get("modules", []):
            files = [
                {
                    "nome": f"{(f.get('filepath') or '/').lstrip('/')}{f.get('filename')}",
                    "tamanho": f.get("filesize"),
                    "modificado": ts(f.get("timemodified")),
                }
                for f in m.get("contents", [])
                if f.get("type") == "file" and not (m.get("modname") == "page" and f.get("filename") == "index.html")
            ]
            url = next((f.get("fileurl") for f in m.get("contents", []) if f.get("type") == "url"), None)
            item = {
                "id": m.get("id"),
                "tipo": m.get("modname"),
                "nome": m.get("name"),
                "descricao": html_text(m.get("description")),
                "links": html_links(m.get("description")),
                "arquivos": files,
            }
            if url:
                item["url_externa"] = url
            if not m.get("uservisible", True):
                item["restrito"] = html_text(m.get("availabilityinfo")) or True
            items.append({k: v for k, v in item.items() if v not in ("", [], None)})
        out.append(
            {"secao": s.get("name"), "resumo": html_text(s.get("summary")), "links": html_links(s.get("summary")), "itens": items}
        )
    return _cut(_json({"curso": course["fullname"], "secoes": out}))


def link_kind(url: str) -> str:
    u = url.lower()
    if "youtu.be" in u or "youtube.com" in u:
        return "youtube"
    if "drive.google.com" in u or "docs.google.com" in u:
        return "google-drive"
    if "moodle3.ifsc.edu.br" in u:
        return "moodle-antigo (pode estar fora do ar)"
    if "pluginfile.php" in u:
        return "arquivo-do-moodle"
    if u.split("?")[0].endswith(".pdf"):
        return "pdf"
    return "externo"


async def _module_texts(site: str, course: dict) -> tuple[list[dict], dict[int, dict]]:
    """Seções do curso e, por cmid, o HTML completo de páginas, pastas e URLs
    (descrições de pastas e conteúdo de páginas não vêm em core_course_get_contents)."""
    m = moodle(site)
    ids = [course["id"]]
    sections = await _contents(site, course)
    extra: dict[int, dict] = {}
    for fn, key in (
        ("mod_page_get_pages_by_courses", "pages"),
        ("mod_folder_get_folders_by_courses", "folders"),
        ("mod_url_get_urls_by_courses", "urls"),
    ):
        try:
            data = await m.call(fn, courseids=ids)
        except MoodleError:
            continue
        for item in data.get(key, []):
            extra[item.get("coursemodule")] = item
    return sections, extra


def _materials(sections: list[dict], extra: dict[int, dict]) -> list[dict]:
    out = []
    for s in sections:
        sec = s.get("name")
        for url in html_links(s.get("summary")):
            out.append({"secao": sec, "item": "(resumo da seção)", "tipo": link_kind(url), "url": url})
        for mod in s.get("modules", []):
            info = extra.get(mod.get("id"), {})
            base = {"secao": sec, "item": mod.get("name"), "modulo": mod.get("modname"), "cmid": mod.get("id")}
            for f in mod.get("contents", []):
                if f.get("type") == "file" and not (mod.get("modname") == "page" and f.get("filename") == "index.html"):
                    out.append({**base, "tipo": "arquivo", "arquivo": f.get("filename"), "tamanho": f.get("filesize")})
            html_fields = [mod.get("description"), info.get("intro"), info.get("content")]
            urls = [u for h in html_fields for u in html_links(h)]
            if info.get("externalurl"):
                urls.insert(0, info["externalurl"])
            for url in dict.fromkeys(urls):
                out.append({**base, "tipo": link_kind(url), "url": url})
    return out


@tool(auto_login=False)
async def moodle_materiais(curso: str, tipo: str = "", site: str = "atual") -> str:
    """Todos os materiais de estudo de um curso numa lista: arquivos e os links
    que ficam dentro de páginas, pastas, rótulos, URLs e resumos de seção,
    classificados (youtube, google-drive, pdf, arquivo, externo...). Muitos
    professores do IFSC põem vídeos e PDFs como links, não como arquivos.
    `tipo` filtra (ex.: 'youtube')."""
    course = await find_course(site, curso)
    items = _materials(*await _module_texts(site, course))
    if tipo:
        items = [i for i in items if tipo.lower() in i["tipo"]]
    return _cut(_json({"curso": course["fullname"], "total": len(items), "materiais": items}))


@tool(auto_login=False)
async def moodle_buscar_material(texto: str, curso: str = "", site: str = "atual") -> str:
    """Busca por palavras nos nomes, textos e links dos materiais de um curso
    ou de todos os cursos em andamento (ex.: 'derivadas parciais videoaulas')."""
    targets = [await find_course(site, curso)] if curso else await courses(site, "inprogress")
    words = pages.norm(texto).split()
    hits = []
    for course in targets:
        sections, extra = await _module_texts(site, course)
        for s in sections:
            for mod in s.get("modules", []):
                info = extra.get(mod.get("id"), {})
                body = " ".join(
                    [s.get("name") or "", mod.get("name") or "", html_text(mod.get("description")),
                     html_text(info.get("intro")), html_text(info.get("content"))]
                    + [f.get("filename", "") for f in mod.get("contents", [])]
                )
                norm_body = pages.norm(body)
                if all(w in norm_body for w in words):
                    hits.append(
                        {"curso": course.get("fullname"), "secao": s.get("name"), "item": mod.get("name"),
                         "modulo": mod.get("modname"), "cmid": mod.get("id")}
                    )
    return _cut(_json(hits))


@tool(auto_login=False)
async def moodle_ler_item(curso: str, item: str, site: str = "atual") -> str:
    """Lê um item do curso pelo nome (ou cmid): texto completo de página, rótulo
    ou descrição de pasta (com fórmulas TeX), arquivos e links; para URL, o
    endereço; para arquivo PDF, baixa e devolve o texto."""
    course = await find_course(site, curso)
    sections, extra = await _module_texts(site, course)
    modules = [{**mod, "secao": s.get("name")} for s in sections for mod in s.get("modules", [])]
    mod = pick(modules, item, "name")
    info = extra.get(mod.get("id"), {})
    kind = mod.get("modname")
    if kind == "resource":
        f = next((f for f in mod.get("contents", []) if f.get("type") == "file"), None)
        if f is None:
            raise MoodleError("Este recurso não tem arquivo.")
        data, ctype = await moodle(site).download(f["fileurl"])
        folder = DOWNLOAD_DIR / "moodle" / (course.get("shortname") or str(course["id"]))
        return _file_result(f["filename"], data, ctype or f.get("mimetype", ""), folder)
    html_fields = [info.get("content"), info.get("intro"), mod.get("description")]
    text = "\n\n".join(t for t in (html_text(h) for h in html_fields) if t)
    return _cut(
        _json(
            {
                "item": mod.get("name"),
                "tipo": kind,
                "secao": mod.get("secao"),
                "texto": text,
                "url": info.get("externalurl"),
                "links": [{"tipo": link_kind(u), "url": u} for u in dict.fromkeys(u for h in html_fields for u in html_links(h))],
                "arquivos": [
                    f.get("filename") for f in mod.get("contents", [])
                    if f.get("type") == "file" and f.get("filename") != "index.html"
                ],
                "restricao": html_text(mod.get("availabilityinfo")) or None,
            }
        )
    )


def _course_files(sections: list[dict]) -> list[dict]:
    return [
        {**f, "modulo": m.get("name", ""), "caminho": f"{(f.get('filepath') or '/').lstrip('/')}{f.get('filename')}"}
        for s in sections
        for m in s.get("modules", [])
        for f in m.get("contents", [])
        if f.get("type") == "file" and not (m.get("modname") == "page" and f.get("filename") == "index.html")
    ]


@tool(auto_login=False)
async def moodle_baixar_arquivo(curso: str, arquivo: str, pasta: str = "", site: str = "atual") -> str:
    """Baixa um arquivo do curso (de recurso ou pasta), pelo nome do arquivo,
    do item ou trecho, para ~/Downloads/SIGAA/moodle/<curso>. PDFs voltam
    também com o texto."""
    course = await find_course(site, curso)
    files = _course_files(await _contents(site, course))
    f = pick(files, arquivo, "filename", "caminho", "modulo")
    data, ctype = await moodle(site).download(f["fileurl"])
    folder = Path(pasta).expanduser() if pasta else DOWNLOAD_DIR / "moodle" / (course.get("shortname") or str(course["id"]))
    return _file_result(f["filename"], data, ctype or f.get("mimetype", ""), folder)


@tool(auto_login=False)
async def moodle_baixar_curso(curso: str, pasta: str = "", limite_mb: float = 200, site: str = "atual") -> str:
    """Baixa todos os arquivos de um curso, mantendo seções/pastas, para
    ~/Downloads/SIGAA/moodle/<curso>. Para quando o total passar de `limite_mb`."""
    course = await find_course(site, curso)
    root = Path(pasta).expanduser() if pasta else DOWNLOAD_DIR / "moodle" / (course.get("shortname") or str(course["id"]))
    saved, skipped, total = [], [], 0
    for f in _course_files(await _contents(site, course)):
        size = f.get("filesize") or 0
        if total + size > limite_mb * 1024 * 1024:
            skipped.append(f["caminho"])
            continue
        data, _ = await moodle(site).download(f["fileurl"])
        target = root / re.sub(r'[<>:"|?*]', "_", f["modulo"]) / (f.get("filepath") or "/").strip("/")
        target.mkdir(parents=True, exist_ok=True)
        path = target / f["filename"]
        path.write_bytes(data)
        total += len(data)
        saved.append(str(path))
    return _json({"pasta": str(root), "arquivos": len(saved), "megabytes": round(total / 1e6, 1), "pulados_por_limite": skipped})


# ---- prazos e calendário ----------------------------------------------------


def _event(e: dict) -> dict:
    return {
        "quando": ts(e.get("timesort") or e.get("timestart")),
        "curso": (e.get("course") or {}).get("fullname"),
        "atividade": e.get("name"),
        "tipo": e.get("modulename") or e.get("eventtype"),
        "acao": (e.get("action") or {}).get("name"),
        "atrasado": bool(e.get("overdue")),
        "url": e.get("url"),
    }


@tool(auto_login=False)
async def moodle_prazos(dias: int = 14, incluir_atrasados: bool = True, site: str = "atual") -> str:
    """Prazos de todos os cursos: atividades atrasadas (últimos 60 dias) e as
    que vencem nos próximos `dias`."""
    now = int(time.time())
    start = now - 60 * 86400 if incluir_atrasados else now
    data = await moodle(site).call(
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=start,
        timesortto=now + max(dias, 1) * 86400,
        limitnum=50,
    )
    return _json([_event(e) for e in data.get("events", [])])


@tool(auto_login=False)
async def moodle_calendario(mes: int = 0, ano: int = 0, site: str = "atual") -> str:
    """Eventos do calendário do Moodle num mês (padrão: mês atual): eventos de
    site, curso, grupo e do usuário."""
    today = datetime.now()
    data = await moodle(site).call(
        "core_calendar_get_calendar_monthly_view", year=ano or today.year, month=mes or today.month
    )
    events = [
        _event(e)
        for week in data.get("weeks", [])
        for day in week.get("days", [])
        for e in day.get("events", [])
    ]
    return _json({"mes": f"{mes or today.month:02d}/{ano or today.year}", "eventos": events})


# ---- notas ------------------------------------------------------------------


@tool(auto_login=False)
async def moodle_notas(curso: str = "", site: str = "atual") -> str:
    """Notas do Moodle. Sem `curso`, a nota de cada curso (visão geral). Com
    `curso`, todos os itens: categoria, peso, nota, intervalo, porcentagem,
    média da turma e feedback. As notas oficiais ficam no SIGAA; muitos
    professores usam o Moodle só como apoio."""
    m = moodle(site)
    if not curso:
        data = await m.call("gradereport_overview_get_course_grades", userid=await user_id(site))
        names = {c["id"]: c.get("fullname") for c in await courses(site)}
        return _json(
            [{"curso": names.get(g.get("courseid"), g.get("courseid")), "nota": g.get("grade")} for g in data.get("grades", [])]
        )
    course = await find_course(site, curso)
    data = await m.call("gradereport_user_get_grade_items", courseid=course["id"], userid=await user_id(site))
    items = []
    for report in data.get("usergrades", []):
        for g in report.get("gradeitems", []):
            name = g.get("itemname") or {"course": "Total do curso", "category": "Total da categoria"}.get(
                g.get("itemtype"), g.get("itemtype")
            )
            items.append(
                {
                    k: v
                    for k, v in {
                        "item": name,
                        "tipo": g.get("itemtype"),
                        "categoria": g.get("categoryid"),
                        "peso": g.get("weightformatted"),
                        "nota": g.get("gradeformatted"),
                        "intervalo": g.get("rangeformatted"),
                        "porcentagem": g.get("percentageformatted"),
                        "media_turma": g.get("averageformatted"),
                        "contribuicao": g.get("contributiontocoursetotal"),
                        "feedback": html_text(g.get("feedback")),
                    }.items()
                    if v not in (None, "", "-")
                }
            )
    return _json({"curso": course["fullname"], "notas": items})


# ---- tarefas ----------------------------------------------------------------


def _assign_configs(a: dict) -> dict:
    cfg: dict[str, dict[str, str]] = {}
    for c in a.get("configs", []):
        cfg.setdefault(c.get("plugin", ""), {})[c.get("name", "")] = c.get("value", "")
    types = [p for p in ("file", "onlinetext") if cfg.get(p, {}).get("enabled") == "1"]
    out = {
        "tipos_de_envio": types,
        "envio_em_rascunho": bool(a.get("submissiondrafts")),
        "exige_declaracao": bool(a.get("requiresubmissionstatement")),
        "em_grupo": bool(a.get("teamsubmission")),
    }
    if "file" in types:
        f = cfg["file"]
        out["max_arquivos"] = int(f.get("maxfilesubmissions") or 0) or None
        size = int(f.get("maxsubmissionsizebytes") or 0)
        out["max_bytes"] = size or None
        out["tipos_aceitos"] = f.get("filetypeslist") or "qualquer"
    if "onlinetext" in types:
        out["limite_palavras"] = int(cfg["onlinetext"].get("wordlimit") or 0) or None
    return out


async def _assignments(site: str, course: dict) -> list[dict]:
    data = await moodle(site).call("mod_assign_get_assignments", courseids=[course["id"]])
    return [a for c in data.get("courses", []) for a in c.get("assignments", [])]


@tool(auto_login=False)
async def moodle_tarefas(curso: str = "", site: str = "atual") -> str:
    """Tarefas (assign): prazo, data de corte e status da entrega. Sem `curso`,
    lista as de todos os cursos (sem status)."""
    targets = [await find_course(site, curso)] if curso else await courses(site, "inprogress")
    out = []
    for course in targets:
        for a in await _assignments(site, course):
            item = {
                "curso": course.get("fullname"),
                "id": a.get("id"),
                "nome": a.get("name"),
                "abre": ts(a.get("allowsubmissionsfromdate")),
                "prazo": ts(a.get("duedate")),
                "corte": ts(a.get("cutoffdate")),
            }
            if curso:
                status = await moodle(site).call("mod_assign_get_submission_status", assignid=a["id"])
                sub = (status.get("lastattempt") or {}).get("submission") or {}
                item["entrega"] = sub.get("status")
                item["nota"] = html_text(((status.get("feedback") or {}).get("gradefordisplay")) or "") or None
            out.append(item)
    return _json(out)


@tool(auto_login=False)
async def moodle_tarefa(curso: str, tarefa: str, site: str = "atual") -> str:
    """Detalhes de uma tarefa: enunciado, anexos do professor, regras de envio
    (tipos, número e tamanho de arquivos, formatos), datas, status da entrega,
    arquivos já enviados, nota e feedback."""
    course = await find_course(site, curso)
    a = pick(await _assignments(site, course), tarefa, "name")
    status = await moodle(site).call("mod_assign_get_submission_status", assignid=a["id"])
    attempt = status.get("lastattempt") or {}
    sub = attempt.get("submission") or attempt.get("teamsubmission") or {}
    sent_files, sent_text = [], ""
    for plugin in sub.get("plugins", []):
        for area in plugin.get("fileareas", []):
            sent_files += [f.get("filename") for f in area.get("files", [])]
        for field in plugin.get("editorfields", []):
            sent_text = html_text(field.get("text")) or sent_text
    feedback = status.get("feedback") or {}
    comments = [
        html_text(field.get("text"))
        for p in feedback.get("plugins", [])
        for field in p.get("editorfields", [])
        if field.get("text")
    ]
    return _cut(
        _json(
            {
                "curso": course["fullname"],
                "id": a["id"],
                "nome": a.get("name"),
                "enunciado": html_text(a.get("intro")),
                "anexos_do_professor": [f.get("filename") for f in a.get("introattachments", [])],
                "abre": ts(a.get("allowsubmissionsfromdate")),
                "prazo": ts(a.get("duedate")),
                "corte": ts(a.get("cutoffdate")),
                "regras": _assign_configs(a),
                "declaracao": html_text(a.get("submissionstatement")) or None,
                "status_envio": sub.get("status"),
                "status_avaliacao": attempt.get("gradingstatus"),
                "pode_editar": attempt.get("canedit"),
                "pode_enviar_para_avaliacao": attempt.get("cansubmit"),
                "arquivos_enviados": sent_files,
                "texto_enviado": sent_text,
                "nota": html_text(feedback.get("gradefordisplay") or "") or None,
                "comentarios": comments,
            }
        )
    )


def _read_file(path_str: str) -> tuple[str, bytes, str]:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise MoodleError(f"Arquivo não encontrado: {path}")
    if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        raise MoodleError(f"{path.name} passa do limite de {MAX_UPLOAD_MB:g} MB (SIGAA_MAX_UPLOAD_MB).")
    return path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _accepts(rule: str, filename: str) -> bool:
    if not rule or rule == "qualquer":
        return True
    ext = Path(filename).suffix.lower()
    tokens = [t.strip().lower() for t in re.split(r"[,;\s]+", rule) if t.strip()]
    # Grupos como "document" ou "image" não dá para checar aqui; o Moodle valida.
    if any(not t.startswith(".") for t in tokens):
        return True
    return ext in tokens


@tool(auto_login=False)
async def moodle_enviar_tarefa(
    curso: str,
    tarefa: str,
    arquivos: list[str] | None = None,
    texto: str = "",
    finalizar: bool = False,
    confirmar: bool = False,
    site: str = "atual",
) -> str:
    """Entrega uma tarefa do Moodle: envia `arquivos` (caminhos locais) e/ou
    `texto` (texto online) e salva a entrega. Com `finalizar=true`, também
    envia para avaliação (aceitando a declaração de autoria, se houver) — em
    tarefas com rascunho, isso impede novas edições.

    Sem `confirmar=true` só valida e mostra uma prévia. Só confirme depois que
    o usuário aprovar, e mostre a declaração antes de finalizar."""
    m = moodle(site)
    course = await find_course(site, curso)
    a = pick(await _assignments(site, course), tarefa, "name")
    rules = _assign_configs(a)
    status = await m.call("mod_assign_get_submission_status", assignid=a["id"])
    attempt = status.get("lastattempt") or {}
    now = time.time()

    problems = []
    if not (arquivos or texto):
        problems.append("Informe `arquivos` e/ou `texto`.")
    if arquivos and "file" not in rules["tipos_de_envio"]:
        problems.append("Esta tarefa não aceita arquivos.")
    if texto and "onlinetext" not in rules["tipos_de_envio"]:
        problems.append("Esta tarefa não aceita texto online.")
    if a.get("cutoffdate") and now > a["cutoffdate"]:
        problems.append(f"O prazo final (corte) já passou: {ts(a['cutoffdate'])}.")
    if a.get("allowsubmissionsfromdate") and now < a["allowsubmissionsfromdate"]:
        problems.append(f"Os envios só abrem em {ts(a['allowsubmissionsfromdate'])}.")
    if attempt.get("canedit") is False:
        problems.append("O Moodle não permite editar esta entrega agora.")
    files = [_read_file(p) for p in (arquivos or [])]
    if rules.get("max_arquivos") and len(files) > rules["max_arquivos"]:
        problems.append(f"No máximo {rules['max_arquivos']} arquivo(s).")
    for name, data, _ in files:
        if rules.get("max_bytes") and len(data) > rules["max_bytes"]:
            problems.append(f"{name} passa do limite de {rules['max_bytes'] / 1e6:.1f} MB da tarefa.")
        if not _accepts(rules.get("tipos_aceitos", ""), name):
            problems.append(f"{name}: formato não aceito ({rules['tipos_aceitos']}).")
    if problems:
        raise MoodleError(" ".join(problems))

    late = bool(a.get("duedate") and now > a["duedate"])
    details = {
        "curso": course["fullname"],
        "tarefa": a.get("name"),
        "arquivos": [f"{n} ({len(d)} bytes)" for n, d, _ in files],
        "texto": texto[:500],
        "finalizar": finalizar,
        "atrasada": late,
        "declaracao": html_text(a.get("submissionstatement")) if finalizar and a.get("requiresubmissionstatement") else None,
        "observacao": "Arquivos enviados substituem os da entrega atual." if files else None,
    }
    if not confirmar:
        return _preview("entregar tarefa no Moodle", details)

    plugindata: dict = {}
    if files:
        plugindata["files_filemanager"] = await m.upload_draft(files)
    if texto:
        draft = await m.call("core_files_get_unused_draft_itemid")
        plugindata["onlinetext_editor"] = {"text": texto, "format": 1, "itemid": draft.get("itemid", 0)}
    warnings = await m.call("mod_assign_save_submission", assignmentid=a["id"], plugindata=plugindata)
    result = {"salvo": not warnings, "avisos": warnings or []}
    if finalizar and not warnings:
        warnings = await m.call(
            "mod_assign_submit_for_grading",
            assignmentid=a["id"],
            acceptsubmissionstatement=bool(a.get("requiresubmissionstatement")),
        )
        result["enviado_para_avaliacao"] = not warnings
        result["avisos"] += warnings or []
    status = await m.call("mod_assign_get_submission_status", assignid=a["id"])
    result["status_envio"] = (((status.get("lastattempt") or {}).get("submission")) or {}).get("status")
    return _json(result)


# ---- questionários ----------------------------------------------------------


@tool(auto_login=False)
async def moodle_questionarios(curso: str, site: str = "atual") -> str:
    """Questionários (quiz) do curso: abertura, fechamento, tempo, tentativas
    permitidas e feitas, e melhor nota. Não inicia tentativas."""
    m = moodle(site)
    course = await find_course(site, curso)
    data = await m.call("mod_quiz_get_quizzes_by_courses", courseids=[course["id"]])
    out = []
    for q in data.get("quizzes", []):
        attempts = await m.call("mod_quiz_get_user_attempts", quizid=q["id"], status="all")
        best = await m.call("mod_quiz_get_user_best_grade", quizid=q["id"])
        out.append(
            {
                "id": q["id"],
                "nome": q.get("name"),
                "descricao": html_text(q.get("intro")),
                "abre": ts(q.get("timeopen")),
                "fecha": ts(q.get("timeclose")),
                "tempo_limite_min": (q.get("timelimit") or 0) // 60 or None,
                "tentativas_permitidas": q.get("attempts") or "ilimitadas",
                "tentativas_feitas": len(attempts.get("attempts", [])),
                "melhor_nota": best.get("grade") if best.get("hasgrade") else None,
                "nota_maxima": q.get("grade"),
            }
        )
    return _json(out)


# ---- fóruns -----------------------------------------------------------------


async def _forums(site: str, course_ids: list[int]) -> list[dict]:
    return await moodle(site).call("mod_forum_get_forums_by_courses", courseids=course_ids)


@tool(auto_login=False)
async def moodle_foruns(curso: str, site: str = "atual") -> str:
    """Fóruns do curso: tipo (avisos ou geral), descrição e número de tópicos."""
    course = await find_course(site, curso)
    return _json(
        [
            {"id": f["id"], "nome": f.get("name"), "tipo": "avisos" if f.get("type") == "news" else f.get("type"),
             "descricao": html_text(f.get("intro")), "topicos": f.get("numdiscussions")}
            for f in await _forums(site, [course["id"]])
        ]
    )


async def _discussions(site: str, forum_id: int, limit: int) -> list[dict]:
    data = await moodle(site).call(
        "mod_forum_get_forum_discussions", forumid=forum_id, sortorder=1, page=0, perpage=limit
    )
    return [
        {
            "id": d.get("discussion"),
            "post_id": d.get("id"),
            "assunto": d.get("name") or d.get("subject"),
            "autor": d.get("userfullname"),
            "criado": ts(d.get("created")),
            "ultima_resposta": ts(d.get("timemodified")),
            "respostas": d.get("numreplies"),
            "mensagem": html_text(d.get("message"))[:2000],
            "anexos": [a.get("filename") for a in d.get("attachments", []) or []],
        }
        for d in data.get("discussions", [])
    ]


@tool(auto_login=False)
async def moodle_topicos(curso: str, forum: str, limite: int = 20, site: str = "atual") -> str:
    """Tópicos de um fórum do curso (mais recentes primeiro), com a mensagem
    inicial. Use `moodle_posts` para ler as respostas de um tópico."""
    course = await find_course(site, curso)
    f = pick(await _forums(site, [course["id"]]), forum, "name")
    return _cut(_json({"forum": f.get("name"), "topicos": await _discussions(site, f["id"], limite)}))


@tool(auto_login=False)
async def moodle_avisos(limite_por_forum: int = 5, site: str = "atual") -> str:
    """Avisos (fóruns de notícias) de todos os cursos em andamento."""
    active = await courses(site, "inprogress")
    names = {c["id"]: c.get("fullname") for c in active}
    out = []
    for f in await _forums(site, list(names)):
        if f.get("type") != "news":
            continue
        for d in await _discussions(site, f["id"], limite_por_forum):
            out.append({"curso": names.get(f.get("course")), **d})
    out.sort(key=lambda d: datetime.strptime(d["criado"], "%d/%m/%Y %H:%M") if d["criado"] else datetime.min, reverse=True)
    return _cut(_json(out))


@tool(auto_login=False)
async def moodle_posts(topico_id: int, site: str = "atual") -> str:
    """Todas as mensagens de um tópico de fórum (id do tópico de moodle_topicos)."""
    data = await moodle(site).call("mod_forum_get_discussion_posts", discussionid=topico_id, sortby="created", sortdirection="ASC")
    return _cut(
        _json(
            [
                {
                    "id": p.get("id"),
                    "autor": (p.get("author") or {}).get("fullname"),
                    "quando": ts(p.get("timecreated")),
                    "assunto": p.get("subject"),
                    "mensagem": html_text(p.get("message")),
                    "responde_a": p.get("parentid"),
                    "anexos": [a.get("filename") for a in p.get("attachments", []) or []],
                }
                for p in data.get("posts", [])
            ]
        )
    )


@tool(auto_login=False)
async def moodle_postar_forum(
    curso: str,
    forum: str,
    assunto: str,
    mensagem: str,
    anexos: list[str] | None = None,
    confirmar: bool = False,
    site: str = "atual",
) -> str:
    """Cria um tópico num fórum do curso (ex.: 'Dúvidas'). Sem `confirmar=true`
    só mostra a prévia; confirme apenas depois que o usuário aprovar."""
    course = await find_course(site, curso)
    f = pick(await _forums(site, [course["id"]]), forum, "name")
    files = [_read_file(p) for p in (anexos or [])]
    if not confirmar:
        return _preview(
            "criar tópico no fórum",
            {"curso": course["fullname"], "forum": f.get("name"), "assunto": assunto, "mensagem": mensagem,
             "anexos": [n for n, _, _ in files]},
        )
    options = []
    if files:
        options.append({"name": "attachmentsid", "value": await moodle(site).upload_draft(files)})
    result = await moodle(site).call(
        "mod_forum_add_discussion", forumid=f["id"], subject=assunto, message=mensagem, options=options
    )
    return _json({"criado": True, "topico_id": result.get("discussionid"), "avisos": result.get("warnings", [])})


@tool(auto_login=False)
async def moodle_responder_forum(post_id: int, mensagem: str, confirmar: bool = False, site: str = "atual") -> str:
    """Responde a uma mensagem de fórum (`post_id` de moodle_posts). Sem
    `confirmar=true` só mostra a prévia."""
    if not confirmar:
        return _preview("responder no fórum", {"responde_ao_post": post_id, "mensagem": mensagem})
    result = await moodle(site).call(
        "mod_forum_add_discussion_post", postid=post_id, subject="Re:", message=mensagem
    )
    return _json({"enviado": True, "post_id": result.get("postid"), "avisos": result.get("warnings", [])})


# ---- mensagens e notificações -----------------------------------------------


@tool(auto_login=False)
async def moodle_mensagens(site: str = "atual") -> str:
    """Conversas do Moodle (individuais, de grupo e notas pessoais) com o número
    de mensagens não lidas. Não marca nada como lido."""
    m = moodle(site)
    data = await m.call("core_message_get_conversations", userid=await user_id(site), limitnum=50)
    kinds = {1: "individual", 2: "grupo", 3: "pessoal"}
    return _json(
        [
            {
                "id": c.get("id"),
                "nome": c.get("name") or ", ".join(u.get("fullname", "") for u in c.get("members", [])),
                "tipo": kinds.get(c.get("type"), c.get("type")),
                "nao_lidas": c.get("unreadcount"),
                "ultima": html_text((c.get("messages") or [{}])[0].get("text")) if c.get("messages") else "",
            }
            for c in data.get("conversations", [])
        ]
    )


@tool(auto_login=False)
async def moodle_conversa(conversa_id: int, limite: int = 30, site: str = "atual") -> str:
    """Mensagens de uma conversa (mais recentes primeiro). Não marca como lidas."""
    data = await moodle(site).call(
        "core_message_get_conversation_messages",
        currentuserid=await user_id(site),
        convid=conversa_id,
        limitnum=limite,
        newest=True,
    )
    members = {u.get("id"): u.get("fullname") for u in data.get("members", [])}
    return _json(
        [
            {"de": members.get(msg.get("useridfrom"), msg.get("useridfrom")), "quando": ts(msg.get("timecreated")),
             "texto": html_text(msg.get("text"))}
            for msg in data.get("messages", [])
        ]
    )


@tool(auto_login=False)
async def moodle_enviar_mensagem(conversa_id: int, texto: str, confirmar: bool = False, site: str = "atual") -> str:
    """Envia uma mensagem numa conversa existente (id de moodle_mensagens). Sem
    `confirmar=true` só mostra a prévia."""
    if not confirmar:
        return _preview("enviar mensagem no Moodle", {"conversa": conversa_id, "texto": texto})
    result = await moodle(site).call(
        "core_message_send_messages_to_conversation",
        conversationid=conversa_id,
        messages=[{"text": texto, "textformat": 2}],
    )
    return _json({"enviada": True, "mensagens": [r.get("id") for r in result or []]})


@tool(auto_login=False)
async def moodle_notificacoes(limite: int = 20, site: str = "atual") -> str:
    """Notificações recentes (fóruns, tarefas, notas...). Não marca como lidas."""
    data = await moodle(site).call(
        "message_popup_get_popup_notifications", useridto=await user_id(site), limit=limite, offset=0, newestfirst=1
    )
    return _json(
        [
            {
                "quando": ts(n.get("timecreated")),
                "assunto": n.get("subject"),
                "texto": html_text(n.get("smallmessage") or n.get("fullmessagehtml") or n.get("fullmessage")),
                "origem": n.get("component"),
                "lida": bool(n.get("read")),
                "url": n.get("contexturl"),
            }
            for n in data.get("notifications", [])
        ]
    )


# ---- consulta direta --------------------------------------------------------


@tool(auto_login=False)
async def moodle_api(funcao: str, parametros: dict | None = None, site: str = "atual") -> str:
    """Chama diretamente uma função de leitura da API do Moodle (com 'get' ou
    'search' no nome), para o que as outras ferramentas não cobrem. Ex.:
    funcao='mod_folder_get_folders_by_courses', parametros={'courseids': [123]}."""
    if not re.search(r"_(get|search)_|_get$", funcao):
        return "Por segurança, só funções de leitura (com 'get' ou 'search' no nome) são permitidas."
    return _cut(_json(await moodle(site).call(funcao, **(parametros or {}))))
