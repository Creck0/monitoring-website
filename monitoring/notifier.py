"""
Notifikasi Telegram (push) saat status perangkat berubah.

Cara kerja:
  * Setiap siklus monitoring, hasil status tiap perangkat dikirim ke
    process_cycle().
  * Perubahan status baru dianggap sah bila bertahan selama
    CONFIRM_CHECKS pemeriksaan berturut-turut, supaya satu kali
    ping gagal tidak membanjiri chat.
  * Pesan dimasukkan ke antrean dan dikirim satu per satu di thread
    terpisah sehingga monitoring tidak ikut tertahan.

Token bot disimpan di tabel settings (atau diambil dari variabel
lingkungan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID bila belum diisi).
"""
import html
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .contacts import list_technicians
from .db import current_time

API_URL = "https://api.telegram.org/bot{token}/{method}"
REQUEST_TIMEOUT = 10

# Jumlah pemeriksaan berturut-turut sebelum perubahan status diumumkan
CONFIRM_CHECKS = 2

KNOWN_STATUSES = ("ONLINE", "SLOW", "OFFLINE")

# Bila lebih dari ini perangkat berubah status dalam satu siklus,
# kirim satu pesan ringkasan (mencegah banjir pesan).
SUMMARY_THRESHOLD = 5

TOKEN_PATTERN = re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", re.ASCII)
CHAT_ID_PATTERN = re.compile(
    r"-?[0-9]{3,}|@[A-Za-z][A-Za-z0-9_]{4,}", re.ASCII
)

TOKEN_ERROR = (
    "Format token bot tidak valid. Contoh: "
    "123456789:AAExampleTokenFromBotFather"
)
CHAT_ID_ERROR = (
    "Chat ID tidak valid. Isi dengan angka (boleh diawali tanda minus "
    "untuk grup) atau @username kanal."
)


# =========================================================
# PENGATURAN
# =========================================================

def get_setting(connection, key, default=""):
    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(connection, key, value):
    connection.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def record_result(ok, message):
    """Simpan hasil pengiriman terakhir agar terlihat di dashboard."""
    from .db import get_db
    try:
        connection = get_db()
        set_setting(
            connection,
            "telegram_last_result",
            json.dumps({
                "ok": bool(ok),
                "message": str(message)[:300],
                "at": current_time(),
            }),
        )
        connection.commit()
        connection.close()
    except Exception as error:
        print(f"[TELEGRAM] gagal mencatat hasil: {type(error).__name__}")


def load_last_result(connection):
    try:
        return json.loads(get_setting(connection, "telegram_last_result", ""))
    except Exception:
        return None


DEFAULT_REPEAT_MINUTES = 15
MAX_REPEAT_MINUTES = 1440


def _parse_repeat(value):
    """Jeda pengingat (menit). Kosong/rusak -> bawaan; 0 -> mati."""
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_REPEAT_MINUTES
    return min(max(minutes, 0), MAX_REPEAT_MINUTES)


def load_config(connection):
    token = get_setting(connection, "telegram_token")
    chat_id = get_setting(connection, "telegram_chat_id")
    source = "database"

    if not token and os.environ.get("TELEGRAM_BOT_TOKEN"):
        token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        source = "environment"
    if not chat_id and os.environ.get("TELEGRAM_CHAT_ID"):
        chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()

    return {
        "enabled": get_setting(connection, "telegram_enabled", "0") == "1",
        "token": token,
        "chat_id": chat_id,
        "notify_slow": get_setting(
            connection, "telegram_notify_slow", "0"
        ) == "1",
        "notify_recovery": get_setting(
            connection, "telegram_notify_recovery", "1"
        ) == "1",
        "repeat_minutes": _parse_repeat(
            get_setting(connection, "telegram_repeat_minutes", "")
        ),
        "token_source": source if token else "",
    }


def mask_token(token):
    """Tampilkan token sebagian saja agar tidak bocor ke browser."""
    if not token:
        return ""
    bot_id, _, secret = token.partition(":")
    return f"{bot_id}:••••{secret[-4:]}"


