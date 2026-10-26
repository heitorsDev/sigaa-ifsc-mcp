"""Extração de dados das páginas do SIGAA.

Seletores e regras portados de sigaa-api (GeovaneSchmitz/sigaa-api, MIT).
"""

from __future__ import annotations

import copy
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from .client import Form, Page, SigaaError


def clean(el: Tag | NavigableString | str | None) -> str:
    """Texto do elemento com espaços normalizados (equivale a removeTagsHtml)."""
    if el is None:
        return ""
    if isinstance(el, Tag):
        el = el.get_text(" ")
    return re.sub(r"\s+", " ", str(el).replace("\xa0", " ")).strip()


def lines(el: Tag | None) -> list[str]:
    if el is None:
        return []
    return [clean(t) for t in el.get_text("\n").split("\n") if clean(t)]


def page_text(page: Page, max_chars: int = 20000) -> str:
    """Texto legível da página, com tabelas como linhas separadas por ' | '."""
    soup = BeautifulSoup(page.html, "html.parser")
    for el in soup(["script", "style", "noscript", "head"]):
        el.decompose()
    # Menus JSCookMenu e elementos ocultos só poluem o texto.
    for el in soup.select('[style*="display:none"], [style*="display: none"]'):
        el.decompose()
    for tr in reversed(soup.find_all("tr")):
        cells = [clean(c) for c in tr.find_all(["td", "th"], recursive=False)]
        cells = [c for c in cells if c]
        tr.replace_with(NavigableString("\n" + " | ".join(cells) + "\n"))
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncado em {max_chars} caracteres]"
    return text


def _onclick_form(page: Page, el: Tag | None) -> Form | None:
    link = el if el is not None and el.get("onclick") else (el.find(onclick=True) if el else None)
    if link is None:
        return None
    try:
        return page.parse_jsfcljs(link["onclick"])
    except SigaaError:
        return None


# ---- portal do discente ---------------------------------------------------


def parse_profile(page: Page) -> dict:
    soup = page.soup
    data: dict = {
        "nome": clean(soup.select_one("p.usuario > span")),
        "periodo_atual": clean(soup.select_one("#info-usuario > p.periodo-atual > strong")),
    }
    table = soup.select_one("#perfil-docente table")
    if table:
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) == 2:
                key = clean(cells[0]).rstrip(":")
                if key:
                    data[key] = clean(cells[1])
    switch = soup.select_one("#info-usuario p i small a")
    data["multiplos_vinculos"] = clean(switch) == "Alterar vínculo"
    return data


