#!/bin/bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Испытание app-stop с настоящим systemd (PID 1 контейнера). Запускается внутри контейнера из host.sh.
# /stage/repo — копия software/sheregesh, тестов ретранслятора и юнита; /stage/harness — этот каталог.
set -u
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
x() { printf '$ %s\n' "$*"; "$@"; }
ok() { if [ "$1" = 0 ]; then PASS=$((PASS+1)); echo "  [PASS] $2"; else FAIL=$((FAIL+1)); echo "  [FAIL] $2"; fi; }
chk() { local d=$1; shift; "$@" >/dev/null 2>&1; ok $? "$d"; }
prop() { systemctl show "$1" -p "$2" --value; }
grp() { ps -eo pid=,pgid=,stat=,args= | awk -v g="$1" '$2==g'; }
unit() { echo "sheregesh-console-$1@$2.service"; }

echo "=== среда"
for _ in $(seq 100); do systemctl is-system-running >/dev/null 2>&1 && break
    [ "$(systemctl is-system-running 2>/dev/null)" = degraded ] && break; sleep 0.2; done
x systemctl is-system-running
echo "$ systemctl --version"; systemctl --version | head -1
x python3 --version
uname -r

echo "=== установка ретранслятора как в пакете (tools/image/build_deb.py WRAPPERS)"
mkdir -p /usr/lib/sheregesh/software /usr/local/lib/appstop
cp -r /stage/repo/software/sheregesh /usr/lib/sheregesh/software/
cat > /usr/bin/sheregesh-notify-relay <<'EOF'
#!/bin/sh
# sheregesh: запуск sheregesh.notify_relay из /usr/lib/sheregesh
export PYTHONPATH=/usr/lib/sheregesh/software:/usr/lib/sheregesh/software/emulators:/usr/lib/sheregesh/software/console:/usr/lib/sheregesh/software/remote
exec /usr/bin/python3 -s -m sheregesh.notify_relay "$@"
EOF
chmod 755 /usr/bin/sheregesh-notify-relay
install -m 755 /stage/harness/fake_cage.sh /usr/local/lib/appstop/fake_cage.sh
install -m 644 /stage/harness/fake_app.py /usr/local/lib/appstop/fake_app.py
x sha256sum /usr/lib/sheregesh/software/sheregesh/notify_relay.py /stage/repo/tools/image/units/sheregesh-console.service
x python3 /stage/harness/make_units.py /stage/repo/tools/image/units/sheregesh-console.service /etc/systemd/system
x cat /etc/systemd/system/sheregesh-console-b@.service
x diff /etc/systemd/system/sheregesh-console-a@.service /etc/systemd/system/sheregesh-console-b@.service
x systemctl daemon-reload

echo "=== модульные тесты ретранслятора на Linux"
(cd /stage/repo && PYTHONPATH=/stage/repo/software x python3 -m pytest -q -p no:cacheprovider --noconftest -rs \
    tests/unit/test_notify_relay_stop.py tests/unit/test_notify_relay.py 2>&1 | tail -5)

start() {   # вариант режим [norestart]
    local u; u=$(unit "$1" "$2")
    mkdir -p -m 1777 /run/appstop; rm -f "/run/appstop/$2.pids"
    rm -rf "/run/systemd/system/$u.d"
    if [ "${3:-}" = norestart ]; then
        mkdir -p "/run/systemd/system/$u.d"
        printf '[Service]\nRestart=no\n' > "/run/systemd/system/$u.d/norestart.conf"
    fi
    systemctl daemon-reload; systemctl reset-failed "$u" 2>/dev/null
    sleep 1; T=$(date +%s)                             # строки прошлого случая — раньше T
    local t0=$SECONDS
    x systemctl start "$u"; ok $? "$u: start (Type=notify, READY от приложения через ретранслятор) за $((SECONDS-t0)) с"
    for _ in $(seq 50); do [ -s "/run/appstop/$2.pids" ] && break; sleep 0.1; done
    read -r CAGE APP < "/run/appstop/$2.pids"
    MAIN=$(prop "$u" MainPID)
    echo "  MainPID=$MAIN ($(tr '\0' ' ' < /proc/$MAIN/cmdline | cut -c1-80)) cgroup=$(cat /proc/$MAIN/cgroup)"
    echo "  cage=$CAGE app=$APP pgid=$(ps -o pgid= -p "$CAGE" | tr -d ' ') cgroup cage=$(cat /proc/$CAGE/cgroup)"
    chk "$u: группа процессов cage непуста" test -n "$(grp "$CAGE")"
}

