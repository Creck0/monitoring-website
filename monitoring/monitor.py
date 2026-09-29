"""
Proses monitoring berkala yang berjalan di latar belakang.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from .db import get_db, current_time
from .diagnostics import diagnose, store_diagnosis
from .pinger import ping_device, determine_status
from .telegram_bot import process_alerts

MONITOR_INTERVAL = 10

MAX_WORKERS = 12

# Batas jumlah log yang disimpan per perangkat
LOG_RETENTION = 2000


def check_device(device_id, ip_address):
    measurement = ping_device(ip_address)
    status = determine_status(measurement)
    checked_at = current_time()

    connection = get_db()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE devices SET
            status      = ?,
            latency     = ?,
            packet_loss = ?,
            jitter      = ?,
            checked_at  = ?
        WHERE id = ?
        """,
        (
            status,
            measurement["latency"],
            measurement["packet_loss"],
            measurement["jitter"],
            checked_at,
            device_id,
        ),
    )

    cursor.execute(
        """
        INSERT INTO monitoring_logs
        (device_id, status, latency, packet_loss, jitter, checked_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            status,
            measurement["latency"],
            measurement["packet_loss"],
            measurement["jitter"],
            checked_at,
        ),
    )

    connection.commit()
    connection.close()

    return device_id, status, measurement


def monitor_all_devices():
    connection = get_db()

    devices = connection.execute(
        "SELECT id, ip_address FROM devices"
    ).fetchall()

    connection.close()

    if not devices:
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(check_device, device["id"], device["ip_address"])
            for device in devices
        ]

        for future in futures:
            try:
                future.result()
            except Exception as error:
                print(f"[MONITOR ERROR] {error}")

    # Analisa penyebab dijalankan setelah semua perangkat
    # diperiksa, agar kondisi antar perangkat bisa dibandingkan.
    connection = get_db()

    for device in devices:
        try:
            diagnosis = diagnose(device["id"], connection)

            if diagnosis:
                store_diagnosis(connection, device["id"], diagnosis)

        except Exception as error:
            print(f"[DIAGNOSA ERROR] {error}")

    connection.commit()
    connection.close()

    # Kirim notifikasi Telegram untuk perangkat yang baru terputus /
    # baru pulih. Dijalankan setelah diagnosa tersimpan agar dugaan
    # penyebab ikut tercantum di pesan.
    try:
        process_alerts()
    except Exception as error:
        print(f"[TELEGRAM ALERT ERROR] {error}")


def prune_logs():
    """Buang log lama agar database tidak membengkak."""

    connection = get_db()

    connection.execute(
        """
        DELETE FROM monitoring_logs
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY device_id ORDER BY id DESC
                       ) AS rn
                FROM monitoring_logs
            )
            WHERE rn <= ?
        )
        """,
        (LOG_RETENTION,),
    )

    connection.commit()
    connection.close()


def monitoring_loop():
    print(f"[MONITORING] Interval = {MONITOR_INTERVAL} detik")

    cycle = 0

    while True:
        try:
            monitor_all_devices()

            cycle += 1

            if cycle % 180 == 0:
                prune_logs()

        except Exception as error:
            print(f"[MONITORING ERROR] {error}")

        time.sleep(MONITOR_INTERVAL)
