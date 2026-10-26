# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Структурная проверка nftables-файлов видеослужбы (ADR-0016 §3.3, контракт видеослужбы §8):
tools/image/nft/sheregesh-video-console.nft (пульт: UDP 7444 и 7445 только с адреса второй платы сеанса) и
tools/image/nft/sheregesh-video-remote.nft (блок: UDP 7445 только с адреса пульта). Наборы peer4/peer6 при загрузке пусты.
Ограничение: «синтаксис nft на реальной системе не проверяется (нет nft на Mac); проверка — структурная».
Проверяется разбор текста: шапка SPDX, идемпотентная загрузка таблицы, пустые типизированные наборы, хук input с
приоритетом -10 и policy accept, три правила udp dport по порядку (drop последним, со счётчиком), запрещённые
конструкции (ct state, tcp, established, flush ruleset, 7443, порт 22, dport 7444 отдельно), баланс скобок,
множество портов из правил.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NFT_DIR = ROOT / "tools/image/nft"
TABLE = "sheregesh_video"
# файл -> (выражение порта в правилах udp dport, ожидаемое множество портов)
FILES = {
    "sheregesh-video-console.nft": ("{ 7444, 7445 }", {7444, 7445}),
    "sheregesh-video-remote.nft": ("7445", {7445}),
}
# наборы -> ожидаемый тип адреса; при загрузке наборы пусты (без elements)
SET_TYPES = {"peer4": "ipv4_addr", "peer6": "ipv6_addr"}
# правила цепочки input после «udp dport <порт>»: адрес по IPv4, адрес по IPv6, затем drop со счётчиком
RULE_TAILS = ["ip saddr @peer4 accept", "ip6 saddr @peer6 accept", "counter drop"]
RULE_RE = re.compile(r"^udp dport (\{[^}]*\}|\d+) (.+)$")
SET_RE = re.compile(r"\bset\s+(\w+)\s*\{([^{}]*)\}")
HOOK_RE = re.compile(r"^type filter hook input priority (-?\d+); policy accept;$")
# запрещённые в коде (без комментариев) конструкции: (регулярное выражение, описание)
FORBIDDEN = [
    (r"\bct\s+state\b", "ct state"),
    (r"\btcp\b", "tcp"),
    (r"\bestablished\b", "established"),
    (r"\bflush\s+ruleset\b", "flush ruleset"),
    (r"\b7443\b", "порт 7443"),
    (r"\b22\b", "порт 22"),
    (r"\bdport\s+7444\b", "dport 7444 отдельным портом"),
]


def code_lines(text):
    """Строки без комментариев: пустые и начинающиеся с # отбрасываются, пробелы по краям срезаются."""
    return [s for s in (line.strip() for line in text.splitlines()) if s and not s.startswith("#")]


def code_text(text):
    return "\n".join(code_lines(text))


def ports_of(expr):
    return {int(p) for p in re.findall(r"\d+", expr)}


# Каждая проверка принимает (текст файла, выражение порта, ожидаемые порты) и возвращает список найденных проблем.

def check_header(text, expr, ports):
    """SPDX-строки должны стоять в первых пяти строках файла."""
    head = "\n".join(text.splitlines()[:5])
    return [f"в первых строках нет {tag}" for tag in ("SPDX-FileCopyrightText:", "SPDX-License-Identifier:")
            if tag not in head]


def check_table_preamble(text, expr, ports):
    """Определение таблицы ровно одно; перед ним идут «table inet ...» и «delete table inet ...» в этом порядке."""
    raw = [line.strip() for line in text.splitlines()]
    defs = [i for i, line in enumerate(raw) if line == f"table inet {TABLE} {{"]
    if len(defs) != 1:
        return [f"строк «table inet {TABLE} {{» должна быть одна, найдено {len(defs)}"]
    i = defs[0]
    if i < 2 or raw[i - 2] != f"table inet {TABLE}" or raw[i - 1] != f"delete table inet {TABLE}":
        return ["перед определением таблицы должны идти «table inet ...» и «delete table inet ...» в этом порядке"]
    return []


def check_sets(text, expr, ports):
    """Ровно два набора peer4 (ipv4_addr) и peer6 (ipv6_addr), без elements."""
    code = code_text(text)
    problems = []
    if "elements" in code:
        problems.append("в наборах не должно быть elements: при загрузке наборы пусты")
    found = {m.group(1): m.group(2) for m in SET_RE.finditer(code)}
    if sorted(found) != sorted(SET_TYPES):
        problems.append(f"наборы: ожидались {sorted(SET_TYPES)}, найдено {sorted(found)}")
    if len(re.findall(r"\bset\b", code)) != len(SET_TYPES):
        problems.append("лишнее объявление set")
    for name, body in found.items():
        types = re.findall(r"\btype\s+(\S+)", body)
        if types != [SET_TYPES[name]]:
            problems.append(f"set {name}: типы {types}, нужен {SET_TYPES[name]}")
    return problems


def check_chain(text, expr, ports):
    """Сразу после «chain input {» — хук input с приоритетом ровно -10 и policy accept."""
    lines = code_lines(text)
    if "chain input {" not in lines:
        return ["нет строки «chain input {»"]
    nxt = lines[lines.index("chain input {") + 1]
    m = HOOK_RE.match(nxt)
    if not m:
        return [f"после «chain input {{» ожидалась «type filter hook input priority -10; policy accept;», есть: {nxt!r}"]
    if int(m.group(1)) != -10:
        return [f"приоритет хука {m.group(1)}, нужен -10"]
    return []


