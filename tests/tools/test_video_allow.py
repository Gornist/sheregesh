# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тесты скрипта tools/image/bin/sheregesh-video-allow (ADR-0016 §3.3, контракт §8).

Скрипт загружается как модуль из файла без расширения .py. Вызов nft подменяется заглушкой (runner), поэтому
реальный nft не запускается. Проверяется: валидный адрес или «none» дают ровно ожидаемую транзакцию для нужного
набора; все недопустимые значения отклоняются кодом 2 до вызова nft; отказ nft или его отсутствие дают код 1;
в исходнике нет shell-вызовов, а единственный subprocess.run передаёт список аргументов; файл исполняемый.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import ipaddress
import os
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/image/bin/sheregesh-video-allow"

# Загрузка модуля из файла без расширения .py (скрипт не является пакетом).
_loader = importlib.machinery.SourceFileLoader("sheregesh_video_allow", str(SCRIPT))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(mod)
main = mod.main

FLUSH = ("flush set inet sheregesh_video peer4\n"
         "flush set inet sheregesh_video peer6\n")

# Значения, которые скрипт обязан отклонить кодом 2 без вызова nft.
INVALID = (
    "", " ", "None", "NONE", "none ", " none",
    "192.168.1.0/24", "10.0.0.0/8", "fe80::/10",
    "1.2.3.4;", "1.2.3.4 ", " 1.2.3.4", "1.2.3.4 5.6.7.8", "1.2.3.4,5.6.7.8",
    "1.2.3.4\n", "1.2.3.4\nflush ruleset",
    "fe80::1%eth0", "fe80::1%", "%", "..", ".", ":", "::",
    "0.0.0.0", "224.0.0.1", "ff02::1",
    "256.1.1.1", "1.2.3", "1.2.3.4.5", "01.2.3.4", "1.2.3.04", "-1.2.3.4",
    "1.2.3.4/32", "1.2.3.4$(id)", "`id`", "$(id)",
    "a.b.c.d", "localhost", "example.com",
    "1.2.3.٤",                  # арабская цифра
    "１.２.３.４",   # полноширинные цифры
    "фе80::1",                       # кириллическая «е»
    "x" * 200, "1" * 100, "f" * 46, ":" * 40,
    "1.2.3.4\x00", "1.2.3.4\t",
    "../etc/passwd", "-h", "--help", "-f /etc/passwd",
)


class FakeNft:
    """Заглушка runner: запоминает переданный текст транзакции и возвращает заданный результат."""

    def __init__(self, returncode=0, stderr="", exc=None):
        self.returncode = returncode
        self.stderr = stderr
        self.exc = exc
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return types.SimpleNamespace(returncode=self.returncode, stderr=self.stderr)


def quiet_main(argv, runner):
    """Запуск main с перехватом stdout/stderr: сообщения об отказе печатаются в stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, runner)
    return code, err.getvalue()


class ValidAddressTest(unittest.TestCase):
    """Валидные значения: точный текст транзакции, один вызов nft, код 0."""

    CASES = (
        ("none", FLUSH),
        ("192.168.1.92",
         FLUSH + "add element inet sheregesh_video peer4 { 192.168.1.92 }\n"),
        ("fe80::1",
         FLUSH + "add element inet sheregesh_video peer6 { fe80::1 }\n"),
        ("FE80:0:0:0:0:0:0:1",  # текст адреса нормализуется
         FLUSH + "add element inet sheregesh_video peer6 { fe80::1 }\n"),
        # Текст для IPv4-mapped адреса берётся из модуля ipaddress: его форма зависит от версии Python.
        ("::ffff:1.2.3.4",
         FLUSH + "add element inet sheregesh_video peer6 { "
         + ipaddress.ip_address("::ffff:1.2.3.4").compressed + " }\n"),
        ("2001:db8::1",
         FLUSH + "add element inet sheregesh_video peer6 { 2001:db8::1 }\n"),
    )

    def test_valid_values_build_exact_transaction(self):
        for arg, expected in self.CASES:
            with self.subTest(arg=arg):
                runner = FakeNft()
                code, _ = quiet_main([arg], runner)
                self.assertEqual(code, 0)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(runner.calls[0], expected)


class InvalidValueTest(unittest.TestCase):
    """Недопустимые значения: код 2, nft не вызывается."""

    def test_invalid_values_rejected_without_calling_nft(self):
        self.assertGreaterEqual(len(INVALID), 35)
        for value in INVALID:
            with self.subTest(value=value):
                runner = FakeNft()
                code, _ = quiet_main([value], runner)
                self.assertEqual(code, 2)
                self.assertEqual(runner.calls, [])


class ArgumentCountTest(unittest.TestCase):
    """Неверное число аргументов: код 2, nft не вызывается."""

    def test_wrong_argument_count_rejected(self):
        for argv in ([], ["a", "b"]):
            with self.subTest(argv=argv):
                runner = FakeNft()
                code, _ = quiet_main(argv, runner)
                self.assertEqual(code, 2)
                self.assertEqual(runner.calls, [])


class NftFailureTest(unittest.TestCase):
    """Сбои nft: отказ возвращает код 1, исключение из runner не выходит наружу."""

    def test_nft_nonzero_exit_returns_1(self):
        runner = FakeNft(returncode=1, stderr="Error: No such file or directory")
        code, err = quiet_main(["192.168.1.92"], runner)
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("No such file or directory", err)

    def test_missing_nft_binary_returns_1(self):
        runner = FakeNft(exc=FileNotFoundError("/usr/sbin/nft"))
        code, err = quiet_main(["none"], runner)
        self.assertEqual(code, 1)
        self.assertIn("FileNotFoundError", err)


class SourceStaticTest(unittest.TestCase):
    """Статическая проверка исходника: без shell и eval, один subprocess.run со списком команд."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_no_shell_or_dynamic_code(self):
        for banned in ("shell=True", "os.system", "eval(", "exec("):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.text)

    def test_single_subprocess_run_with_argument_list(self):
        self.assertEqual(self.text.count("subprocess.run("), 1)
        self.assertIn('[NFT, "-f", "-"]', self.text)


class PermissionsTest(unittest.TestCase):
    """Права и шапка: файл исполняемый, первая строка — интерпретатор /usr/bin/python3."""

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_shebang_line(self):
        with open(SCRIPT, encoding="utf-8") as f:
            first = f.readline().rstrip("\r\n")
        self.assertEqual(first, "#!/usr/bin/python3")


if __name__ == "__main__":
    unittest.main()
