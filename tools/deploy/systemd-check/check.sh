#!/bin/bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Сценарий внутри контейнера (root): действия sheregesh-ui через polkit, установщик, launcher, откат.
# Пакеты в /opt/check: good1, good2 (рабочие), bad (пульт падает при старте), crash (READY, затем падение),
# foreign (подпись чужим ключом).
set -u
PASS=0; FAIL=0
ok() { echo "PASS $*"; PASS=$((PASS + 1)); }
ko() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }
check() { local name=$1; shift; if "$@"; then ok "$name"; else ko "$name"; fi; }
ui() { runuser -u sheregesh-ui -- "$@"; }
# Действия установщика: от sheregesh-ui через polkit, если в контейнере есть polkitd и D-Bus; иначе от root
# (тот же юнит и тот же установщик; правило polkit тогда проверяет tests/tools/test_polkit_rule.py).
if command -v pkaction >/dev/null && systemctl is-active -q dbus polkit; then POLKIT=1; else POLKIT=0; fi
act() { if [ "$POLKIT" = 1 ]; then ui systemctl start "sheregesh-deploy@$1.service"; else systemctl start "sheregesh-deploy@$1.service"; fi; }
INC=/home/sheregesh-ui/.cache/sheregesh-deploy/incoming
STATE=/var/lib/sheregesh-deploy/state.json
id_of() { cat "/opt/check/$1.id"; }
field() { python3 -c "import json,sys; d=json.load(open('$STATE')); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
push() { ui mkdir -p "$INC"; ui cp "/opt/check/$1.tar.xz" "$INC/bundle.tar.xz"; ui cp "/opt/check/$1.tar.xz.sig" "$INC/bundle.tar.xz.sig"; }
release_of() {   # SHEREGESH_RELEASE в окружении главного процесса службы (пусто — заводской код)
  local pid; pid=$(systemctl show -p MainPID --value "$1")
  [ "$pid" != 0 ] && tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^SHEREGESH_RELEASE=//p'
}
console_release() { local a b; a=$(release_of sheregesh-console); b=$(release_of sheregesh-remote-emu)
  [ "$a" = "$b" ] && echo "$a" || echo "РАЗНЫЕ:$a/$b"; }
wait_active() {
  for _ in $(seq 1 90); do
    [ "$(systemctl show -p ActiveState --value sheregesh-console)" = active ] &&
      [ "$(systemctl show -p ActiveState --value sheregesh-remote-emu)" = active ] && return 0; sleep 1
  done; return 1
}

systemctl is-system-running --wait >/dev/null 2>&1 || true
echo "== systemd $(systemctl --version | head -1 | cut -d' ' -f2), polkit $(pkaction --version 2>/dev/null | awk '{print $NF}')"
check "службы стартуют из заводского кода (/usr) и выходят в READY" wait_active
check "до установки launcher выбирает заводской код" test -z "$(console_release)"

# --- polkit: только start трёх экземпляров установщика
if [ "$POLKIT" = 1 ]; then
  check "polkit: sheregesh-ui не может запустить иной экземпляр (sheregesh-deploy@shell)" \
    bash -c "! runuser -u sheregesh-ui -- systemctl start sheregesh-deploy@shell.service 2>/dev/null"
  check "polkit: sheregesh-ui не может остановить установщик (verb stop)" \
    bash -c "! runuser -u sheregesh-ui -- systemctl stop sheregesh-deploy@install.service 2>/dev/null"
  check "polkit: sheregesh-ui не может перезапустить чужую службу (dbus)" \
    bash -c "! runuser -u sheregesh-ui -- systemctl restart dbus.service 2>/dev/null"
else
  echo "SKIP polkit: в контейнере нет polkitd/D-Bus — действия от root; правило проверяет test_polkit_rule.py"
fi
check "иной экземпляр установщика отвергается самим установщиком" bash -c "! systemctl start sheregesh-deploy@shell.service 2>/dev/null"

# --- установка good1
push good1
act install; rc=$?
check "install good1: systemctl start успешен" test "$rc" = 0
check "install good1: итог OK" test "$(field "d['last']['result']")" = OK
check "install good1: current → releases/$(id_of good1)" test "$(readlink /var/lib/sheregesh-deploy/current)" = "releases/$(id_of good1)"
check "install good1: пульт работает из выпуска (SHEREGESH_RELEASE)" test "$(console_release)" = "$(id_of good1)"
check "выпуск принадлежит root, sheregesh-ui не может его менять" \
  bash -c "! runuser -u sheregesh-ui -- touch /var/lib/sheregesh-deploy/releases/$(id_of good1)/software/x 2>/dev/null"
check "входящие удалены после установки" test ! -e "$INC/bundle.tar.xz"
check "байт-код собран установщиком" bash -c "ls /var/lib/sheregesh-deploy/releases/$(id_of good1)/software/sheregesh/__pycache__/*.pyc >/dev/null"

# --- good2, затем плохой выпуск → автоматический возврат
push good2
act install
check "install good2: OK, previous = good1" test "$(field "d['last']['result'] + ' ' + str(d['previous'])")" = "OK $(id_of good1)"
push bad
act install; rc=$?
check "install bad: код возврата ненулевой" test "$rc" != 0
check "install bad: итог ROLLED_BACK" test "$(field "d['last']['result']")" = ROLLED_BACK
check "install bad: current снова good2" test "$(readlink /var/lib/sheregesh-deploy/current)" = "releases/$(id_of good2)"
wait_active
check "install bad: пульт снова работает из good2" test "$(console_release)" = "$(id_of good2)"
echo "   причина: $(field "d['last']['units']")"
push crash
act install; rc=$?
check "install crash (READY, затем падение в окне наблюдения): ROLLED_BACK" \
  test "$rc" != 0 -a "$(field "d['last']['result']")" = ROLLED_BACK
wait_active
check "install crash: пульт снова работает из good2" test "$(console_release)" = "$(id_of good2)"
echo "   причина: $(field "d['last']['units']")"

# --- чужая подпись и подмена
push foreign
act install
check "чужая подпись: REJECTED, службы не перезапускались" test "$(field "d['last']['result']")" = REJECTED
push good1
ui bash -c "printf 'x' >> $INC/bundle.tar.xz"
act install
check "изменённый пакет: REJECTED" test "$(field "d['last']['result']")" = REJECTED
check "после отказов current не изменился" test "$(readlink /var/lib/sheregesh-deploy/current)" = "releases/$(id_of good2)"

# --- rollback и factory
act rollback
check "rollback: current → good1" test "$(readlink /var/lib/sheregesh-deploy/current)" = "releases/$(id_of good1)"
wait_active
check "rollback: пульт из good1" test "$(console_release)" = "$(id_of good1)"
act factory
check "factory: current снят" test ! -L /var/lib/sheregesh-deploy/current
wait_active
check "factory: пульт из заводского кода" test -z "$(console_release)"

# --- битый выпуск на диске → launcher уходит на заводской код
act rollback
check "rollback после factory: снова good1" test "$(readlink /var/lib/sheregesh-deploy/current)" = "releases/$(id_of good1)"
echo "corrupt = True" >> "/var/lib/sheregesh-deploy/releases/$(id_of good1)/software/sheregesh/__init__.py"
systemctl restart sheregesh-console sheregesh-remote-emu; wait_active
check "повреждённый выпуск: launcher выбирает заводской код" test -z "$(console_release)"
check "launcher пишет причину в журнал" bash -c "journalctl -u sheregesh-console --no-pager | grep -q 'sheregesh-launcher: sheregesh_remote: заводской код: выпуск отвергнут'"

echo "== status (sheregesh-deployd status от sheregesh-ui)"
ui sheregesh-deployd status | head -20
echo "== итог: PASS $PASS, FAIL $FAIL"
[ "$FAIL" = 0 ]