def check_other_objects(text, expr, ports):
    """Кроме цепочки input — никаких других chain и hook; policy ровно одна и это accept."""
    code = code_text(text)
    problems = []
    chains = re.findall(r"\bchain\s+(\w+)", code)
    hooks = re.findall(r"\bhook\s+(\w+)", code)
    policies = re.findall(r"\bpolicy\s+(\w+)", code)
    if chains != ["input"]:
        problems.append(f"цепочки {chains}, нужна только input")
    if hooks != ["input"]:
        problems.append(f"хуки {hooks}, нужен только input")
    if policies != ["accept"]:
        problems.append(f"policy {policies}, нужна одна с accept")
    return problems


def check_rules(text, expr, ports):
    """Ровно три правила «udp dport <порт> ...»: одно выражение порта, порядок и хвосты как в RULE_TAILS."""
    lines = code_lines(text)
    code = "\n".join(lines)
    rules = [line for line in lines if line.startswith("udp dport")]
    if len(rules) != 3:
        return [f"правил «udp dport» должно быть 3, найдено {len(rules)}"]
    parsed = [RULE_RE.match(r) for r in rules]
    bad = [r for r, m in zip(rules, parsed) if not m]
    if bad:
        return [f"правило не разобрано: {r!r}" for r in bad]
    problems = []
    exprs = [m.group(1) for m in parsed]
    if any(e != expr for e in exprs):
        problems.append(f"выражения порта {exprs}, нужно одно и то же: {expr!r}")
    tails = [m.group(2) for m in parsed]
    if tails != RULE_TAILS:
        problems.append(f"правила {tails}, нужны по порядку {RULE_TAILS}")
    if len(re.findall(r"\bdport\b", code)) != 3:
        problems.append("лишние конструкции dport вне трёх правил")
    return problems


def check_rule_ports(text, expr, ports):
    """Множество портов, взятое из самих правил, равно ожидаемому (console: 7444 и 7445, remote: 7445)."""
    rule_ports = set()
    for line in code_lines(text):
        m = RULE_RE.match(line) if line.startswith("udp dport") else None
        if m:
            rule_ports |= ports_of(m.group(1))
    if rule_ports != ports:
        return [f"порты в правилах {sorted(rule_ports)}, ожидались {sorted(ports)}"]
    return []


def check_forbidden(text, expr, ports):
    """В коде (без комментариев) нет запрещённых конструкций из FORBIDDEN."""
    code = code_text(text)
    return [f"запрещено: {what}" for pattern, what in FORBIDDEN if re.search(pattern, code)]


def check_braces(text, expr, ports):
    """Фигурные скобки сбалансированы и не закрываются раньше времени."""
    depth = 0
    for ch in code_text(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return ["лишняя закрывающая скобка «}»"]
    return [] if depth == 0 else [f"не закрыто скобок «{{»: {depth}"]


class VideoNftStructureTest(unittest.TestCase):
    """Структурные проверки обоих файлов; каждая проверка гоняется по всем файлам через subTest."""

    def _check(self, checker):
        for name, (expr, ports) in FILES.items():
            with self.subTest(file=name):
                text = (NFT_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(checker(text, expr, ports), [])

    def test_spdx_header(self):
        self._check(check_header)

    def test_table_definition_and_idempotent_preamble(self):
        self._check(check_table_preamble)

    def test_sets_empty_and_typed(self):
        self._check(check_sets)

    def test_chain_hook_priority_policy(self):
        self._check(check_chain)

    def test_no_other_chain_hook_or_policy(self):
        self._check(check_other_objects)

    def test_rules_order_port_expr_and_final_counter_drop(self):
        self._check(check_rules)

    def test_rule_ports_set(self):
        self._check(check_rule_ports)

    def test_forbidden_constructs_absent(self):
        self._check(check_forbidden)

    def test_braces_balanced(self):
        self._check(check_braces)

    def test_checkers_catch_broken_variants(self):
        """Самопроверка: искажённые копии remote-файла должны давать ошибки, иначе проверки были бы пустыми."""
        name = "sheregesh-video-remote.nft"
        expr, ports = FILES[name]
        base = (NFT_DIR / name).read_text(encoding="utf-8")
        broken = {
            "drop без счётчика": (base.replace("udp dport 7445 counter drop", "udp dport 7445 drop"), check_rules),
            "приоритет -5": (base.replace("priority -10", "priority -5"), check_chain),
            "ct state в правиле": (base.replace("ip6 saddr", "ct state established ip6 saddr"), check_forbidden),
            "flush ruleset": (base.replace("table inet sheregesh_video\n", "flush ruleset\ntable inet sheregesh_video\n", 1),
                              check_forbidden),
            "elements в наборе": (base.replace("type ipv4_addr", "type ipv4_addr\n\t\telements = { 10.0.0.1 }"),
                                  check_sets),
            "нет delete table": (base.replace("delete table inet sheregesh_video\n", ""), check_table_preamble),
            "лишняя скобка": (base + "}\n", check_braces),
            "порт 7444 у remote": (base.replace("udp dport 7445", "udp dport 7444"), check_forbidden),
        }
        for label, (text, checker) in broken.items():
            with self.subTest(case=label):
                self.assertTrue(checker(text, expr, ports), f"проверка не поймала: {label}")


if __name__ == "__main__":
    unittest.main()
