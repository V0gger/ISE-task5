#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
import sys
import os

# ----------------------------------------------------------------------
# Чтение входного файла
# ----------------------------------------------------------------------
def read_input(path):
    with open(path, encoding='utf-8') as f:
        raw_lines = f.read().splitlines()

    # отрезаем комментарии вне секций (символ '#')
    formats = {}          # имя формата -> список '1'/'0'
    stages = None
    conflict = 'memory'
    code_lines = []
    section = None

    for line in raw_lines:
        stripped = line.lstrip()
        if stripped.startswith('#'):
            continue  # строка-комментарий целиком
        # вне секции [code] символ '#' начинает комментарий;
        # в секции [code] комментарии не поддерживаются (в строках кода может
        # встречаться '#'), поэтому там строку берём целиком
        if section != 'code':
            line = line.split('#')[0].rstrip()
        s = line.strip()
        if not s:
            continue
        m = re.match(r'^\[(\w+)\]\s*(.*)$', s)
        if m and m.group(1) in ('formats', 'pipeline', 'code'):
            section = m.group(1)
            s = m.group(2).strip()
            if not s:
                continue
        # внутри секции код '#' может встречаться в комментариях - уже убран
        if section == 'formats':
            name, pattern = s.split(':')
            formats[name.strip()] = [c.strip() for c in pattern.split(',')]
        elif section == 'pipeline':
            key, val = s.split(':')
            val = val.strip()
            if key.strip() == 'stages':
                stages = int(val)
            elif key.strip() == 'conflict':
                conflict = val
        elif section == 'code':
            code_lines.append(s)

    return formats, stages, conflict, code_lines

OPERAND_RE = re.compile(r'\b[RM]\w+\b')

def parse_commands(code_lines, formats):
    """Разбирает строки вида: L1: ALU, R1 <- R2, R3  или  L7: JMP -> L10 ; условие: R7"""
    commands = []
    for i, s in enumerate(code_lines):
        label, rest = (s.split(':', 1) + [''])[:2] if ':' in s else (f'C{i+1}', s)
        label, rest = label.strip(), rest.strip()
        is_jump = False
        jump_target = None
        cond_ops = []
        # выделяем условие перехода из комментария после ';'
        cond_text = ''
        if ';' in rest:
            main_part, cond_text = rest.split(';', 1)
            rest = main_part.strip()
        else:
            rest = rest
        if '->' in rest:
            is_jump = True
            parts = rest.split('->')
            fmt = parts[0].strip().split(',')[0].strip()
            jump_target = parts[1].strip()
            dst, srcs = None, []
            # операнды условия перехода (например "условие: R7" -> {R7})
            cond_ops = OPERAND_RE.findall(cond_text)
        else:
            fmt, body = rest.split(',', 1)
            fmt = fmt.strip()
            if '<-' in body:
                d, sr = body.split('<-', 1)
                dst = d.strip()
                srcs = [x.strip() for x in sr.split(',')]
            else:
                dst, srcs = None, []
        pattern = formats.get(fmt)
        if pattern is None:
            raise ValueError(f'Неизвестный формат команды: {fmt}')
        commands.append({
            'label': label, 'format': fmt, 'pattern': pattern,
            'dst': dst, 'srcs': srcs,
            'is_jump': is_jump, 'jump_target': jump_target,
            'cond_ops': cond_ops,
        })
    return commands

# ----------------------------------------------------------------------
# Анализ зависимостей (логика программы)
# ----------------------------------------------------------------------
def build_dep_graph(commands):
    n = len(commands)
    succ = [[] for _ in range(n)]   # i -> j: команда j должна идти после команды i
    reasons = {}

    def add(i, j, why):
        if i != j and j not in succ[i]:
            succ[i].append(j)
            reasons[(i, j)] = why

    last_writer = {}
    for j, c in enumerate(commands):
        if not c['is_jump']:
            operands = {c['dst']} | set(c['srcs'])
        else:
            # переход зависит только от команд, записавших операнды его условия
            operands = set(c.get('cond_ops', []))
        for op in sorted(operands, key=lambda x: (x is None, x)):
            if op is None:
                continue
            if op in last_writer:
                i = last_writer[op]
                if c['is_jump']:
                    add(i, j, f'переход {j+1} читает операнд условия {op} команды {i+1}')
                elif op == c['dst']:
                    add(i, j, f'WAR/WAW через {op}: команда {i+1} пишет, команда {j+1} читает/пишет')
                else:
                    add(i, j, f'RAW через {op}: команда {j+1} читает результат команды {i+1}')
        if not c['is_jump'] and c['dst'] is not None:
            last_writer[c['dst']] = j
        # поток управления: сам переход не переносится относительно всех команд;
        # команды после перехода нельзя поднимать выше него.
        # Команды ПЕРЕД переходом можно перемещать между собой и опускать ниже
        # перехода - при условии, что переход не читает их операнды (см. зависимости выше).
        if c['is_jump']:
            for k in range(j + 1, n):
                add(j, k, 'команда находится после перехода')
    return succ, reasons

