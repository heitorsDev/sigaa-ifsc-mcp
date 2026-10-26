"""Buscas do SIGAA para alunos logados (Ensino > Consultas).

A área pública (/sigaa/public) fica atrás do CAPTCHA, então tudo aqui parte
do menu do portal do discente.
"""

from __future__ import annotations

import re

from . import pages
from .client import PORTAL_PATH, Page, SigaaClient, SigaaError
from .parsers import clean


async def open_from_menu(client: SigaaClient, action: str) -> Page:
    portal = await client.get(PORTAL_PATH)
    entry = pages.find_menu_item(portal, action)
    page = await client.post(pages.menu_form(portal, entry["acao"]))
    errors = pages.messages(page)["erros"]
    if errors:
        raise SigaaError("SIGAA: " + " ".join(errors))
    return page


def paired_field(page: Page, form_id: str, check_suffix: str) -> tuple[str, str]:
    """Nos formulários de busca, cada filtro é um checkbox `...checkX` com o
    campo de valor na mesma linha. Os ids gerados mudam, então o campo é
    achado pela posição. Retorna (nome do checkbox, nome do campo)."""
    form = page.soup.find("form", id=form_id)
    if form is None:
        raise SigaaError(f'Formulário de busca "{form_id}" não encontrado.')
    check = next(
        (i for i in form.find_all("input", type="checkbox") if i.get("name", "").endswith(check_suffix)), None
    )
    if check is None:
        raise SigaaError(f'Filtro "{check_suffix}" não encontrado no formulário.')
    def is_value_field(el) -> bool:
        if el is check or not el.get("name"):
            return False
        return el.name == "select" or (el.get("type") or "text").lower() == "text"

    row = check.find_parent("tr") or form
    field = next((el for el in row.find_all(["input", "select"]) if is_value_field(el)), None)
    if field is None:
        raise SigaaError(f'Campo do filtro "{check_suffix}" não encontrado.')
    return check["name"], field["name"]


# ---- Consultar Turma --------------------------------------------------------


def parse_class_search(page: Page, limit: int) -> list[dict]:
    table = page.soup.select_one("#lista-turmas") or page.soup.select_one("table.listagem")
    if table is None:
        return []
    out, component = [], ""
    for tr in table.find_all("tr"):
        if "destaque" in (tr.get("class") or []):
            component = clean(tr)
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 8:
            continue
        nearby = str(tr) + str(tr.find_next_sibling("tr") or "")
        m = re.search(r"PainelTurma\.show\((\d+)\)", nearby)
        out.append(
            {
                "componente": component,
                "periodo": clean(cells[0]),
                "turma": clean(cells[1]),
                "docentes": clean(cells[2]),
                "tipo": clean(cells[3]),
                "situacao": clean(cells[4]),
                "horario": clean(cells[5]),
                "local": clean(cells[6]),
                "matriculados": clean(cells[7]),
                "id": m.group(1) if m else None,
            }
        )
        if len(out) >= limit:
            break
    return out


async def search_classes(client: SigaaClient, filters: dict[str, str], limit: int) -> list[dict]:
    """`filters`: chaves do formulário sem o prefixo 'form:' e valores, por
    exemplo {'Disciplina': 'cálculo'} liga 'form:checkDisciplina' e preenche
    o campo correspondente."""
    page = await open_from_menu(client, "buscaTurmaBean.popularBuscaGeral")
    fields = {
        "Disciplina": "form:inputNomeDisciplina",
        "Docente": "form:inputNomeDocente",
        "Codigo": "form:inputCodDisciplina",
        "Horario": "form:inputHorario",
        "Local": "form:inputLocal",
        "Situacao": "form:selectSituacaoTurma",
    }
    campos: dict = {}
    for key, value in filters.items():
        if key == "AnoPeriodo":
            ano, _, periodo = value.partition(".")
            campos.update({"form:checkAnoPeriodo": True, "form:inputAno": ano, "form:inputPeriodo": periodo})
        elif value:
            campos[f"form:check{key}"] = True
            campos[fields[key]] = value
    form, _, _ = pages.build_form(page, "form", campos, "Buscar")
    result = await client.post(form)
    errors = pages.messages(result)["erros"]
    if errors:
        raise SigaaError("SIGAA: " + " ".join(errors))
    return parse_class_search(result, limit)


CLASS_DETAILS_PATH = "/sigaa/graduacao/turma/view_painel.jsf?ajaxRequest=true&contarMatriculados=true&id={id}"


# ---- Consultar Componente Curricular -----------------------------------------


async def search_components(client: SigaaClient, nome: str, codigo: str) -> Page:
    page = await open_from_menu(client, "componenteCurricular.popularBuscaDiscente")
    campos: dict = {}
    for suffix, value in (("checkNome", nome), ("checkCodigo", codigo)):
        if value:
            check, field = paired_field(page, "formBusca", suffix)
            campos[check] = True
            campos[field] = value
    form, _, _ = pages.build_form(page, "formBusca", campos, "Buscar")
    result = await client.post(form)
    errors = pages.messages(result)["erros"]
    if errors:
        raise SigaaError("SIGAA: " + " ".join(errors))
    return result


def parse_table(page: Page, limit: int = 100) -> list[dict]:
    """Primeira table.listagem como lista de dicionários (cabeçalho -> valor)."""
    table = page.soup.select_one("table.listagem")
    if table is None:
        return []
    header = [clean(th) for th in table.select("thead th, thead td")] or [
        clean(th) for th in table.find_all("th")
    ]
    out = []
    for tr in table.select("tbody tr") or table.find_all("tr")[1:]:
        cells = [clean(td) for td in tr.find_all("td", recursive=False)]
        if not any(cells):
            continue
        out.append({(header[i] if i < len(header) and header[i] else f"col{i}"): v for i, v in enumerate(cells)})
        if len(out) >= limit:
            break
    return out


# ---- Calendário acadêmico ----------------------------------------------------


async def academic_calendar(client: SigaaClient) -> tuple[Page, Page | None]:
    """Busca os calendários com os filtros padrão e abre o primeiro (o mais
    recente). Retorna (lista de calendários, detalhe ou None)."""
    page = await open_from_menu(client, "calendario.iniciarBusca")
    form, _, _ = pages.build_form(page, "form", {}, "Buscar")
    listing = await client.post(form)
    link = next(
        (
            link
            for link in pages.links(listing)
            if link["form"] and any(k.endswith(":visualizar") for k in link["form"].values)
        ),
        None,
    )
    detail = await client.post(link["form"]) if link else None
    return listing, detail