def public_config(connection):
    """Konfigurasi yang aman dikirim ke browser (tanpa token utuh)."""
    config = load_config(connection)
    return {
        "enabled": config["enabled"],
        "has_token": bool(config["token"]),
        "token_masked": mask_token(config["token"]),
        "token_source": config["token_source"],
        "chat_id": config["chat_id"],
        "notify_slow": config["notify_slow"],
        "notify_recovery": config["notify_recovery"],
        "repeat_minutes": config["repeat_minutes"],
        "last_result": load_last_result(connection),
    }


def save_config(connection, data):
    """
    Simpan pengaturan. Kembalikan pesan error (str) atau None.
    Token kosong berarti 'jangan diubah'.
    """
    token = str(data.get("token") or "").strip()
    chat_id = str(data.get("chat_id") or "").strip()

    if token and not TOKEN_PATTERN.fullmatch(token):
        return TOKEN_ERROR
    if chat_id and not CHAT_ID_PATTERN.fullmatch(chat_id):
        return CHAT_ID_ERROR

    repeat = None
    if "repeat_minutes" in data:
        raw = data.get("repeat_minutes")
        if isinstance(raw, bool) or not str(raw).strip().lstrip("-").isdigit():
            return "Jeda pengingat harus berupa angka menit."
        repeat = int(str(raw).strip())
        if repeat < 0 or repeat > MAX_REPEAT_MINUTES:
            return (
                f"Jeda pengingat harus antara 0 dan {MAX_REPEAT_MINUTES} menit."
            )

    enabled = bool(data.get("enabled"))
    effective = load_config(connection)
    final_token = token or effective["token"]
    final_chat = chat_id or effective["chat_id"]

    if enabled and not final_token:
        return "Isi token bot terlebih dahulu sebelum mengaktifkan notifikasi."
    if enabled and not final_chat:
        return "Isi Chat ID terlebih dahulu sebelum mengaktifkan notifikasi."

    if token:
        set_setting(connection, "telegram_token", token)
    set_setting(connection, "telegram_chat_id", chat_id)
    if repeat is not None:
        set_setting(connection, "telegram_repeat_minutes", str(repeat))
    set_setting(connection, "telegram_enabled", "1" if enabled else "0")
    set_setting(
        connection, "telegram_notify_slow",
        "1" if data.get("notify_slow") else "0",
    )
    set_setting(
        connection, "telegram_notify_recovery",
        "1" if data.get("notify_recovery") else "0",
    )
    connection.commit()
    return None


# =========================================================
# PENGIRIMAN KE TELEGRAM
# =========================================================

def _friendly_error(description):
    text = (description or "").lower()
    if "unauthorized" in text or text.strip() == "not found":
        return "Token bot salah atau bot sudah dihapus."
    if "chat not found" in text:
        return (
            "Chat tidak ditemukan. Buka bot di Telegram lalu tekan Start "
            "(atau masukkan bot ke grup), kemudian coba lagi."
        )
    if "bot was blocked" in text:
        return "Bot diblokir oleh pengguna tersebut."
    if "bot can't initiate" in text:
        return "Bot belum bisa mengirim. Tekan Start pada bot terlebih dahulu."
    if "not enough rights" in text or "have no rights" in text:
        return "Bot tidak punya izin mengirim pesan di chat tersebut."
    return description or "Telegram menolak permintaan."


def _call(token, method, payload=None, http_method="POST"):
    """
    Panggil API Telegram. Kembalikan (ok, data_atau_pesan, retry_after).
    URL (yang memuat token) sengaja tidak pernah dicetak ke log.
    """
    url = API_URL.format(token=token, method=method)
    data = None
    headers = {}

    if http_method == "POST":
        data = json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif payload:
        url += "?" + urllib.parse.urlencode(payload)

    request = urllib.request.Request(
        url, data=data, headers=headers, method=http_method
    )

    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except Exception:
            return False, f"Telegram membalas HTTP {error.code}.", None
        retry_after = (body.get("parameters") or {}).get("retry_after")
        return (
            False,
            _friendly_error(body.get("description")),
            retry_after,
        )
    except urllib.error.URLError:
        return False, (
            "Tidak dapat menjangkau server Telegram. Periksa koneksi "
            "internet server."
        ), None
    except TimeoutError:
        return False, "Permintaan ke Telegram melewati batas waktu.", None
    except Exception as error:
        return False, f"Gagal menghubungi Telegram ({type(error).__name__}).", None

    if not body.get("ok"):
        return False, _friendly_error(body.get("description")), None

    return True, body.get("result"), None