def parse_activities(page: Page) -> list[dict]:
    """Tabela 'Atividades' do portal (#avaliacao-portal)."""
    out = []
    for tr in page.soup.select("#avaliacao-portal > table tbody > tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        img = cells[0].find("img")
        info = lines(cells[2].find("small") or cells[2])
        if not info:
            continue
        kind, _, title = (info[1] if len(info) > 1 else info[0]).partition(": ")
        out.append(
            {
                "data": clean(cells[1]),
                "disciplina": info[0],
                "tipo": kind if title else "",
                "titulo": title or kind,
                "concluida": bool(img and img.get("src", "").endswith("/check.png")),
            }
        )
    return out


def parse_courses(page: Page, all_periods: bool = False) -> list[dict]:
    """Tabela de turmas de /portais/discente/turmas.jsf.

    Cada turma vem com o formulário JSF (`form`) que abre a turma virtual."""
    soup = page.soup
    table = soup.select_one(".listagem")
    if table is None:
        return []
    cols: dict[str, int] = {}
    for i, td in enumerate(table.select("thead > tr td")):
        name = clean(td)
        key = {"Disciplina": "title", "Turma": "class", "Horário": "schedule",
               "Alunos": "students", "": "button"}.get(name)
        if key and key not in cols:
            cols[key] = i
    if "title" not in cols or "button" not in cols:
        raise SigaaError("Tabela de turmas em formato inesperado.")

    rows = table.select("tbody > tr")
    periods = [clean(r.find("td")) for r in rows if r.find("td", class_="periodo")]
    latest = max(periods) if periods and not all_periods else None

    courses, period = [], None
    for tr in rows:
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        if "periodo" in (cells[0].get("class") or []):
            period = clean(cells[0])
            continue
        if period is None or (latest and period != latest):
            continue
        if len(cells) <= max(cols.values()):
            continue
        fullname = clean(cells[cols["title"]])
        code, _, title = fullname.partition(" - ")
        form = _onclick_form(page, cells[cols["button"]])
        courses.append(
            {
                "codigo": code,
                "nome": title or fullname,
                "turma": clean(cells[cols["class"]]) if "class" in cols else "",
                "horario": clean(cells[cols["schedule"]]) if "schedule" in cols else "",
                "periodo": period,
                "id": form.values.get("idTurma") if form else None,
                "form": form,
            }
        )
    return courses


# ---- turma virtual --------------------------------------------------------


def _rich_menu_form(page: Page, item: Tag) -> Form | None:
    """Item de menu RichFaces (div.rich-menu-item, RichFaces.Menu.submitForm):
    POST com os campos do formulário + `<id do item>=<id do item>`."""
    form_el = item.find_parent("form")
    item_id = item.get("id", "")
    if form_el is None or not item_id or not form_el.get("action"):
        return None
    values = {
        i["name"]: i.get("value", "")
        for i in form_el.find_all("input")
        if i.get("name") and i.get("type") != "submit"
    }
    form_name = form_el.get("id") or form_el.get("name") or ""
    if form_name:
        values.setdefault(form_name, form_name)
    params = re.findall(r"'([^']+)'\s*:\s*'([^']*)'", item.get("onclick", ""))
    values.update(params or [(item_id, item_id)])
    return Form(urljoin(page.url, form_el["action"]), values)


def course_menu(page: Page) -> dict[str, Form]:
    """Itens do menu da turma virtual -> formulário. O SIGAA do IFSC usa dois
    layouts: a barra lateral (#formMenu, links JSF; div.itemMenu nas versões
    antigas) e o menu suspenso RichFaces (#formMenuDrop, div.rich-menu-item)."""
    items: dict[str, Form] = {}
    soup = page.soup
    for div in soup.select("div.itemMenu"):
        parent = div.parent
        label = clean(div)
        if label and parent is not None and parent.get("onclick"):
            try:
                items.setdefault(label, page.parse_jsfcljs(parent["onclick"]))
            except SigaaError:
                pass
    for a in soup.select('#formMenu a[onclick*="jsfcljs"]'):
        label = clean(a)
        if label:
            try:
                items.setdefault(label, page.parse_jsfcljs(a["onclick"]))
            except SigaaError:
                pass
    for div in soup.select("div.rich-menu-item"):
        label = clean(div)
        form = _rich_menu_form(page, div) if label else None
        if form is not None:
            items.setdefault(label, form)
    return items


def course_code(page: Page) -> str:
    el = page.soup.select_one("#linkCodigoTurma")
    if el is not None:
        return clean(el).rstrip(" -")
    return clean(page.soup.select_one("#relatorio h3")).split(" - ")[0]


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_grades(page: Page) -> list[dict]:
    """Página 'Ver Notas' da turma (table.tabelaRelatorio)."""
    soup = page.soup
    table = soup.select_one("table.tabelaRelatorio")
    if table is None:
        raise SigaaError("Página de notas sem tabela.")
    head_rows = table.select("thead tr")
    value_row = table.select_one("tbody tr")
    if not head_rows or value_row is None:
        return []
    values = [clean(td) for td in value_row.find_all(["td", "th"], recursive=False)]
    top = head_rows[0].find_all("th", recursive=False)
    sub = head_rows[1].find_all("th", recursive=False) if len(head_rows) > 1 else []

    def hidden(prefix: str, gid: str) -> str:
        el = soup.select_one(f"input#{prefix}_{gid}")
        return el.get("value", "") if el else ""

    types = {"P": "media-ponderada", "A": "media-aritmetica", "S": "soma"}
    grades, pos = [], 0
    for i, th in enumerate(top):
        name = clean(th)
        span = int(th.get("colspan", 1) or 1)
        start, pos = pos, pos + span
        if name in ("", "Matrícula", "Nome", "Sit.", "Faltas"):
            continue
        tipo_el = soup.select_one(f"#tipoUnid{i}")
        kind = types.get(tipo_el.get("value", "") if tipo_el else "", "apenas-media")
        if kind == "apenas-media" or span == 1:
            grades.append({"unidade": name, "valor": _num(values[start]) if start < len(values) else None})
            continue
        group = {"unidade": name, "tipo": kind, "avaliacoes": [], "valor": None}
        for j in range(start, start + span):
            gid = (sub[j].get("id", "") if j < len(sub) else "")[5:]
            val = _num(values[j]) if j < len(values) else None
            if not gid:
                group["valor"] = val
                continue
            item = {"nome": hidden("denAval", gid), "sigla": hidden("abrevAval", gid), "valor": val}
            if kind == "soma":
                item["valor_maximo"] = _num(hidden("notaAval", gid))
            elif kind == "media-ponderada":
                item["peso"] = _num(hidden("pesoAval", gid))
            group["avaliacoes"].append(item)
        grades.append(group)

    for label, idx in (("situacao", "Sit."), ("faltas", "Faltas")):
        pos = 0
        for th in top:
            if clean(th) == idx and pos < len(values):
                grades.append({label: values[pos]})
            pos += int(th.get("colspan", 1) or 1)
    return grades


def parse_absences(page: Page) -> dict:
    """Página 'Frequência' da turma."""
    days = []
    for tr in page.soup.select(".listing tr[class]"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        status = clean(cells[1])
        if status in ("", "Não Informada"):
            continue
        faltas = 0 if status == "Presente" else int(re.sub(r"\D", "", status) or 0)
        days.append({"data": clean(cells[0]), "faltas": faltas})
    total = maximo = None
    for line in lines(page.soup.select_one(".botoes-show")):
        digits = re.sub(r"\D", "", line)
        if "Total de Faltas" in line and digits:
            total = int(digits)
        elif "Máximo de Faltas Permitido" in line and digits:
            maximo = int(digits)
    return {"total_faltas": total, "maximo_faltas": maximo, "aulas": days}


def parse_news_list(page: Page) -> list[dict]:
    out = []
    for tr in page.soup.select(".listing tr[class]"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        form = _onclick_form(page, cells[2].find(onclick=True)) if len(cells) > 2 else None
        out.append(
            {
                "titulo": clean(cells[0]),
                "data": clean(cells[1]) if len(cells) > 1 else "",
                "form": form,
            }
        )
    return out


def parse_news_detail(page: Page) -> dict:
    ul = page.soup.select_one("ul.form")
    if ul is None:
        raise SigaaError("Página de notícia em formato inesperado.")
    spans = ul.find_all("span")
    body = ul.find("div")
    return {
        "data": clean(spans[1]) if len(spans) > 1 else "",
        "conteudo": "\n".join(lines(body)),
    }


def parse_homeworks(page: Page) -> list[dict]:
    """Página 'Tarefas' da turma: linhas em pares (dados + descrição)."""
    out = []
    for table in page.soup.select(".listing"):
        legend = table.find_previous_sibling("legend")
        group = clean(legend) == "Tarefas Em Grupo"
        rows = table.select("tr[class]")
        for k in range(0, len(rows), 2):
            cells = rows[k].find_all("td", recursive=False)
            if len(cells) < 4:
                continue
            desc = rows[k + 1] if k + 1 < len(rows) else None
            out.append(
                {
                    "titulo": clean(cells[1]),
                    "periodo": clean(cells[2]),
                    "vale_nota": clean(cells[3]) != "Não",
                    "em_grupo": group,
                    "pode_enviar": len(cells) > 5 and cells[5].find(onclick=True) is not None,
                    "enviada": len(cells) > 6 and cells[6].find(onclick=True) is not None,
                    "descricao": "\n".join(lines(desc)) if desc else "",
                    "form": _onclick_form(page, cells[5]) if len(cells) > 5 else None,
                }
            )
    return out


def parse_exam_calendar(page: Page) -> list[dict]:
    """Card 'Avaliações' na lateral direita da página principal da turma."""
    for header in page.soup.select(".rich-stglpanel-header.headerBloco"):
        if clean(header) != "Avaliações":
            continue
        card = header.parent.parent if header.parent else None
        if card is None:
            return []
        return [
            {
                "descricao": clean(li.select_one("span.descricao")),
                "data": clean(li.select_one("span.data")),
            }
            for li in card.find_all("li")
        ]
    return []


def parse_files(page: Page) -> list[dict]:
    """Seção 'Arquivos' da turma (Título | Descrição | Tópico de Aula | ícone):
    título, descrição, tópico e o formulário de download, com `id` e `key`.
    O clique fica no ícone (img "Baixar Arquivo") ou num link, conforme a versão."""
    out = []
    for tr in page.soup.select(".listing tr[class]"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        link = next(
            (el for c in cells[1:] for el in c.find_all(onclick=True) if "jsfcljs" in el["onclick"]),
            None,
        )
        form = _onclick_form(page, link)
        out.append(
            {
                "titulo": clean(cells[0]),
                "descricao": clean(cells[1]) if len(cells) > 1 else "",
                "topico": clean(cells[2]) if len(cells) > 3 else "",
                "id": form.values.get("id") if form else None,
                "key": form.values.get("key") if form else None,
                "form": form,
            }
        )
    return out


_ITEM_TYPES = {"pdf": "arquivo", "txt": "arquivo", "tarefa": "tarefa", "site_add": "link"}


def parse_topics(page: Page) -> list[dict]:
    """Tópicos de aula da página principal da turma (div.topico-aula), com os
    materiais de cada um: arquivos, tarefas e links."""
    topics = []
    for div in page.soup.select("div.topico-aula"):
        title = clean(div.select_one("div.titulo"))
        dates = re.search(r"\((\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})\)", title)
        body = div.select_one("div.conteudotopico")
        items = []
        for item in body.select("div.item") if body else []:
            a = item.find("a")
            if a is None:
                continue
            onclick = a.get("onclick", "")
            form = _onclick_form(page, a) if "jsfcljs" in onclick else None
            img = item.find("img")
            icon = re.sub(r"\.\w+$", "", (img.get("src", "") if img else "").rsplit("/", 1)[-1])
            if form and "idEnviarMaterialTarefa" in onclick:
                kind = "tarefa"
            elif form and form.values.get("key"):
                kind = "arquivo"
            elif a.get("href", "").startswith("http"):
                kind = "link"
            else:
                kind = _ITEM_TYPES.get(icon, icon or "item")
            items.append(
                {
                    "tipo": kind,
                    "nome": clean(a),
                    "descricao": clean(item.select_one("div.descricao-item")),
                    "id": form.values.get("id") if form else None,
                    "key": form.values.get("key") if form else None,
                    "url": a["href"] if kind == "link" else None,
                    "form": form,
                }
            )
        text_body = copy.copy(body) if body is not None else None  # não altera a página
        if text_body is not None:
            for item in text_body.select("div.item"):
                item.decompose()
        topics.append(
            {
                "titulo": re.sub(r"\s*\(\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}\)", "", title),
                "inicio": dates.group(1) if dates else "",
                "fim": dates.group(2) if dates else "",
                "conteudo": "\n".join(lines(text_body)),
                "materiais": items,
            }
        )
    return topics


def strip_forms(items: list[dict]) -> list[dict]:
    """Remove os formulários internos antes de devolver ao cliente MCP."""
    return [{k: v for k, v in i.items() if k != "form"} for i in items]
