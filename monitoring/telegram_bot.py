"""
Notifikasi gangguan jaringan ke bot Telegram.

Alur kerja:
  1. monitor.py memanggil process_alerts() setiap selesai satu siklus
     pengecekan.
  2. Perangkat yang OFFLINE (terputus / packet loss besar) selama
     OFFLINE_CONFIRM siklus berturut-turut dianggap benar-benar
     terganggu -> pesan dikirim ke SEMUA akun Telegram terdaftar.
  3. Selama perangkat masih terputus, pesan "MASIH TERPUTUS" dikirim
     ulang otomatis setiap REPEAT_INTERVAL detik (default 30 detik).
  4. Saat perangkat kembali normal, dikirim pesan "pulih".

Pengiriman dilakukan di thread terpisah supaya loop monitoring tidak
tertahan bila Telegram lambat / tidak bisa dijangkau.

Hanya memakai pustaka standar Python (tanpa dependensi tambahan).
"""

import html
import json
import os
import threading
import time
import urllib.error
import urllib.request

from .db import current_time, get_db, get_setting

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")

REQUEST_TIMEOUT = 10

# Jumlah pengecekan OFFLINE berturut-turut sebelum notifikasi dikirim.
# Mencegah alarm palsu akibat satu kali ping gagal.
OFFLINE_CONFIRM = 2

# Selama perangkat masih terputus, kirim pesan pengingat otomatis tiap
# sekian detik. Isi 0 untuk mematikan (pesan hanya dikirim sekali).
REPEAT_INTERVAL = 30

# Siklus monitoring butuh waktu (ping + jeda), jadi pengingat dikirim
# sedikit lebih awal agar jatuh di sekitar REPEAT_INTERVAL.
REPEAT_SLACK = 6

# Jeda sebelum mencoba kirim ulang bila seluruh pengiriman gagal (detik)
RETRY_DELAY = 60

# Batas jumlah perangkat yang dirinci dalam satu pesan
MAX_LISTED = 25


# =========================================================
# Pengaturan
# =========================================================

def get_token():
    """Token bot: variabel lingkungan diutamakan, lalu pengaturan DB."""

    return (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or get_setting("telegram_token", "")
        or ""
    ).strip()


def mask_token(token):
    if not token:
        return ""

    if len(token) <= 12:
        return "*" * len(token)

    return f"{token[:6]}…{token[-4:]}"


def notify_enabled():
    return get_setting("telegram_notify", "1") == "1"


def get_accounts():
    connection = get_db()

    rows = connection.execute(
        "SELECT id, name, chat_id FROM telegram_accounts ORDER BY id"
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_technicians():
    connection = get_db()

    rows = connection.execute(
        "SELECT id, name, contact FROM technicians ORDER BY id"
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


# =========================================================
# Panggilan ke Telegram Bot API
# =========================================================

def _call(token, method, payload=None):
    """
    Panggil satu method Bot API.

    Kembalian: (ok, data, error)
      ok    -> True bila Telegram membalas ok
      data  -> field "result" dari Telegram
      error -> teks kesalahan yang bisa ditampilkan ke pengguna
    """

    url = f"{API_BASE}/bot{token}/{method}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT
        ) as response:
            body = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            description = body.get("description") or str(error)
        except Exception:
            description = str(error)

        return False, None, description

    except Exception as error:
        return False, None, f"Tidak dapat menghubungi Telegram: {error}"

    if not body.get("ok"):
        return False, None, body.get("description", "Telegram menolak permintaan.")

    return True, body.get("result"), None


def validate_token(token):
    """
    Cek token lewat getMe.

    Kembalian: (status, info)
      "ok"       -> info = username bot
      "invalid"  -> info = pesan dari Telegram (token ditolak)
      "network"  -> info = pesan kesalahan jaringan (belum bisa dipastikan)
    """

    ok, result, error = _call(token, "getMe")

    if ok:
        return "ok", result.get("username", "")

    if error and error.startswith("Tidak dapat menghubungi"):
        return "network", error

    return "invalid", error


def send_message(chat_id, text, token=None):
    """Kirim satu pesan ke satu chat. Kembalian: (ok, error)."""

    token = token or get_token()

    if not token:
        return False, "Token bot belum diisi."

    ok, _, error = _call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )

    return ok, error


def broadcast(text):
    """Kirim pesan ke semua akun terdaftar. Kembalian: jumlah sukses."""

    token = get_token()
    delivered = 0

    for account in get_accounts():
        ok, error = send_message(account["chat_id"], text, token)

        if ok:
            delivered += 1
        else:
            print(
                f"[TELEGRAM] gagal kirim ke {account['name']} "
                f"({account['chat_id']}): {error}"
            )

    return delivered