def send_message(token, chat_id, text):
    """Kirim satu pesan. Kembalikan (ok, pesan)."""
    ok, result, retry_after = _call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )

    if not ok and retry_after:
        time.sleep(min(int(retry_after), 30))
        ok, result, _ = _call(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    return ok, ("Terkirim." if ok else result)


def detect_chats(token):
    """
    Baca pesan terbaru yang masuk ke bot untuk menemukan Chat ID.
    Kembalikan (daftar_chat, pesan_error).
    """
    ok, result, _ = _call(
        token, "getUpdates", {"limit": 100}, http_method="GET"
    )
    if not ok:
        return [], result

    chats = {}
    for update in result or []:
        for key in (
            "message", "edited_message", "channel_post", "my_chat_member",
        ):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            name = (
                chat.get("title")
                or " ".join(filter(None, [
                    chat.get("first_name"), chat.get("last_name"),
                ]))
                or chat.get("username")
                or str(chat["id"])
            )
            chats[chat["id"]] = {
                "id": str(chat["id"]),
                "name": name,
                "type": chat.get("type", ""),
            }

    return list(chats.values()), None


# =========================================================
# ANTREAN PENGIRIMAN
# =========================================================

_queue = queue.Queue(maxsize=300)
_worker_lock = threading.Lock()
_worker_started = False


def _worker():
    while True:
        token, chat_id, text = _queue.get()
        try:
            ok, message = send_message(token, chat_id, text)
            record_result(ok, message)
            if not ok:
                print(f"[TELEGRAM] gagal kirim: {message}")
        except Exception as error:
            print(f"[TELEGRAM ERROR] {type(error).__name__}")
        # Telegram membatasi sekitar 1 pesan/detik per chat
        time.sleep(1.1)


def _enqueue(token, chat_id, text):
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True).start()
            _worker_started = True
    try:
        _queue.put_nowait((token, chat_id, text))
    except queue.Full:
        print("[TELEGRAM] antrean penuh, pesan dibuang")


# =========================================================
# PENYUSUNAN PESAN
# =========================================================

STATUS_LABEL = {
    "ONLINE": "Normal",
    "SLOW": "Lambat",
    "OFFLINE": "Terputus",
}


def _esc(value):
    return html.escape(str(value if value not in (None, "") else "-"))


def _place(device):
    parts = [
        str(device[key]).strip()
        for key in ("building", "floor", "room")
        if device[key]
    ]
    return ", ".join(parts) if parts else "Lokasi fisik belum diisi"