journal() {   # юнит, ретранслятор и ExecStopPost с момента T. При PAMName процессы в session-N.scope, их строки
    # не попадают под -u — нужны SYSLOG_IDENTIFIER (так же и на плате). «-u X + …» journalctl 257 не принимает.
    journalctl --since "@$T" --no-pager -o short-monotonic _SYSTEMD_UNIT="$1" + UNIT="$1" \
        + SYSLOG_IDENTIFIER=sheregesh-notify-relay + SYSLOG_IDENTIFIER=sh | tee /tmp/j.txt
}

after() {   # юнит ожидаемые: ActiveState Result ExecMainStatus
    local u=$1
    x systemctl show "$u" -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus -p NRestarts
    chk "$u: ActiveState=$2" test "$(prop "$u" ActiveState)" = "$2"
    chk "$u: Result=$3" test "$(prop "$u" Result)" = "$3"
    chk "$u: ExecMainStatus=$4" test "$(prop "$u" ExecMainStatus)" = "$4"
    sleep 0.5
    local left; left=$(grp "$CAGE")
    [ -n "$left" ] && echo "  остались в группе $CAGE: $left"
    chk "$u: процессов группы $CAGE нет" test -z "$left"
    journal "$u"
}

wait_state() {   # юнит состояние тайм-аут
    for _ in $(seq $(( $3 * 10 ))); do [ "$(prop "$1" ActiveState)" = "$2" ] && return 0; sleep 0.1; done; return 1
}

stop_case() {   # вариант режим мин_с макс_с
    local u; u=$(unit "$1" "$2"); echo; echo "=== [$1] штатный останов, режим $2 (Restart=always, как в юните)"
    start "$1" "$2"
    if [ "$2" = term ]; then
        echo "  ждём 25 с > WatchdogSec=20: WATCHDOG приложения пересылается"; sleep 25
        chk "$u: активен после 25 с, NRestarts=0" test "$(prop "$u" ActiveState)/$(prop "$u" NRestarts)" = active/0
    fi
    [ "$2" = frozen ] && x kill -STOP "$APP" && ps -o pid=,stat=,args= -p "$APP"
    local t0; t0=$(date +%s.%N)
    x systemctl stop "$u"
    local dt; dt=$(python3 -c "import time; print(round(time.time() - $t0, 2))")
    echo "  systemctl stop: $dt с"
    chk "$u: останов за $3..$4 с (TimeoutStopSec=$(prop "$u" TimeoutStopUSec))" \
        python3 -c "import sys; sys.exit(not ($3 <= $dt < $4))"
    after "$u" inactive success 0
    chk "$u: ExecStopPost: result=success code=exited status=0" grep -q 'result=success code=exited status=0' /tmp/j.txt
    chk "$u: нет 'Failed with result' и status=143" sh -c "! grep -qE 'Failed with result|status=143' /tmp/j.txt"
    [ "$2" != term ] && chk "$u: ретранслятор добил группу SIGKILL после GRACE_S" grep -q 'still alive after 5 s' /tmp/j.txt
}

fail_case() {   # вариант режим
    local u; u=$(unit "$1" "$2"); echo; echo "=== [$1] отказ, режим $2 (Restart=no — наблюдаем failed)"
    start "$1" "$2" norestart
    case $2 in
        term) x systemctl kill --kill-whom=main -s SIGABRT "$u"; wait_state "$u" failed 20
              after "$u" failed exit-code 134 ;;
        hang) echo "  приложение молчит; ждём срабатывания watchdog (WatchdogSec=20)"; wait_state "$u" failed 45
              after "$u" failed watchdog 134
              chk "$u: журнал: Watchdog timeout" grep -q 'Watchdog timeout' /tmp/j.txt ;;
        crash) wait_state "$u" failed 20; after "$u" failed exit-code 70 ;;
    esac
    chk "$u: журнал: Failed with result" grep -q 'Failed with result' /tmp/j.txt
}

hang_restart() {   # вариант: watchdog при Restart=always — отказ и перезапуск
    local u; u=$(unit "$1" hang); echo; echo "=== [$1] watchdog при Restart=always (как в юните)"
    start "$1" hang
    for _ in $(seq 450); do [ "$(prop "$u" NRestarts)" -ge 1 ] && break; sleep 0.1; done
    journal "$u" >/dev/null
    chk "$u: Failed with result 'watchdog'" grep -q "Failed with result 'watchdog'" /tmp/j.txt
    chk "$u: перезапущен (NRestarts>=1)" test "$(prop "$u" NRestarts)" -ge 1
    chk "$u: процессов прежней группы $CAGE нет" test -z "$(grp "$CAGE")"
    x systemctl stop "$u"; systemctl reset-failed "$u" 2>/dev/null
}

for v in a b; do
    stop_case "$v" term 0 3
    stop_case "$v" ignore 4.9 7
    stop_case "$v" frozen 4.9 7
    fail_case "$v" term
    fail_case "$v" hang
    fail_case "$v" crash
    hang_restart "$v"
done

echo
echo "=== ИТОГ: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