# =========================================================
# Format pesan
# =========================================================

def _e(value):
    return html.escape(str(value if value is not None else "-"))


def _technician_block():
    technicians = get_technicians()

    if not technicians:
        return ""

    lines = [
        f"• {_e(t['name'])} — <code>{_e(t['contact'])}</code>"
        for t in technicians
    ]

    return "\n\n👷 <b>Hubungi teknisi:</b>\n" + "\n".join(lines)


def _place(device):
    parts = [
        str(device[key]).strip()
        for key in ("building", "floor", "room")
        if device[key]
    ]

    return ", ".join(parts)


def _format_duration(seconds):
    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds} detik"

    minutes, seconds = divmod(seconds, 60)

    if minutes < 60:
        return f"{minutes} menit"

    hours, minutes = divmod(minutes, 60)

    if hours < 24:
        return f"{hours} jam {minutes} menit"

    days, hours = divmod(hours, 24)

    return f"{days} hari {hours} jam"


def build_down_message(devices, reminder=False, since=None):
    """
    Pesan gangguan untuk satu atau beberapa perangkat.

    reminder -> pengingat berkala (perangkat masih terputus)
    since    -> dict device_id -> epoch mulai gangguan (untuk durasi)
    """

    now = current_time()
    since = since or {}
    title = "MASIH TERPUTUS" if reminder else "GANGGUAN JARINGAN"

    if len(devices) == 1:
        device = devices[0]
        place = _place(device)

        lines = [
            f"🔴 <b>{title}</b>",
            "",
            f"📍 <b>Label/Ruangan:</b> {_e(device['location'])}",
            f"🌐 <b>IP:</b> <code>{_e(device['ip_address'])}</code>",
        ]

        if place:
            lines.append(f"🏢 <b>Lokasi:</b> {_e(place)}")

        loss = device["packet_loss"]
        lines.append(
            "📉 <b>Packet loss:</b> "
            f"{_e(round(loss) if loss is not None else 100)}%"
        )
        lines.append("⚠️ <b>Status:</b> TERPUTUS")

        if reminder and device["id"] in since:
            lines.append(
                "⏱ <b>Sudah terputus:</b> "
                f"{_format_duration(time.time() - since[device['id']])}"
            )

        if device["cause_title"]:
            lines.append(f"🔎 <b>Dugaan:</b> {_e(device['cause_title'])}")

        lines.append(f"🕒 {_e(now)}")

        return "\n".join(lines) + _technician_block()

    lines = [
        f"🔴 <b>{title} — {len(devices)} perangkat terputus</b>",
        "",
    ]

    for device in devices[:MAX_LISTED]:
        place = _place(device)
        entry = (
            f"• <b>{_e(device['location'])}</b> "
            f"(<code>{_e(device['ip_address'])}</code>)"
        )

        if place:
            entry += f" — {_e(place)}"

        if reminder and device["id"] in since:
            entry += (
                " — sudah "
                f"{_format_duration(time.time() - since[device['id']])}"
            )

        lines.append(entry)

    if len(devices) > MAX_LISTED:
        lines.append(f"… dan {len(devices) - MAX_LISTED} perangkat lainnya")

    lines += ["", f"🕒 {_e(now)}"]

    return "\n".join(lines) + _technician_block()


def build_recovery_message(items):
    """
    Pesan pulih. items = list of (device, durasi_detik).
    """

    now = current_time()

    if len(items) == 1:
        device, seconds = items[0]
        place = _place(device)

        lines = [
            "🟢 <b>JARINGAN PULIH</b>",
            "",
            f"📍 <b>Label/Ruangan:</b> {_e(device['location'])}",
            f"🌐 <b>IP:</b> <code>{_e(device['ip_address'])}</code>",
        ]

        if place:
            lines.append(f"🏢 <b>Lokasi:</b> {_e(place)}")

        lines.append(
            f"⏱ <b>Lama gangguan:</b> {_format_duration(seconds)}"
        )
        lines.append(f"🕒 {_e(now)}")

        return "\n".join(lines)

    lines = [
        f"🟢 <b>JARINGAN PULIH — {len(items)} perangkat kembali terhubung</b>",
        "",
    ]

    for device, seconds in items[:MAX_LISTED]:
        lines.append(
            f"• <b>{_e(device['location'])}</b> "
            f"(<code>{_e(device['ip_address'])}</code>) — "
            f"gangguan {_format_duration(seconds)}"
        )

    if len(items) > MAX_LISTED:
        lines.append(f"… dan {len(items) - MAX_LISTED} perangkat lainnya")

    lines += ["", f"🕒 {_e(now)}"]

    return "\n".join(lines)


