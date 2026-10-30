<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Сырые доказательства приёмок — архив, не git

С 2026-10-30 в `docs/acceptance` хранятся только отчёты `*.md`. Журналы, JSON, снимки экрана, PNG, скрипты прогонов
и прочие сырые данные — в архиве на devbox. Решение владельца: репозиторий публичный, сырые данные раздували его
(44 МБ в дереве, 39 МБ истории); история переписана (`git filter-repo`), сырые файлы удалены из всех коммитов.

| Что | Где (devbox) | SHA-256 |
|---|---|---|
| Сырые данные `docs/acceptance` на 2026-10-30 (1608 файлов) | `~/archive/sheregesh-2026-10-30/acceptance-evidence-20261030.tar.gz` | `85c1f69d9d772319ae868a9ba96d948cafa0b4abe4311a1811751da2b49b8d19` |
| Полный репозиторий до чистки (все ветки, теги, `git bundle --all`) | `~/archive/sheregesh-2026-10-30/sheregesh-full-20261030.bundle` | `4e188c18755846e4f8b12bc47a48e0d52071a53e1aba7929b98d80c545a77bb2` |

Ссылки отчётов на файлы, кроме `*.md`, указывают в архив: тот же путь внутри `acceptance-evidence-20261030.tar.gz`.
Хеши коммитов в отчётах — до чистки; новые — `docs/development/history-rewrite-2026-10-30.map`.

**Новые приёмки:** сырые данные пишутся в каталог прогона как прежде (git их не видит — `.gitignore`), затем
`rsync -a docs/acceptance/<прогон>/ devbox-lan:archive/acceptance/<прогон>/`; в отчёт — путь и SHA-256 архива.