def _duration(seconds):
    minutes = max(int(seconds // 60), 1)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days} hari")
    if hours:
        parts.append(f"{hours} jam")
    if minutes and not days:
        parts.append(f"{minutes} menit")
    return " ".join(parts) or "kurang dari 1 menit"


def _technician_lines(connection):
    technicians = list_technicians(connection)[:5]
    if not technicians:
        return ""
    lines = ["", "<b>Hubungi teknisi:</b>"]
    for item in technicians:
        lines.append(f"• {_esc(item['name'])} — {_esc(item['phone'])}")
    return "\n".join(lines)


def _device_lines(device):
    latency = device["latency"]
    loss = device["packet_loss"]
    lines = [
        f"<b>Perangkat:</b> {_esc(device['location'])}",
        f"<b>IP:</b> <code>{_esc(device['ip_address'])}</code>",
        f"<b>Lokasi:</b> {_esc(_place(device))}",
    ]
    if latency is not None:
        lines.append(f"<b>Latency:</b> {latency:g} ms")
    if loss is not None:
        lines.append(f"<b>Packet loss:</b> {loss:g}%")
    return lines


def build_alert(connection, device, status):
    icon, title = {
        "OFFLINE": ("🔴", "PERANGKAT TERPUTUS"),
        "SLOW": ("🟠", "JARINGAN LAMBAT"),
    }[status]

    lines = [f"{icon} <b>{title}</b>", ""]
    lines += _device_lines(device)
    if device["cause_title"]:
        lines.append(f"<b>Dugaan penyebab:</b> {_esc(device['cause_title'])}")
    lines.append(f"<b>Waktu:</b> {current_time()}")
    text = "\n".join(lines) + _technician_lines(connection)
    return text


def build_reminder(connection, device, status, bad_seconds):
    title = {
        "OFFLINE": "MASIH TERPUTUS",
        "SLOW": "MASIH LAMBAT",
    }[status]

    lines = [f"⏰ <b>{title}</b>", ""]
    lines += _device_lines(device)
    if bad_seconds is not None:
        lines.append(f"<b>Sudah bermasalah:</b> {_duration(bad_seconds)}")
    if device["cause_title"]:
        lines.append(f"<b>Dugaan penyebab:</b> {_esc(device['cause_title'])}")
    lines.append(f"<b>Waktu:</b> {current_time()}")
    return "\n".join(lines) + _technician_lines(connection)


def build_recovery(device, status, down_seconds):
    still_slow = status == "SLOW"
    lines = [
        "🟡 <b>PERANGKAT KEMBALI MERESPONS (MASIH LAMBAT)</b>"
        if still_slow else
        "🟢 <b>PERANGKAT KEMBALI NORMAL</b>",
        "",
    ]
    lines += _device_lines(device)
    if down_seconds is not None:
        lines.append(f"<b>Lama gangguan:</b> {_duration(down_seconds)}")
    lines.append(f"<b>Waktu:</b> {current_time()}")
    return "\n".join(lines)


def build_summary(connection, group, kind):
    if kind == "alert":
        title = f"🔴 <b>{len(group)} PERANGKAT BERMASALAH</b>"
    elif kind == "reminder":
        title = f"⏰ <b>{len(group)} PERANGKAT MASIH BERMASALAH</b>"
    else:
        title = f"🟢 <b>{len(group)} PERANGKAT KEMBALI MERESPONS</b>"

    lines = [title, ""]
    for device, status, _ in group[:30]:
        lines.append(
            f"• {_esc(device['location'])} "
            f"(<code>{_esc(device['ip_address'])}</code>) — "
            f"{STATUS_LABEL.get(status, status)}"
        )
    if len(group) > 30:
        lines.append(f"… dan {len(group) - 30} perangkat lainnya")

    lines.append(f"\n<b>Waktu:</b> {current_time()}")
    text = "\n".join(lines)
    if kind in ("alert", "reminder"):
        text += _technician_lines(connection)
    return text


def build_test_message():
    return (
        "✅ <b>Tes notifikasi berhasil</b>\n\n"
        "Bot Telegram Network Monitoring Diskominfo sudah terhubung. "
        "Anda akan menerima pemberitahuan di chat ini saat perangkat "
        "terputus atau kembali normal.\n"
        f"<b>Waktu:</b> {current_time()}"
    )


# =========================================================
# DETEKSI PERUBAHAN STATUS
# =========================================================

_state = {}
_state_lock = threading.Lock()


def _track(results, repeat_seconds=0, notify_slow=False, now=None):
    """
    Perbarui mesin status. Kembalikan (events, reminders):
      events    : [(device_id, status_sebelumnya, status_baru, detik_gangguan)]
      reminders : [(device_id, status, detik_sejak_bermasalah)]
    Pengingat dikirim ulang tiap `repeat_seconds` selama perangkat masih
    berada pada status bermasalah yang sudah diumumkan.
    """
    events = []
    reminders = []
    now = time.time() if now is None else now

    with _state_lock:
        for result in results:
            device_id = result["id"]
            status = result["status"]

            if status not in KNOWN_STATUSES:
                continue

            state = _state.get(device_id)
            if state is None:
                # Perangkat dianggap sehat saat pertama dipantau, sehingga
                # perangkat yang sudah terputus ketika bot diaktifkan atau
                # aplikasi dijalankan tetap diumumkan satu kali.
                state = {
                    "notified": "ONLINE",
                    "candidate": status,
                    "count": 0,
                    "bad_since": None,
                    "last_sent": None,
                }
                _state[device_id] = state

            if status == state["candidate"]:
                state["count"] += 1
            else:
                state["candidate"] = status
                state["count"] = 1

            fired = False

            if (
                state["count"] >= CONFIRM_CHECKS
                and state["candidate"] != state["notified"]
            ):
                previous = state["notified"]
                state["notified"] = state["candidate"]
                state["last_sent"] = now
                fired = True

                if previous == "ONLINE":
                    state["bad_since"] = now

                down_seconds = None
                if state["candidate"] == "ONLINE" and state["bad_since"]:
                    down_seconds = now - state["bad_since"]
                if state["candidate"] == "ONLINE":
                    state["bad_since"] = None

                events.append(
                    (device_id, previous, state["candidate"], down_seconds)
                )

            still_bad = state["notified"] == "OFFLINE" or (
                state["notified"] == "SLOW" and notify_slow
            )

            if (
                not fired
                and repeat_seconds > 0
                and still_bad
                and state["candidate"] == state["notified"]
                and state["last_sent"] is not None
                and now - state["last_sent"] >= repeat_seconds
            ):
                state["last_sent"] = now
                bad_seconds = (
                    now - state["bad_since"] if state["bad_since"] else None
                )
                reminders.append((device_id, state["notified"], bad_seconds))

        # Buang state perangkat yang sudah dihapus
        active = {result["id"] for result in results}
        for stale in set(_state) - active:
            del _state[stale]

    return events, reminders


def forget_device(device_id):
    with _state_lock:
        _state.pop(device_id, None)


def _should_send(config, previous, status):
    if status == "OFFLINE":
        return "alert"
    if status == "SLOW" and previous == "ONLINE":
        return "alert" if config["notify_slow"] else None
    if previous == "OFFLINE" and status in ("ONLINE", "SLOW"):
        return "recovery" if config["notify_recovery"] else None
    if previous == "SLOW" and status == "ONLINE":
        if config["notify_slow"] and config["notify_recovery"]:
            return "recovery"
    return None


def process_cycle(connection, results, now=None):
    """
    Dipanggil sekali per siklus monitoring (setelah diagnosa tersimpan).
    results: list dict {id, status, old_status}
    """
    config = load_config(connection)
    if not (config["enabled"] and config["token"] and config["chat_id"]):
        # Saat nonaktif, lupakan status agar begitu diaktifkan semua
        # perangkat bermasalah saat itu langsung diumumkan.
        with _state_lock:
            _state.clear()
        return

    events, reminders = _track(
        results,
        repeat_seconds=config["repeat_minutes"] * 60,
        notify_slow=config["notify_slow"],
        now=now,
    )
    if not events and not reminders:
        return

    def fetch(device_id):
        return connection.execute(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        ).fetchone()

    alerts, recoveries, repeats = [], [], []

    for device_id, previous, status, down_seconds in events:
        kind = _should_send(config, previous, status)
        if not kind:
            continue

        device = fetch(device_id)
        if not device:
            continue

        (alerts if kind == "alert" else recoveries).append(
            (device, status, down_seconds)
        )

    for device_id, status, bad_seconds in reminders:
        device = fetch(device_id)
        if device:
            repeats.append((device, status, bad_seconds))

    groups = (
        (alerts, "alert"),
        (recoveries, "recovery"),
        (repeats, "reminder"),
    )

    for group, kind in groups:
        if not group:
            continue

        if len(group) > SUMMARY_THRESHOLD:
            text = build_summary(connection, group, kind)
            _enqueue(config["token"], config["chat_id"], text)
            continue

        for device, status, seconds in group:
            if kind == "alert":
                text = build_alert(connection, device, status)
            elif kind == "reminder":
                text = build_reminder(connection, device, status, seconds)
            else:
                text = build_recovery(device, status, seconds)
            _enqueue(config["token"], config["chat_id"], text)