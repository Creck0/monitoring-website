"""
NETWORK MONITORING - DISKOMINFO
Backend REST API + penyaji dashboard.

Menjalankan:
    python app.py
"""

import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    Response,
)
from werkzeug.security import check_password_hash, generate_password_hash

from monitoring import reports, telegram_bot
from monitoring.db import (
    current_time,
    get_db,
    get_setting,
    init_database,
    set_setting,
)
from monitoring.diagnostics import area_summary, diagnose, hotspots
from monitoring.monitor import MONITOR_INTERVAL, monitoring_loop
from monitoring.pinger import LATENCY_NORMAL, LATENCY_SLOW
from monitoring.tracer import last_trace, trace_device

HOST = "127.0.0.1"
PORT = 5000

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = 8 * 60 * 60
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


# =========================================================
# AUTENTIKASI
# =========================================================

def login_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Sesi login berakhir. Silakan login kembali."
                }), 401

            return redirect(url_for("login", next=request.path))

        return view_function(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if "user_id" in session:
            return redirect(url_for("index"))

        return render_template("login.html")

    data = request.form or request.get_json(silent=True) or {}

    username = data.get("username", "").strip()
    password = data.get("password", "")

    wants_json = request.is_json or request.headers.get(
        "X-Requested-With"
    ) == "XMLHttpRequest"

    error = None

    if not username or not password:
        error = "Username dan password wajib diisi."

    else:
        connection = get_db()

        user = connection.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        connection.close()

        if not user or not check_password_hash(
            user["password_hash"], password
        ):
            error = "Username atau password salah."

        else:
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session.permanent = True

    if error:
        if wants_json:
            return jsonify({"error": error}), 401

        return render_template("login.html", error=error)

    next_url = (
        request.args.get("next")
        or data.get("next")
        or url_for("index")
    )

    if wants_json:
        return jsonify({"message": "Login berhasil.", "redirect": next_url})

    return redirect(next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/account/password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json() or {}

    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if len(new_password) < 6:
        return jsonify({
            "error": "Password baru minimal 6 karakter."
        }), 400

    connection = get_db()

    user = connection.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()

    if not user or not check_password_hash(
        user["password_hash"], old_password
    ):
        connection.close()
        return jsonify({"error": "Password lama salah."}), 400

    connection.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), session["user_id"]),
    )

    connection.commit()
    connection.close()

    return jsonify({"message": "Password berhasil diperbarui."})


# =========================================================
# HALAMAN
# =========================================================

@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        interval=MONITOR_INTERVAL,
        latency_normal=LATENCY_NORMAL,
        latency_slow=LATENCY_SLOW,
        username=session.get("username", ""),
    )


# =========================================================
# API - PERANGKAT
# =========================================================

DEVICE_FIELDS = (
    "location", "ip_address", "description", "device_type",
    "building", "floor", "room", "parent_id",
)


def _clean_payload(data):
    payload = {}

    for field in DEVICE_FIELDS:
        value = data.get(field)

        if field == "parent_id":
            payload[field] = int(value) if value else None
        else:
            payload[field] = (value or "").strip()

    return payload


def _serialize(row):
    device = dict(row)

    device["recommendations"] = json.loads(
        device.get("recommendations") or "[]"
    )

    parts = [
        value
        for value in (
            device.get("building"),
            device.get("floor"),
            device.get("room"),
        )
        if value
    ]

    device["place"] = ", ".join(parts)

    return device


@app.route("/api/devices", methods=["GET"])
@login_required
def get_devices():
    connection = get_db()

    devices = connection.execute(
        """
        SELECT d.*, p.location AS parent_location,
               t.bottleneck_hop, t.bottleneck_ip, t.bottleneck_label,
               t.bottleneck_delta, t.created_at AS trace_at
        FROM devices d
        LEFT JOIN devices p ON d.parent_id = p.id
        LEFT JOIN path_traces t ON t.device_id = d.id
        ORDER BY
            CASE d.status
                WHEN 'OFFLINE' THEN 0
                WHEN 'SLOW' THEN 1
                ELSE 2
            END,
            d.building, d.floor, d.location
        """
    ).fetchall()

    connection.close()

    return jsonify([_serialize(device) for device in devices])