def transitive_closure(succ):
    n = len(succ)
    reach = [set(succ[i]) for i in range(n)]
    changed = True
    while changed:
        changed = False
        for i in range(n):
            new = set(reach[i])
            for j in list(reach[i]):
                new |= reach[j]
            if new != reach[i]:
                reach[i] = new
                changed = True
    return reach

def swap_conflict(a, b, commands):
    """Проверяет ПРЯМЫЕ потоковые конфликты (RAW/WAR/WAW) между командами a и b.
    Возвращает True, если команды напрямую конфликтуют по операндам."""
    ca, cb = commands[a], commands[b]
    if ca['is_jump'] or cb['is_jump']:
        return True
    wa, sa = ca['dst'], set(ca['srcs'])
    wb, sb = cb['dst'], set(cb['srcs'])
    if wb in sa or wa in sb or (wa is not None and wa == wb):
        return True
    return False

def swappable_pairs(commands, succ):
    """Находит пары команд, которые можно переставить местами, не нарушая логику
    программы. Обмен i <-> j допустим, если:
      * обе команды не являются переходами;
      * после обмена выполняются все рёбра графа зависимостей (RAW/WAR/WAW и
        порядок относительно переходов);
      * ни один переход не смещается относительно других команд (иначе изменится
        поток управления)."""
    result = []
    n = len(commands)
    for i in range(n):
        for j in range(i + 1, n):
            neworder = list(range(n))
            neworder[i], neworder[j] = neworder[j], neworder[i]
            pos = {c: p for p, c in enumerate(neworder)}
            bad = False
            # 1) сохраняются ли все зависимости
            for x in range(n):
                for y in succ[x]:
                    if pos[x] > pos[y]:
                        bad = True
                        break
                if bad:
                    break
            if bad:
                continue
            # 2) переходы остаются на своих позициях относительно всех команд
            for k in range(n):
                if commands[k]['is_jump'] and pos[k] != k:
                    bad = True
                    break
            if bad:
                continue
            result.append((i, j))
    return result

def greedy_reorder(commands, succ, conflict, stages_total):
    """«Жадная» стратегия перестановки: на каждой позиции выбирается доступная
    (все предшественники уже размещены) команда, которая при жадном размещении
    даёт минимальное общее время конвейера. Логика программы не нарушается."""
    n = len(commands)
    preds = [[] for _ in range(n)]
    for i in range(n):
        for j in succ[i]:
            preds[j].append(i)
    remaining = set(range(n))
    pos_of = {}          # индекс -> позиция в новом порядке
    order = []

    def prefix_time(cand):
        seq = [commands[i] for i in order + [cand]]
        _, t, _ = schedule_greedy(seq, stages_total, conflict)
        return t

    while remaining:
        ready = [c for c in sorted(remaining) if all(p in pos_of for p in preds[c])]
        # просматриваем готовые команды; если выбранная команда смещает переход
        # относительно других команд, пробуем следующую (переходы не переносим)
        chosen = None
        for c in sorted(ready, key=lambda x: (prefix_time(x), x)):
            trial = order + [c]
            jump_ok = True
            for jc in range(n):
                if not commands[jc]['is_jump']:
                    continue
                new_pos = trial.index(jc) if jc in trial else None
                if new_pos is None:
                    continue
                cnt_before = sum(1 for q in trial if q != jc and pos_of.get(q, 10**9) < 0)
                # число команд, которые в новом порядке стоят перед переходом,
                # должно совпадать с исходным числом команд перед ним
                orig_before = jc
                now_before = new_pos
                if now_before != orig_before:
                    jump_ok = False
                    break
            if jump_ok:
                chosen = c
                break
        if chosen is None:
            raise RuntimeError('Цикл в графе зависимостей!')
        order.append(chosen)
        pos_of[chosen] = len(order) - 1
        remaining.discard(chosen)
    return order