def build_test_message(account_name):
    return (
        "✅ <b>Tes notifikasi</b>\n\n"
        f"Halo {_e(account_name)}, akun ini sudah terhubung ke "
        "Network Monitoring Diskominfo dan akan menerima "
        "pemberitahuan gangguan jaringan.\n\n"
        f"🕒 {_e(current_time())}"
    )


# =========================================================
# Pelacakan status (dipanggil dari loop monitoring)
# =========================================================

_lock = threading.Lock()

_streak = {}        # device_id -> jumlah cek OFFLINE berturut-turut
_alerted = {}       # device_id -> waktu (epoch) pesan terakhir terkirim
_down_since = {}    # device_id -> waktu (epoch) gangguan pertama diberitahukan
_in_flight = set()  # device_id yang pesannya sedang dikirim
_retry_after = 0.0  # epoch; jangan kirim sebelum waktu ini


def _can_send():
    return bool(notify_enabled() and get_token() and get_accounts())


def _deliver_down(devices, reminder=False):
    global _retry_after

    delivered = 0

    try:
        with _lock:
            since = dict(_down_since)

        delivered = broadcast(
            build_down_message(devices, reminder=reminder, since=since)
        )
    except Exception as error:
        print(f"[TELEGRAM ERROR] {error}")

    with _lock:
        for device in devices:
            _in_flight.discard(device["id"])

        if delivered:
            stamp = time.time()

            for device in devices:
                _alerted[device["id"]] = stamp
                _down_since.setdefault(device["id"], stamp)
        else:
            _retry_after = time.time() + RETRY_DELAY


def _deliver_recovery(items):
    global _retry_after

    delivered = 0

    try:
        delivered = broadcast(build_recovery_message(items))
    except Exception as error:
        print(f"[TELEGRAM ERROR] {error}")

    with _lock:
        for device, _ in items:
            _in_flight.discard(device["id"])

            if delivered:
                _alerted.pop(device["id"], None)
                _down_since.pop(device["id"], None)

        if not delivered:
            _retry_after = time.time() + RETRY_DELAY


def process_alerts():
    """
    Bandingkan status terbaru semua perangkat, lalu kirim notifikasi
    untuk perangkat yang baru terputus atau baru pulih.
    """

    connection = get_db()

    devices = connection.execute("SELECT * FROM devices").fetchall()

    connection.close()

    known_ids = {device["id"] for device in devices}

    newly_down = []
    repeating = []
    recovered = []
    now_ts = time.time()

    with _lock:
        # Bersihkan state perangkat yang sudah dihapus
        for store in (_streak, _alerted, _down_since):
            for stale in [i for i in store if i not in known_ids]:
                store.pop(stale, None)

        for device in devices:
            device_id = device["id"]

            if device["status"] == "OFFLINE":
                _streak[device_id] = _streak.get(device_id, 0) + 1
            else:
                _streak[device_id] = 0

            if device_id in _in_flight:
                continue

            if (
                device["status"] == "OFFLINE"
                and _streak[device_id] >= OFFLINE_CONFIRM
                and device_id not in _alerted
            ):
                newly_down.append(device)

            elif (
                device["status"] == "OFFLINE"
                and device_id in _alerted
                and REPEAT_INTERVAL > 0
                and now_ts - _alerted[device_id]
                >= REPEAT_INTERVAL - REPEAT_SLACK
            ):
                repeating.append(device)

            elif (
                device_id in _alerted
                and device["status"] in ("ONLINE", "SLOW")
            ):
                recovered.append(
                    (
                        device,
                        now_ts - _down_since.get(
                            device_id, _alerted[device_id]
                        ),
                    )
                )

    if not newly_down and not repeating and not recovered:
        return

    if not _can_send():
        # Tidak ada tujuan pengiriman. Perangkat yang sudah pulih
        # cukup dibersihkan dari daftar tanpa pesan.
        with _lock:
            for device, _ in recovered:
                _alerted.pop(device["id"], None)
                _down_since.pop(device["id"], None)

        return

    if time.time() < _retry_after:
        return

    if newly_down:
        with _lock:
            for device in newly_down:
                _in_flight.add(device["id"])

        threading.Thread(
            target=_deliver_down, args=(newly_down,), daemon=True
        ).start()

    if repeating:
        with _lock:
            for device in repeating:
                _in_flight.add(device["id"])

        threading.Thread(
            target=_deliver_down, args=(repeating, True), daemon=True
        ).start()

    if recovered:
        with _lock:
            for device, _ in recovered:
                _in_flight.add(device["id"])

        threading.Thread(
            target=_deliver_recovery, args=(recovered,), daemon=True
        ).start()
