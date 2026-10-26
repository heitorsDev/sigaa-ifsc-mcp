"""Navegação genérica: links e formulários de qualquer página do SIGAA."""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .client import Form, Page, SigaaError
from .parsers import clean


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").lower().split())


def _link_text(a: Tag) -> str:
    img = a.find("img")
    return clean(a) or clean(a.get("title")) or (clean(img.get("alt") or img.get("title")) if img else "")


def links(page: Page) -> list[dict]:
    """Links navegáveis: commandLinks JSF (viram POST) e links normais para o
    próprio SIGAA (viram GET), na ordem em que aparecem na página."""
    out = []
    for a in page.soup.find_all("a"):
        text = _link_text(a)
        if not text:
            continue
        onclick, href = a.get("onclick", ""), a.get("href", "")
        if "jsfcljs" in onclick:
            try:
                out.append({"texto": text, "form": page.parse_jsfcljs(onclick), "url": None})
            except SigaaError:
                continue
        elif href and not href.startswith(("javascript:", "#", "mailto:")):
            url = urljoin(page.url, href)
            if "/sigaa/" in url and "logOff" not in url:
                out.append({"texto": text, "form": None, "url": url})
    for i, item in enumerate(out):
        item["indice"] = i
    return out


def find_link(page: Page, texto: str = "", indice: int = -1) -> dict:
    items = links(page)
    if indice >= 0:
        if indice >= len(items):
            raise SigaaError(f"Não há link com índice {indice} (a página tem {len(items)}).")
        return items[indice]
    q = norm(texto)
    for match in (lambda t: t == q, lambda t: t.startswith(q), lambda t: q in t):
        found = [i for i in items if match(norm(i["texto"]))]
        if found:
            return found[0]
    raise SigaaError(f'Nenhum link com o texto "{texto}" na página atual.')


def _find_form(soup: BeautifulSoup, form_id: str) -> Tag:
    f = soup.find("form", id=form_id) or soup.find("form", attrs={"name": form_id})
    if f is None:
        raise SigaaError(f'Formulário "{form_id}" não está na página atual.')
    return f


def _label(soup: BeautifulSoup, el: Tag) -> str:
    if el.get("id"):
        label = soup.find("label", attrs={"for": el["id"]})
        if label:
            return clean(label).rstrip(":* ")
    td = el.find_parent("td")
    if td is not None:
        prev = td.find_previous_sibling(["th", "td"])
        if prev is not None:
            return clean(prev).rstrip(":* ")
    return clean(el.get("title") or el.get("placeholder") or "")


def _kind(el: Tag) -> str:
    if el.name == "input":
        return (el.get("type") or "text").lower()
    if el.name == "button":
        return "submit" if (el.get("type") or "submit").lower() == "submit" else "button"
    return el.name


def forms(page: Page) -> list[dict]:
    """Descreve os formulários da página: campos editáveis, campos de arquivo e
    botões. Campos ocultos (como javax.faces.ViewState) vão automaticamente."""
    soup = page.soup
    out = []
    for f in soup.find_all("form"):
        fields, files, buttons, hidden = [], [], [], 0
        for el in f.find_all(["input", "select", "textarea", "button"]):
            name = el.get("name")
            if not name:
                continue
            kind = _kind(el)
            if kind == "hidden":
                hidden += 1
            elif kind in ("submit", "image"):
                buttons.append({"nome": name, "texto": el.get("value") or clean(el)})
            elif kind == "file":
                files.append({"nome": name, "rotulo": _label(soup, el), "aceita": el.get("accept", "")})
            elif kind not in ("button", "reset"):
                field = {"nome": name, "tipo": kind, "rotulo": _label(soup, el)}
                if kind == "select":
                    field["opcoes"] = [
                        {"valor": o.get("value", clean(o)), "texto": clean(o)} for o in el.find_all("option")
                    ][:50]
                elif kind in ("checkbox", "radio"):
                    field["valor"] = el.get("value", "on")
                    field["marcado"] = el.has_attr("checked")
                else:
                    field["valor"] = el.get("value", "") if el.name == "input" else el.get_text()
                fields.append(field)
        if fields or files or buttons:
            out.append(
                {
                    "id": f.get("id") or f.get("name") or "",
                    "multipart": "multipart" in (f.get("enctype") or ""),
                    "campos": fields,
                    "arquivos": files,
                    "botoes": buttons,
                    "ocultos": hidden,
                }
            )
    return out