@app.route("/api/devices", methods=["POST"])
@login_required
def add_device():
    payload = _clean_payload(request.get_json() or {})

    if not payload["location"]:
        return jsonify({"error": "Nama/lokasi wajib diisi."}), 400

    if not payload["ip_address"]:
        return jsonify({"error": "IP Address wajib diisi."}), 400

    connection = get_db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO devices
            (location, ip_address, description, device_type,
             building, floor, room, parent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(payload[field] for field in DEVICE_FIELDS),
        )

        connection.commit()
        device_id = cursor.lastrowid
        connection.close()

        return jsonify({
            "message": "Perangkat berhasil ditambahkan.",
            "id": device_id,
        }), 201

    except sqlite3.IntegrityError:
        connection.close()
        return jsonify({
            "error": "IP Address tersebut sudah terdaftar."
        }), 400


@app.route("/api/devices/<int:device_id>", methods=["PUT"])
@login_required
def update_device(device_id):
    payload = _clean_payload(request.get_json() or {})

    if not payload["location"]:
        return jsonify({"error": "Nama/lokasi wajib diisi."}), 400

    if not payload["ip_address"]:
        return jsonify({"error": "IP Address wajib diisi."}), 400

    if payload["parent_id"] == device_id:
        return jsonify({
            "error": "Perangkat tidak boleh menjadi induk dirinya sendiri."
        }), 400

    connection = get_db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            UPDATE devices SET
                location = ?, ip_address = ?, description = ?,
                device_type = ?, building = ?, floor = ?,
                room = ?, parent_id = ?
            WHERE id = ?
            """,
            tuple(payload[field] for field in DEVICE_FIELDS) + (device_id,),
        )

        if cursor.rowcount == 0:
            connection.close()
            return jsonify({"error": "Perangkat tidak ditemukan."}), 404

        connection.commit()
        connection.close()

        return jsonify({"message": "Perangkat berhasil diperbarui."})

    except sqlite3.IntegrityError:
        connection.close()
        return jsonify({
            "error": "IP Address tersebut sudah digunakan."
        }), 400


@app.route("/api/devices/<int:device_id>", methods=["DELETE"])
@login_required
def delete_device(device_id):
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute(
        "DELETE FROM monitoring_logs WHERE device_id = ?", (device_id,)
    )
    cursor.execute(
        "DELETE FROM path_traces WHERE device_id = ?", (device_id,)
    )
    cursor.execute(
        "UPDATE devices SET parent_id = NULL WHERE parent_id = ?",
        (device_id,),
    )
    cursor.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    if cursor.rowcount == 0:
        connection.close()
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    connection.commit()
    connection.close()

    return jsonify({"message": "Perangkat berhasil dihapus."})


# =========================================================
# API - RINGKASAN & ANALISA LOKASI
# =========================================================

@app.route("/api/summary", methods=["GET"])
@login_required
def get_summary():
    connection = get_db()
    cursor = connection.cursor()

    def count(condition="", params=()):
        return cursor.execute(
            f"SELECT COUNT(*) FROM devices {condition}", params
        ).fetchone()[0]

    average = cursor.execute(
        "SELECT AVG(latency) FROM devices WHERE status IN "
        "('ONLINE','SLOW')"
    ).fetchone()[0]

    result = {
        "total": count(),
        "online": count("WHERE status = 'ONLINE'"),
        "slow": count("WHERE status = 'SLOW'"),
        "offline": count("WHERE status = 'OFFLINE'"),
    }

    connection.close()

    return jsonify({
        **result,
        "avg_latency": round(average, 2) if average else None,
        "interval": MONITOR_INTERVAL,
        "updated_at": current_time(),
    })


@app.route("/api/hotspots", methods=["GET"])
@login_required
def get_hotspots():
    """Daftar titik yang bermasalah beserta lokasi fisiknya."""

    return jsonify(hotspots())


@app.route("/api/areas", methods=["GET"])
@login_required
def get_areas():
    """Rekap kondisi jaringan per gedung/lantai."""

    return jsonify(area_summary())


@app.route("/api/devices/<int:device_id>/diagnosis", methods=["GET"])
@login_required
def get_diagnosis(device_id):
    result = diagnose(device_id)

    if result is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    return jsonify(result)


# =========================================================
# API - PELACAKAN JALUR
# =========================================================

@app.route("/api/devices/<int:device_id>/trace", methods=["POST"])
@login_required
def run_trace(device_id):
    connection = get_db()

    device = connection.execute(
        "SELECT id, ip_address FROM devices WHERE id = ?", (device_id,)
    ).fetchone()

    connection.close()

    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    result = trace_device(device_id, device["ip_address"])

    if result.get("error"):
        return jsonify(result), 400

    return jsonify(result)


@app.route("/api/devices/<int:device_id>/trace", methods=["GET"])
@login_required
def get_trace(device_id):
    result = last_trace(device_id)

    if result is None:
        return jsonify({
            "message": "Belum ada hasil pelacakan untuk perangkat ini."
        }), 404

    return jsonify(result)


# =========================================================
# API - LOG
# =========================================================

@app.route("/api/devices/<int:device_id>/logs", methods=["GET"])
@login_required
def get_device_logs(device_id):
    limit = min(request.args.get("limit", default=60, type=int), 300)

    connection = get_db()

    logs = connection.execute(
        """
        SELECT id, device_id, status, latency, packet_loss,
               jitter, checked_at
        FROM monitoring_logs
        WHERE device_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()

    connection.close()

    return jsonify([dict(log) for log in logs])


