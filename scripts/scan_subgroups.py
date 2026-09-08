#!/usr/bin/env python3
"""
Разведка: у каких групп в списке «студентов» лежат подгруппы, а не люди.

Зачем. Сайт заводит подгруппы (потоки иностранного языка) как отдельные
сущности и кладёт их в тот же список, что и живых студентов, с тем же
обращением `?selst=N`. У части магистерских групп людей в списке нет вообще.
Отсюда два открытых вопроса, на которые нельзя ответить рассуждением:

  1. У кого ещё, кроме магистров, есть подгруппы?
  2. Отличает ли их текущий парсер — или молча пишет в таблицу students,
     и тогда человек в боте увидит «мг51соврс-1» среди фамилий?

Скрипт отвечает на оба, ничего не меняя: только читает списки групп,
по одному запросу на группу (~47 запросов, около минуты). В базу не пишет,
персональные расписания не трогает.

Использование:
    python scripts/scan_subgroups.py
    python scripts/scan_subgroups.py --json data/subgroups.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup

from core.database import get_connection
from core.db_students import get_groups_for_student_parse
from parsers.base import FetchError
from parsers.socio import SocioParser


def roster_rows(html: str) -> list:
    """Сырые строки списка: id, что в title, что видно текстом."""
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for tr in soup.find_all('tr', onclick=SocioParser.SELST_LINK_RE):
        m = SocioParser.SELST_LINK_RE.search(tr.get('onclick', ''))
        if not m:
            continue
        tds = tr.find_all('td')
        if len(tds) < 2:
            continue
        name_td = tds[1]
        rows.append({
            'selst': m.group(1),
            'title': (name_td.get('title') or '').strip(),
            'text': name_td.get_text(strip=True),
        })
    return rows


def main():
    out_path = None
    if '--json' in sys.argv:
        idx = sys.argv.index('--json')
        if idx + 1 < len(sys.argv):
            out_path = ROOT / sys.argv[idx + 1]

    conn = get_connection()
    groups = get_groups_for_student_parse(conn)
    conn.close()

    if not groups:
        sys.exit("Нет групп в базе. Сначала: python run_parser.py socio")

    parser = SocioParser()
    print(f"Групп для проверки: {len(groups)}\n")

    try:
        parser.download('/index.php?mnu=75', encoding=parser.ENCODING, required=True)
    except FetchError as e:
        sys.exit(f"Не удалось войти в режим студента: {e}")

    report = []
    for g in groups:
        code, site_id = g['code'], g['site_id']
        html = parser.download(f'/index.php?gr={site_id}', encoding=parser.ENCODING)
        rows = roster_rows(html) if html else []

        people, subs, nameless = [], [], []
        for r in rows:
            label = r['text'] or r['title']
            if not label:
                nameless.append(r)
            elif SocioParser.is_subgroup_label(label, code):
                subs.append(r)
            else:
                people.append(r)

        # Попадёт ли подгруппа в таблицу students при нынешнем парсере?
        # _find_students берёт запись только если у неё непустой title.
        leaks = [r for r in subs if r['title']]

        report.append({
            'code': code, 'site_id': site_id, 'rows': len(rows),
            'people': len(people), 'subgroups': len(subs),
            'nameless': len(nameless), 'would_leak': len(leaks),
            'subgroup_labels': [r['text'] for r in subs][:6],
            'sample_person': people[0]['text'] if people else None,
        })

        mark = ''
        if subs:
            mark += f"  подгрупп: {len(subs)}"
        if nameless:
            mark += f"  без имени: {len(nameless)}"
        if not people and rows:
            mark += "  ЛЮДЕЙ НЕТ"
        print(f"  {code:14s} записей {len(rows):3d}, людей {len(people):3d}{mark}")

    print("\n" + "=" * 66)
    print("ИТОГ")
    print("=" * 66)

    with_subs = [r for r in report if r['subgroups']]
    no_people = [r for r in report if r['rows'] and not r['people']]
    leaking = [r for r in report if r['would_leak']]
    nameless = [r for r in report if r['nameless']]

    print(f"  групп всего:                    {len(report)}")
    print(f"  из них с подгруппами:           {len(with_subs)}")
    print(f"  где людей нет вовсе:            {len(no_people)}")
    print(f"  где есть записи без имени:      {len(nameless)}")
    print(f"  подгрупп попало бы в students:  {sum(r['would_leak'] for r in leaking)}")

    if with_subs:
        print("\n  Группы с подгруппами:")
        for r in with_subs:
            print(f"    {r['code']:14s} {r['subgroups']} шт: {', '.join(r['subgroup_labels'])}")

    if leaking:
        print("\n  ⚠️ Эти подгруппы нынешний парсер записал бы как студентов —")
        print("     человек увидел бы их в боте среди фамилий:")
        for r in leaking:
            print(f"    {r['code']}: {', '.join(r['subgroup_labels'])}")
    else:
        print("\n  Подгруппы в students не попадут: у них пустой title,")
        print("  а _find_students берёт только записи с непустым title.")
        print("  Значит расписание подгрупп сейчас просто не собирается.")

    print(f"\n  {parser.stats_line()}")

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        print(f"\n  Подробности: {out_path}")


if __name__ == '__main__':
    main()