# ----------------------------------------------------------------------
# Жадное расписание на конвейере
# ----------------------------------------------------------------------
def resource_cells(cmd, conflict_type):
    """Возвращает множество ресурсов, используемых командой на каждом такте.
    Ступень S_t на такте t занимает ресурс того же номера."""
    return None

def shared_resources(cmd, conflict):
    """Возвращает множество имён разделяемых внешних ресурсов, которые занимает
    команда на тех или иных тактах. Возвращается dict: имя ресурса -> список
    относительных тактов (сдвиг внутри команды), когда ресурс занят."""
    res = {}
    patt = cmd['pattern']
    if conflict == 'memory':
        # память общая: конфликтуют команды, обращающиеся к памяти (формат MEM)
        if cmd['format'].upper() == 'MEM':
            res['MEM'] = list(range(len(patt)))
    elif conflict == 'regfile':
        # регистровый файл: чтение источников (первая ступень) и запись приёмника (последняя)
        if cmd['dst'] is not None or cmd['srcs']:
            res['RF'] = [0, len(patt) - 1]
    return res

def schedule_greedy(seq_commands, stages_total, conflict):
    """Жадное размещение: команда ставится в самый ранний возможный такт входа,
    начиная с такта, равного её порядковому номеру (конвейер принимает не более
    одной команды за такт). Условия размещения:
      * ступень S_i в такте t+i свободна (структурный конфликт ступеней);
      * общий внешний ресурс (память/регистровый файл) в соответствующие такты свободен.
    Команды размещаются строго по очереди, поэтому цикл поиска такта конечен."""
    n = len(seq_commands)
    start = [-1] * n
    occupied_stage = {}   # (takt, stage_idx) -> id команды
    occupied_res = {}     # (takt, res_name) -> id команды

    for idx in range(n):
        cmd = seq_commands[idx]
        patt = cmd['pattern']
        ext = shared_resources(cmd, conflict)
        # верхняя граница поиска всегда находится: предыдущие команды уже размещены
        t = idx
        while True:
            ok = True
            for s_i, bit in enumerate(patt):
                if bit != '1':
                    continue
                tk = t + s_i
                if (tk, s_i) in occupied_stage:
                    ok = False
                    break
            if ok:
                for rname, shifts in ext.items():
                    used = set()
                    for sh in shifts:
                        tk = t + sh
                        if tk in used or (tk, rname) in occupied_res:
                            ok = False
                            break
                        used.add(tk)
                    if not ok:
                        break
            if ok:
                for s_i, bit in enumerate(patt):
                    if bit == '1':
                        occupied_stage[(t + s_i, s_i)] = idx
                for rname, shifts in ext.items():
                    for sh in shifts:
                        occupied_res[(t + sh, rname)] = idx
                start[idx] = t
                break
            t += 1
    total_time = max(start[i] + len(seq_commands[i]['pattern']) for i in range(n))
    return start, total_time, occupied_stage

def occupancy_table(seq_commands, start, total_time, stages_total):
    rows = []
    for i, cmd in enumerate(seq_commands):
        row = ['.'] * total_time
        patt = cmd['pattern']
        for s_i, bit in enumerate(patt):
            if bit == '1':
                row[start[i] + s_i] = str(s_i + 1)
        rows.append(row)
    return rows