@app.route("/api/logs", methods=["GET"])
@login_required
def get_all_logs():
    limit = min(request.args.get("limit", default=100, type=int), 500)
    status = request.args.get("status", default="", type=str).upper()

    condition = ""
    params = []

    if status in ("ONLINE", "SLOW", "OFFLINE"):
        condition = "WHERE monitoring_logs.status = ?"
        params.append(status)

    params.append(limit)

    connection = get_db()

    logs = connection.execute(
        f"""
        SELECT monitoring_logs.id, monitoring_logs.device_id,
               devices.location, devices.ip_address,
               devices.building, devices.floor, devices.room,
               monitoring_logs.status, monitoring_logs.latency,
               monitoring_logs.packet_loss, monitoring_logs.checked_at
        FROM monitoring_logs
        JOIN devices ON monitoring_logs.device_id = devices.id
        {condition}
        ORDER BY monitoring_logs.id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()

    connection.close()

    return jsonify([dict(log) for log in logs])


# =========================================================
# API - LAPORAN (CSV / EXCEL / PDF)
# =========================================================

@app.route("/api/report/daily", methods=["GET"])
@login_required
def download_daily_report():
    report_date = request.args.get(
        "date", default=datetime.now().strftime("%Y-%m-%d")
    )

    file_format = request.args.get("format", default="pdf").lower()

    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({
            "error": "Format tanggal tidak valid. Gunakan YYYY-MM-DD."
        }), 400

    report = reports.build_report(
        report_date, session.get("username", "-")
    )

    filename = f"laporan-monitoring-{report_date}"

    if file_format == "csv":
        response = Response(reports.to_csv(report), mimetype="text/csv")
        response.headers["Content-Disposition"] = (
            f"attachment; filename={filename}.csv"
        )
        return response

    if file_format in ("xlsx", "excel"):
        return send_file(
            reports.to_xlsx(report),
            mimetype=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            as_attachment=True,
            download_name=f"{filename}.xlsx",
        )

    if file_format == "pdf":
        return send_file(
            reports.to_pdf(report),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"{filename}.pdf",
        )

    return jsonify({
        "error": "Format tidak dikenal. Gunakan csv, xlsx, atau pdf."
    }), 400


@app.route("/api/report/preview", methods=["GET"])
@login_required
def report_preview():
    """Ringkasan laporan untuk ditampilkan di dashboard."""

    report_date = request.args.get(
        "date", default=datetime.now().strftime("%Y-%m-%d")
    )

    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Format tanggal tidak valid."}), 400

    report = reports.build_report(
        report_date, session.get("username", "-")
    )

    return jsonify({
        "date": report["date"],
        "summary": {
            key: value
            for key, value in report["summary"].items()
            if key != "bermasalah"
        },
        "bermasalah": report["summary"]["bermasalah"],
        "rows": report["rows"],
    })


# =========================================================
# API - SUPPORT / TEKNISI
# =========================================================

PHONE_PATTERN = re.compile(r"^\+?[\d\s\-().]{6,20}$")


def _valid_contact(value):
    """Kontak teknisi boleh berupa alamat IP atau nomor telepon."""

    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass

    digits = re.sub(r"\D", "", value)

    return bool(PHONE_PATTERN.match(value)) and 6 <= len(digits) <= 15


@app.route("/api/technicians", methods=["GET"])
@login_required
def get_technicians():
    return jsonify(telegram_bot.get_technicians())


@app.route("/api/technicians", methods=["POST"])
@login_required
def add_technician():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    contact = (data.get("contact") or "").strip()

    if not name:
        return jsonify({"error": "Nama teknisi wajib diisi."}), 400

    if len(name) > 100:
        return jsonify({"error": "Nama teknisi terlalu panjang."}), 400

    if not contact:
        return jsonify({
            "error": "IP atau nomor telepon teknisi wajib diisi."
        }), 400

    if not _valid_contact(contact):
        return jsonify({
            "error": "Isi dengan alamat IP yang valid atau nomor "
                     "telepon (contoh: 192.168.1.20 atau 0812-3456-7890)."
        }), 400

    connection = get_db()

    cursor = connection.execute(
        "INSERT INTO technicians (name, contact, created_at) "
        "VALUES (?, ?, ?)",
        (name, contact, current_time()),
    )

    connection.commit()
    technician_id = cursor.lastrowid
    connection.close()

    return jsonify({
        "message": "Teknisi berhasil ditambahkan.",
        "id": technician_id,
    }), 201


@app.route("/api/technicians/<int:technician_id>", methods=["DELETE"])
@login_required
def delete_technician(technician_id):
    connection = get_db()

    cursor = connection.execute(
        "DELETE FROM technicians WHERE id = ?", (technician_id,)
    )

    connection.commit()
    connection.close()

    if cursor.rowcount == 0:
        return jsonify({"error": "Teknisi tidak ditemukan."}), 404

    return jsonify({"message": "Teknisi berhasil dihapus."})


# =========================================================
# API - NOTIFIKASI TELEGRAM
# =========================================================

TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
CHAT_ID_PATTERN = re.compile(r"^(-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")


def _telegram_settings():
    token = telegram_bot.get_token()

    return {
        "configured": bool(token),
        "token_masked": telegram_bot.mask_token(token),
        "token_source": (
            "env" if os.environ.get("TELEGRAM_BOT_TOKEN") else "database"
        ),
        "bot_username": get_setting("telegram_bot_username", ""),
        "notify_enabled": telegram_bot.notify_enabled(),
        "confirm_checks": telegram_bot.OFFLINE_CONFIRM,
        "repeat_interval": telegram_bot.REPEAT_INTERVAL,
    }


@app.route("/api/telegram/settings", methods=["GET"])
@login_required
def get_telegram_settings():
    return jsonify(_telegram_settings())


@app.route("/api/telegram/settings", methods=["POST"])
@login_required
def update_telegram_settings():
    data = request.get_json(silent=True) or {}

    warning = None

    if "notify_enabled" in data:
        set_setting(
            "telegram_notify", "1" if data["notify_enabled"] else "0"
        )

    token = (data.get("token") or "").strip()

    if token:
        if not TOKEN_PATTERN.match(token):
            return jsonify({
                "error": "Format token tidak valid. Salin token utuh "
                         "dari @BotFather (contoh: 123456789:AAE...)."
            }), 400

        state, info = telegram_bot.validate_token(token)

        if state == "invalid":
            return jsonify({
                "error": f"Token ditolak Telegram: {info}"
            }), 400

        if state == "network":
            warning = (
                "Token disimpan, tetapi belum bisa diverifikasi karena "
                "server tidak dapat terhubung ke Telegram. "
                "Periksa koneksi internet server."
            )
        else:
            set_setting("telegram_bot_username", info)

        set_setting("telegram_token", token)

    result = {"message": "Pengaturan Telegram disimpan.", **_telegram_settings()}

    if warning:
        result["warning"] = warning

    return jsonify(result)


@app.route("/api/telegram/accounts", methods=["GET"])
@login_required
def get_telegram_accounts():
    return jsonify(telegram_bot.get_accounts())


@app.route("/api/telegram/accounts", methods=["POST"])
@login_required
def add_telegram_account():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    chat_id = str(data.get("chat_id") or "").strip()

    if not name:
        return jsonify({"error": "Nama akun wajib diisi."}), 400

    if len(name) > 100:
        return jsonify({"error": "Nama akun terlalu panjang."}), 400

    if not chat_id:
        return jsonify({"error": "Chat ID Telegram wajib diisi."}), 400

    if not CHAT_ID_PATTERN.match(chat_id):
        return jsonify({
            "error": "Chat ID harus berupa angka (contoh: 123456789 "
                     "atau -1001234567890 untuk grup)."
        }), 400

    connection = get_db()

    try:
        cursor = connection.execute(
            "INSERT INTO telegram_accounts (name, chat_id, created_at) "
            "VALUES (?, ?, ?)",
            (name, chat_id, current_time()),
        )

        connection.commit()
        account_id = cursor.lastrowid

    except sqlite3.IntegrityError:
        return jsonify({
            "error": "Chat ID tersebut sudah terdaftar."
        }), 400

    finally:
        connection.close()

    return jsonify({
        "message": "Akun Telegram berhasil ditambahkan.",
        "id": account_id,
    }), 201


@app.route("/api/telegram/accounts/<int:account_id>", methods=["DELETE"])
@login_required
def delete_telegram_account(account_id):
    connection = get_db()

    cursor = connection.execute(
        "DELETE FROM telegram_accounts WHERE id = ?", (account_id,)
    )

    connection.commit()
    connection.close()

    if cursor.rowcount == 0:
        return jsonify({"error": "Akun tidak ditemukan."}), 404

    return jsonify({"message": "Akun Telegram berhasil dihapus."})


@app.route("/api/telegram/accounts/<int:account_id>/test", methods=["POST"])
@login_required
def test_telegram_account(account_id):
    connection = get_db()

    account = connection.execute(
        "SELECT name, chat_id FROM telegram_accounts WHERE id = ?",
        (account_id,),
    ).fetchone()

    connection.close()

    if account is None:
        return jsonify({"error": "Akun tidak ditemukan."}), 404

    if not telegram_bot.get_token():
        return jsonify({
            "error": "Token bot belum diisi. Isi token bot terlebih dahulu."
        }), 400

    ok, error = telegram_bot.send_message(
        account["chat_id"],
        telegram_bot.build_test_message(account["name"]),
    )

    if not ok:
        if error and "chat not found" in error.lower():
            error = (
                "Chat ID tidak ditemukan. Pastikan pemilik akun sudah "
                "membuka bot dan menekan Start / mengirim /start."
            )

        return jsonify({"error": f"Gagal mengirim: {error}"}), 400

    return jsonify({"message": f"Pesan tes terkirim ke {account['name']}."})


# =========================================================
# MENJALANKAN PROGRAM
# =========================================================

def create_runtime():
    init_database()

    thread = threading.Thread(target=monitoring_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    print("")
    print("=" * 58)
    print("        NETWORK MONITORING - DISKOMINFO")
    print("=" * 58)
    print("")

    create_runtime()

    print(f"Dashboard           : http://{HOST}:{PORT}")
    print(f"Interval monitoring : {MONITOR_INTERVAL} detik")
    print(f"Batas latency normal: {LATENCY_NORMAL} ms")
    print("")
    print("Tekan CTRL+C untuk menghentikan server.")
    print("")

    app.run(host=HOST, port=PORT, debug=False, threaded=True)
