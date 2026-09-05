"""Local browser regression: run Vite, then set TRA_TEST_FRONTEND_URL for pytest."""

import json
import os
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest


@pytest.mark.skipif(not os.getenv("TRA_TEST_FRONTEND_URL"), reason="requires local Vite + Chromium")
def test_round_end_and_stale_train_selection():
    from playwright.sync_api import sync_playwright

    task = {
        "id": "audit-task", "status": "waiting_human", "route": "竹北 → 大甲",
        "ride_date": "2026/09/07", "order_type": "BY_TRAIN_NO", "train_label": "自強 110",
        "mode": "book_when_available", "poll_interval_seconds": 300,
        "monitor_until": None, "next_check_at": None, "last_checked_at": None,
        "last_error": None, "booking_code": None, "availability_note": "unknown",
        **{key: "2026-09-06T00:00:00Z" for key in (
            "scheduled_at", "monitor_start_at", "created_at", "updated_at")},
    }
    candidate = {
        "train_no": "110", "train_type_name": "自強", "departure_time": "08:00",
        "arrival_time": "10:00", "duration_minutes": 120, "is_reserved_type": True,
        "seat_type_label": "對號列車", "note": "", "in_requested_window": True,
    }
    suggestions = {"primary": [candidate], "alternatives": [], "transfers": [],
                   "availability_known": False}

    def respond(route):
        url = urlsplit(route.request.url)
        path = url.path.removeprefix('/api')
        responses = {
            '/auth/me': {"id": 1, "email": "audit@example.com"},
            '/profile': {"identity": "", "member_account": "", "has_member_password": False},
            '/stations': [{"value": f"{code}-{name}", "label": name, "county": county}
                          for code, name, county in [('1180', '竹北', '新竹縣'),
                                                     ('2200', '大甲', '臺中市')]],
            '/times': [f'{h:02d}:{m:02d}' for h in range(24) for m in (0, 30)] + ['23:59'],
            '/travelers': [{"id": 1, "label": "測試", "identity": "TEST"}],
            '/tasks': [task],
            '/suggestions': suggestions,
            '/tasks/audit-task/suggestions': suggestions,
            '/tasks/audit-task/booking-session': {
                "task_id": task['id'], "session_url": '/booking-session/old-token/',
                "expires_at": '2026-09-06T01:00:00Z', "notice": 'test handoff'},
        }
        if path.endswith('/booking-result'):
            assert route.request.headers['x-booking-session'] == 'old-token'
            data = {"task_id": task['id'], "status": "ended", "booking_code": None,
                    "message": "本輪已結束，下一輪請重新開啟。"}
        else:
            assert path in responses, path
            data = responses[path]
        route.fulfill(content_type='application/json', body=json.dumps(data))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale='zh-TW')
        try:
            page.add_init_script("localStorage.setItem('tra-sniper-token', 'local-test')")
            page.route('**/api/**', respond)
            page.route('**/booking-session/old-token/**', lambda route: route.fulfill(body='VNC'))
            page.goto(os.environ['TRA_TEST_FRONTEND_URL'])
            page.get_by_role('button', name='開啟驗證畫面', exact=True).click()
            dialog = page.get_by_role('dialog')
            dialog.get_by_role('button', name='關閉', exact=True).wait_for(timeout=8000)
            assert dialog.locator('iframe').count() == 0
            dialog.get_by_role('button', name='關閉', exact=True).click()
            page.get_by_role('button', name='① 查詢車次建議').click()
            page.get_by_role('button', name='② 改用此車次').first.click()
            submit = page.get_by_role('button', name='加入任務佇列')
            assert submit.is_enabled()
            page.get_by_label('乘車日期', exact=True).fill('2026-09-25')
            assert submit.is_disabled()
            assert page.get_by_text('將鎖定', exact=False).count() == 0
            page.get_by_role('button', name='① 查詢車次建議').click()
            page.get_by_role('button', name='② 改用此車次').first.click()
            assert submit.is_enabled()
            page.get_by_role('button', name='交換出發與抵達', exact=True).click()
            assert submit.is_disabled()
            page.get_by_role('radio', name='直接輸入車次').check()
            page.get_by_label('車次', exact=True).fill('110')
            page.get_by_label('乘車日期', exact=True).fill('2026-09-26')
            assert submit.is_enabled()  # Explicit manual entry is still supported.
        finally:
            browser.close()


@pytest.mark.skipif(not os.getenv("TRA_TEST_FRONTEND_URL"), reason="requires local Chromium")
def test_reset_waits_for_a_new_browser_process(tmp_path):
    from playwright.sync_api import sync_playwright

    from tra_sniper.automation import TRCBookingAutomator

    with sync_playwright() as p:
        executable = p.chromium.executable_path
        probe = p.chromium.launch(headless=True)
        probe.close()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    command = [executable, '--headless', '--no-sandbox', f'--remote-debugging-port={port}',
               f'--user-data-dir={tmp_path / "profile"}', 'about:blank']

    def launch():
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    processes = [launch()]
    stop = threading.Event()

    def supervise():
        processes[0].wait()
        if not stop.is_set():
            processes.append(launch())  # Same restart contract as the sidecar.

    supervisor = threading.Thread(target=supervise)
    supervisor.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(url + '/json/version', timeout=1) as response:
                    original = json.load(response)['webSocketDebuggerUrl']
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.1)
        TRCBookingAutomator(cdp_url=url).reset_browser()
        with urlopen(url + '/json/version', timeout=1) as response:
            replacement = json.load(response)['webSocketDebuggerUrl']
        assert original != replacement
        assert processes[0].poll() is not None
        assert len(processes) == 2 and processes[1].poll() is None
    finally:
        stop.set()
        if processes[0].poll() is None:
            processes[0].terminate()
        supervisor.join(3)
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
