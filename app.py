"""
NETWORK MONITORING - DISKOMINFO
Backend REST API + penyaji dashboard.

Menjalankan:
    python app.py
"""

import io
import json
import os
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

from monitoring import reports
from monitoring.db import current_time, get_db, init_database
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
# API - KONVERTER BERKAS
# =========================================================

@app.route("/api/tools/convert", methods=["POST"])
@login_required
def convert_file():
    """
    Konversi berkas tabel:
    CSV  -> XLSX / PDF
    XLSX -> PDF / CSV
    """

    uploaded = request.files.get("file")

    target = request.form.get("target", "xlsx").lower()
    title = request.form.get("title", "").strip()

    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Berkas belum dipilih."}), 400

    name = uploaded.filename.rsplit(".", 1)[0]
    extension = uploaded.filename.rsplit(".", 1)[-1].lower()

    try:
        if extension in ("csv", "txt"):
            raw = uploaded.read()

            for encoding in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                return jsonify({
                    "error": "Encoding berkas tidak dikenali."
                }), 400

            rows = reports.sniff_csv(text)

        elif extension in ("xlsx", "xlsm"):
            rows = reports.xlsx_to_rows(uploaded)

        else:
            return jsonify({
                "error":
                    "Jenis berkas tidak didukung. "
                    "Gunakan .csv atau .xlsx."
            }), 400

    except Exception as error:
        return jsonify({"error": f"Gagal membaca berkas: {error}"}), 400

    if not rows:
        return jsonify({"error": "Berkas tidak berisi data."}), 400

    heading = title or name.replace("-", " ").replace("_", " ").title()

    if target == "xlsx":
        return send_file(
            reports.csv_to_xlsx(rows, title=heading[:30]),
            mimetype=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            as_attachment=True,
            download_name=f"{name}.xlsx",
        )

    if target == "pdf":
        return send_file(
            reports.rows_to_pdf(
                rows,
                title=heading,
                subtitle=(
                    f"Hasil konversi berkas {uploaded.filename} | "
                    f"{len(rows) - 1} baris data"
                ),
                printed_by=session.get("username", "-"),
            ),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"{name}.pdf",
        )

    if target == "csv":
        buffer = io.StringIO()

        import csv as csv_module

        writer = csv_module.writer(buffer)
        writer.writerows(rows)

        response = Response(
            "\ufeff" + buffer.getvalue(), mimetype="text/csv"
        )
        response.headers["Content-Disposition"] = (
            f"attachment; filename={name}.csv"
        )
        return response

    return jsonify({"error": "Format tujuan tidak dikenal."}), 400


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
