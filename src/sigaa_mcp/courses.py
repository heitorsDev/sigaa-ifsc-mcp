"""Navegação até a turma virtual e suas seções."""

from __future__ import annotations

import unicodedata

from . import parsers
from .client import TURMAS_PATH, Page, SigaaClient, SigaaError


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").lower().split())


async def list_courses(client: SigaaClient, all_periods: bool = False) -> list[dict]:
    return parsers.parse_courses(await client.get(TURMAS_PATH), all_periods)


async def find_course(client: SigaaClient, query: str) -> dict:
    """Acha a turma pelo código ou por um trecho do nome (sem acentos)."""
    courses = await list_courses(client, all_periods=False)
    q = _norm(query)
    for match in (
        lambda c: _norm(c["codigo"]) == q,
        lambda c: _norm(c["nome"]) == q,
        lambda c: q in _norm(f'{c["codigo"]} {c["nome"]}'),
    ):
        found = [c for c in courses if match(c)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = ", ".join(f'{c["codigo"]} - {c["nome"]}' for c in found)
            raise SigaaError(f'"{query}" corresponde a mais de uma turma: {names}.')
    names = "; ".join(f'{c["codigo"]} - {c["nome"]}' for c in courses) or "nenhuma"
    raise SigaaError(f'Turma "{query}" não encontrada. Turmas do semestre: {names}.')


async def open_course(client: SigaaClient, query: str) -> tuple[dict, Page]:
    course = await find_course(client, query)
    if course["form"] is None:
        raise SigaaError(f'A turma {course["codigo"]} não tem link para a turma virtual.')
    page = await client.post(course["form"])
    if "Menu Turma Virtual" not in page.html and not parsers.course_menu(page):
        raise SigaaError("Não foi possível abrir a turma virtual.")
    return course, page


async def open_section(client: SigaaClient, query: str, section: str) -> tuple[dict, Page]:
    """Abre uma seção do menu da turma virtual (ex.: 'Ver Notas', 'Tarefas')."""
    course, page = await open_course(client, query)
    if _norm(section) == "principal":
        return course, page
    menu = parsers.course_menu(page)
    wanted = _norm(section)
    label = next((k for k in menu if _norm(k) == wanted), None) or next(
        (k for k in menu if wanted in _norm(k)), None
    )
    if label is None:
        raise SigaaError(
            f'Seção "{section}" não existe nesta turma. Seções: {", ".join(menu) or "nenhuma"}.'
        )
    result = await client.post(menu[label])
    code = parsers.course_code(result)
    if code and course["codigo"] and code != course["codigo"]:
        raise SigaaError(
            f"O SIGAA abriu outra turma ({code}) em vez de {course['codigo']}; tente de novo."
        )
    return course, result