# ----------------------------------------------------------------------
# Основная часть
# ----------------------------------------------------------------------
def main():
    base = os.path.dirname(os.path.abspath(__file__))
    in_path = os.path.join(base, 'vvod.txt')
    out_path = os.path.join(base, 'vivod.txt')

    formats, stages, conflict, code_lines = read_input(in_path)
    commands = parse_commands(code_lines, formats)
    n = len(commands)
    stages_total = stages or max(len(p) for p in formats.values())

    succ, reasons = build_dep_graph(commands)
    reach = transitive_closure(succ)

    # 1.1 / 1.2 жадная сборка конвейера в исходном порядке
    start1, time1, occ1 = schedule_greedy(commands, stages_total, conflict)
    table1 = occupancy_table(commands, start1, time1, stages_total)

    # 1.3 перестановки
    pairs = swappable_pairs(commands, succ)

    # 1.4 жадная перестановка (выбор доступной команды с минимальным временем префикса)
    order = greedy_reorder(commands, succ, conflict, stages_total)
    reordered = [commands[i] for i in order]
    start2, time2, occ2 = schedule_greedy(reordered, stages_total, conflict)
    table2 = occupancy_table(reordered, start2, time2, stages_total)

    # 1.5 коэффициент роста производительности
    speedup = time1 / time2 if time2 else 1.0

    # ---------------- вывод ----------------
    out = []
    w = out.append
    w('=' * 78)
    w('ЗАДАНИЕ 5. Конвейерная обработка потока команд')
    w('=' * 78)
    w('')
    w('Исходные данные (файл vvod.txt):')
    w(f'  Число ступеней конвейера: {stages_total}; тип конфликта ресурсов: {conflict}')
    w('  Таблицы занятости форматов:')
    for name, patt in formats.items():
        w(f'    {name}: ' + ''.join(patt) + f'  (длительность {len(patt)} тактов)')
    w('')
    w('Поток команд (10 шт.):')
    for i, c in enumerate(commands):
        if c['is_jump']:
            w(f'  {i+1:2d}. {c["label"]}: {c["format"]} -> {c["jump_target"]}  (команда перехода)')
        else:
            w(f'  {i+1:2d}. {c["label"]}: {c["format"]}, {c["dst"]} <- {", ".join(c["srcs"])}')
    w('')

    def render_table(title, cmds, start, table, total_time):
        w('-' * 78)
        w(title)
        w('-' * 78)
        header = 'Команда '.ljust(10) + '|'
        for t in range(total_time):
            header += f'{t+1}|'
        w(header)
        w('-' * len(header))
        for i, c in enumerate(cmds):
            tag = f'{i+1}:{c["label"]}'
            row = tag.ljust(10) + '|'
            for cell in table[i]:
                row += cell + '|'
            w(row)
        w('')

    render_table('1.1/1.2 ИТОГОВАЯ ТАБЛИЦА ЗАНЯТОСТИ («жадная» стратегия, исходный порядок)',
                 commands, start1, table1, time1)
    w(f'  Время выполнения программы с конвейером (исходный порядок): T1 = {time1} усл. ед. (тактов)')
    w('')

    w('-' * 78)
    w('1.3 Команды, которые можно переставить местами, НЕ нарушая логику программы')
    w('-' * 78)
    if pairs:
        for i, j in pairs:
            w(f'  Можно поменять местами: команда {i+1} ({commands[i]["label"]}) '
              f'<-> команда {j+1} ({commands[j]["label"]})')
    else:
        w('  Независимых пар не найдено.')
    w('')
    w('  Обоснование: пары не связаны отношениями зависимости RAW/WAR/WAW и не разделены')
    w('  командами перехода, влияющими на поток управления.')
    w('')

    render_table('1.4 ИТОГОВАЯ ТАБЛИЦА ЗАНЯТОСТИ С ПЕРЕСТАВЛЕННЫМИ КОМАНДАМИ',
                 reordered, start2, table2, time2)
    w('  Новый порядок входа команд в конвейер:')
    for pos, idx in enumerate(order):
        w(f'    позиция {pos+1}: команда {idx+1} ({commands[idx]["label"]}, формат {commands[idx]["format"]})')
    w('')
    w(f'  Время выполнения программы с конвейером (переставленные команды): T2 = {time2} усл. ед. (тактов)')
    w('')

    w('-' * 78)
    w('1.5 Коэффициент роста производительности')
    w('-' * 78)
    w(f'  K = T1 / T2 = {time1} / {time2} = {speedup:.3f}')
    gain = (1 - time2 / time1) * 100 if time1 else 0
    w(f'  Выигрыш по времени: {time1 - time2} такт(ов) ({gain:.1f}%)')
    w('')
    w('=' * 78)

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')

    print('\n'.join(out))
    print(f'\nРезультаты записаны в {out_path}')

if __name__ == '__main__':
    main()