def build_form(
    page: Page, form_id: str, campos: dict, botao: str | None
) -> tuple[Form, list[str], dict]:
    """Monta o que um navegador enviaria no formulário, aplicando `campos` (por
    nome ou rótulo) e o botão (por nome ou texto).

    Retorna (Form, nomes dos campos de arquivo, resumo do que foi alterado)."""
    soup = page.soup
    f = _find_form(soup, form_id)
    values: dict[str, str] = {}
    names: set[str] = set()
    by_label: dict[str, str] = {}
    submits: list[tuple[str, str]] = []
    file_names: list[str] = []
    for el in f.find_all(["input", "select", "textarea", "button"]):
        name = el.get("name")
        if not name:
            continue
        kind = _kind(el)
        if kind in ("submit", "image"):
            submits.append((name, el.get("value") or clean(el)))
            continue
        if kind == "file":
            file_names.append(name)
            continue
        if kind in ("button", "reset"):
            continue
        names.add(name)
        label = _label(soup, el)
        if label:
            by_label.setdefault(norm(label), name)
        if kind in ("checkbox", "radio"):
            if el.has_attr("checked"):
                values[name] = el.get("value", "on")
        elif el.name == "select":
            opts = el.find_all("option")
            sel = next((o for o in opts if o.has_attr("selected")), opts[0] if opts else None)
            if sel is not None:
                values[name] = sel.get("value", clean(sel))
        elif el.name == "textarea":
            values[name] = el.get_text()
        else:
            values[name] = el.get("value", "")

    changed: dict = {}
    for key, value in (campos or {}).items():
        name = key if key in names else by_label.get(norm(key))
        if name is None:
            raise SigaaError(f'Campo "{key}" não existe no formulário "{form_id}".')
        if value is None or value is False:
            values.pop(name, None)
        else:
            values[name] = "on" if value is True else str(value)
        changed[name] = values.get(name)

    if submits:
        chosen = submits[0]
        if botao:
            q = norm(botao)
            match = [s for s in submits if norm(s[0]) == q or q in norm(s[1])]
            if not match:
                raise SigaaError(f'Botão "{botao}" não existe. Botões: {", ".join(s[1] for s in submits)}.')
            chosen = match[0]
        values[chosen[0]] = chosen[1]
        changed["(botão)"] = chosen[1]
    return Form(urljoin(page.url, f.get("action") or page.url), values), file_names, changed


# ---- menu do portal (JSCookMenu) --------------------------------------------

# Itens do array JS do menu: [ícone, 'rótulo', 'ação', alvo, ...]. A ação tem a
# forma '<menu>:A]#{ bean.metodo }' (ação JSF) ou '<menu>:L]...' (link legado).
_MENU_ITEM = re.compile(
    r"\[\s*(?:null|'[^']*'|\"[^\"]*\")\s*,\s*'((?:[^'\\]|\\.)*)'\s*,\s*'((?:[^'\\]|\\.)*)'"
)


def portal_menu(page: Page) -> list[dict]:
    """Itens do menu superior do portal do discente (Ensino, Pesquisa...)."""
    items: dict[tuple[str, str], dict] = {}
    for script in page.soup.find_all("script"):
        js = script.string or script.get_text()
        if "cmDraw" not in js and "A]" not in js:
            continue
        for label, action in _MENU_ITEM.findall(js):
            action = action.replace("\\'", "'")
            if ":A]" not in action and ":L]" not in action:
                continue
            text = clean(BeautifulSoup(html.unescape(label.replace("\\'", "'")), "html.parser").get_text())
            if text:
                items.setdefault((text, action), {"texto": text, "acao": action})
    return list(items.values())


def find_menu_item(page: Page, query: str) -> dict:
    """Acha um item do menu pelo texto ou por um trecho da ação
    (ex.: 'verComprovanteSolicitacoes')."""
    items = portal_menu(page)
    if not items:
        raise SigaaError("Não encontrei o menu do portal nesta página.")
    q = norm(query)
    for match in (
        lambda i: norm(i["texto"]) == q,
        lambda i: query in i["acao"],
        lambda i: norm(i["texto"]).startswith(q),
        lambda i: q in norm(i["texto"]),
    ):
        found = [i for i in items if match(i)]
        if found:
            return found[0]
    raise SigaaError(f'Item de menu "{query}" não encontrado. Use menu_portal para listar os itens.')


def menu_form(page: Page, action: str) -> Form:
    """POST que o JSCookMenu faz ao clicar num item: campos do formulário do
    menu + jscook_action."""
    soup = page.soup
    f = soup.find("form", id=re.compile("form_menu_discente")) or next(
        (f for f in soup.find_all("form") if f.find("input", attrs={"name": "jscook_action"})), None
    )
    if f is None or not f.get("action"):
        raise SigaaError("Formulário do menu do portal não encontrado.")
    values = {
        i["name"]: i.get("value", "")
        for i in f.find_all("input")
        if i.get("name") and (i.get("type") or "text").lower() not in ("submit", "button", "image")
    }
    form_id = f.get("id") or f.get("name") or ""
    if form_id:
        values[form_id] = form_id
    values["jscook_action"] = action
    return Form(urljoin(page.url, f["action"]), values)


def messages(page: Page) -> dict:
    """Mensagens de sucesso/erro que o SIGAA mostra depois de um envio."""
    soup = page.soup
    errors = list(dict.fromkeys(clean(li) for li in soup.select("#painel-erros ul.erros li, ul.erros li")))
    infos = [clean(li) for li in soup.select("ul.info li, .info li, ul.warning li")]
    ok = "Operação realizada com sucesso" in page.html or any("sucesso" in norm(i) for i in infos)
    return {"sucesso": ok and not errors, "erros": errors, "avisos": infos}
