"""Grade de horários a partir do comprovante de matrícula.

No SIGAA do IFSC a grade só aparece no comprovante de matrícula
(Ensino > Matrícula On-Line > Ver Comprovante). Turmas de horário flexível
não são desenhadas na grade: o horário real vem como códigos com período de
validade, ex.: "4T34 6T12 (03/08/2026 - 22/12/2026)".

Código de horário: dígitos dos dias (2=seg ... 7=sáb), turno (M, T, N) e os
números das aulas no turno. "24M34" = seg e qua, manhã, 3ª e 4ª aulas.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .client import Page
from .parsers import clean

DAYS = {"2": "Segunda", "3": "Terça", "4": "Quarta", "5": "Quinta", "6": "Sexta", "7": "Sábado"}
TURNOS = "MTN"

# Horários das aulas no IFSC, usados se o comprovante não trouxer a grade.
DEFAULT_SLOTS = {
    "M": ["07:30-08:25", "08:25-09:20", "09:40-10:35", "10:35-11:30", "12:00-13:30"],
    "T": ["13:30-14:25", "14:25-15:20", "15:40-16:35", "16:35-17:30"],
    "N": ["18:30-19:25", "19:25-20:20", "20:40-21:35", "21:35-22:30"],
}

_CODE = re.compile(r"\b([2-7]+)([MTN])(\d+)\b")
_RANGE = re.compile(r"([^,()]*?)\s*\((\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})\)")
_TIME = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")


def parse_slot_times(page: Page) -> dict[str, list[str]]:
    """Horário de cada aula por turno, lido das linhas da grade
    (table.formulario). Linhas separadoras (colspan) mudam de turno."""
    table = next(
        (t for t in page.soup.select("table.formulario") if "Horários" in clean(t.find("tr"))),
        None,
    )
    if table is None:
        return DEFAULT_SLOTS
    slots: dict[str, list[str]] = {t: [] for t in TURNOS}
    turno = 0
    for tr in table.find_all("tr"):
        first = tr.find("td")
        if first is None:
            continue
        if first.get("colspan"):
            if slots[TURNOS[turno]]:
                turno = min(turno + 1, len(TURNOS) - 1)
            continue
        m = _TIME.search(clean(first))
        if m:
            slots[TURNOS[turno]].append(f"{m.group(1)}-{m.group(2)}")
    return slots if any(slots.values()) else DEFAULT_SLOTS


def parse_patterns(text: str) -> list[dict]:
    """'4T34 6T12 (03/08/2026 - 22/12/2026), 3N12 (...)' -> padrões com validade.
    Sem datas, o texto inteiro vale para o semestre todo."""
    found = [
        {"codigos": codes.strip(), "inicio": start, "fim": end}
        for codes, start, end in _RANGE.findall(text)
        if codes.strip()
    ]
    if not found and _CODE.search(text):
        found = [{"codigos": text.strip(), "inicio": "", "fim": ""}]
    return found


def decode(codes: str, slots: dict[str, list[str]]) -> list[dict]:
    """Expande códigos de horário em aulas: dia, turno, nº da aula e horário."""
    out = []
    for days, turno, numbers in _CODE.findall(codes):
        for d in days:
            for n in numbers:
                idx = int(n) - 1
                times = slots.get(turno, [])
                start, _, end = (times[idx] if 0 <= idx < len(times) else "").partition("-")
                out.append({"dia": DAYS[d], "turno": turno, "aula": int(n), "inicio": start, "fim": end})
    return out


def _course_title(text: str) -> tuple[str, str, str]:
    """'COD - NOME - 60h (horário)' -> (código, nome, horário entre parênteses)."""
    m = re.match(r"\s*(\S+)\s+-\s+(.+?)\s+-\s+\d+h\b(.*)$", text)
    if not m:
        code, _, rest = text.partition(" - ")
        return code.strip(), rest.strip(), ""
    inside = re.search(r"\((.*)\)\s*$", m.group(3))
    return m.group(1), m.group(2).strip(), inside.group(1) if inside else ""


def parse_enrollment(page: Page) -> dict:
    """Comprovante de matrícula: turmas (local, situação, horários) e a
    validade de cada padrão de horário das turmas flexíveis."""
    soup = page.soup
    root = soup.select_one("#relatorio") or soup
    header = clean(root.find(["h2", "h3"])) if root.find(["h2", "h3"]) else ""
    period = re.search(r"Per[ií]odo\s*:?\s*(\d{4}\.\d)", clean(root))

    courses: dict[str, dict] = {}
    listing = root.select_one("table.listagem")
    for tr in listing.find_all("tr") if listing else []:
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        code, name, horario = _course_title(clean(cells[0]))
        courses[code] = {
            "codigo": code,
            "nome": name,
            "turma": clean(cells[1]),
            "local": clean(cells[2]),
            "situacao": clean(cells[3]),
            "padroes": parse_patterns(horario) if horario else [],
        }

    flexible = root.select_one("table.subFormulario")
    for tr in flexible.find_all("tr") if flexible else []:
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        code, name, _ = _course_title(clean(cells[0]))
        entry = courses.setdefault(
            code, {"codigo": code, "nome": name, "turma": clean(cells[1]), "local": "", "situacao": "", "padroes": []}
        )
        entry["padroes"] = parse_patterns(clean(cells[2])) or entry["padroes"]
        entry["horario_flexivel"] = True

    return {
        "titulo": header,
        "periodo": period.group(1) if period else "",
        "turmas": list(courses.values()),
    }


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


def active_patterns(course: dict, on: date) -> list[dict]:
    """Padrões de horário válidos numa data (os sem datas valem sempre)."""
    out = []
    for p in course.get("padroes", []):
        start, end = _parse_date(p["inicio"]), _parse_date(p["fim"])
        if (start is None or start <= on) and (end is None or on <= end):
            out.append(p)
    return out


def week_grid(courses: list[dict], slots: dict[str, list[str]], on: date) -> dict[str, list[dict]]:
    """Grade da semana de `on`: por dia, as aulas em ordem de horário."""
    grid: dict[str, list[dict]] = {name: [] for name in DAYS.values()}
    for c in courses:
        if "cancel" in c.get("situacao", "").lower():
            continue
        for p in active_patterns(c, on):
            for aula in decode(p["codigos"], slots):
                grid[aula["dia"]].append(
                    {
                        "inicio": aula["inicio"],
                        "fim": aula["fim"],
                        "codigo_horario": f'{aula["turno"]}{aula["aula"]}',
                        "disciplina": f'{c["codigo"]} - {c["nome"]}',
                        "local": c.get("local", ""),
                    }
                )
    for day in grid:
        grid[day].sort(key=lambda a: (a["inicio"] or "99", a["codigo_horario"]))
    return {day: aulas for day, aulas in grid.items() if aulas}
