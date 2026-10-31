"""
Mesin analisa penyebab gangguan dan saran penanganan.

Modul ini membaca riwayat monitoring sebuah perangkat lalu
menyimpulkan kemungkinan penyebab gangguan beserta langkah
penanganan yang disarankan, dalam bahasa yang bisa dipahami
petugas non-teknis.

Catatan: hasil analisa bersifat dugaan berdasarkan pola data
ping, bukan kepastian. Pemeriksaan fisik tetap diperlukan.
"""

import json

from .db import get_db
from .pinger import LATENCY_NORMAL, LATENCY_SLOW

# Jumlah log terakhir yang dianalisa
WINDOW = 20

SEVERITY = {
    "ONLINE": 0,
    "SLOW": 1,
    "OFFLINE": 2,
    "UNKNOWN": 0,
}


# ---------------------------------------------------------
# Pengambilan data pendukung
# ---------------------------------------------------------

def _recent_logs(connection, device_id, limit=WINDOW):
    return connection.execute(
        """
        SELECT status, latency, packet_loss, jitter, checked_at
        FROM monitoring_logs
        WHERE device_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()


def _siblings(connection, device):
    """Perangkat lain pada segmen yang sama (induk / gedung / lantai)."""

    if device["parent_id"]:
        return connection.execute(
            "SELECT id, location, status FROM devices "
            "WHERE parent_id = ? AND id != ?",
            (device["parent_id"], device["id"]),
        ).fetchall()

    if device["building"]:
        return connection.execute(
            "SELECT id, location, status FROM devices "
            "WHERE building = ? AND id != ?",
            (device["building"], device["id"]),
        ).fetchall()

    return []


def _parent(connection, device):
    if not device["parent_id"]:
        return None

    return connection.execute(
        "SELECT id, location, ip_address, status, device_type "
        "FROM devices WHERE id = ?",
        (device["parent_id"],),
    ).fetchone()


# ---------------------------------------------------------
# Analisa utama
# ---------------------------------------------------------

def diagnose(device_id, connection=None):
    own_connection = connection is None

    if own_connection:
        connection = get_db()

    device = connection.execute(
        "SELECT * FROM devices WHERE id = ?",
        (device_id,),
    ).fetchone()

    if device is None:
        if own_connection:
            connection.close()
        return None

    logs = _recent_logs(connection, device_id)
    siblings = _siblings(connection, device)
    parent = _parent(connection, device)

    trace = connection.execute(
        "SELECT * FROM path_traces WHERE device_id = ?",
        (device_id,),
    ).fetchone()

    result = _analyse(device, logs, siblings, parent, trace)

    if own_connection:
        connection.close()

    return result


def _stats(logs):
    total = len(logs)

    latencies = [
        log["latency"] for log in logs
        if log["latency"] is not None
    ]

    losses = [
        log["packet_loss"] for log in logs
        if log["packet_loss"] is not None
    ]

    jitters = [
        log["jitter"] for log in logs
        if log["jitter"] is not None
    ]

    offline = sum(1 for log in logs if log["status"] == "OFFLINE")
    slow = sum(1 for log in logs if log["status"] == "SLOW")

    # Berapa kali gagal berturut-turut (log terbaru di urutan pertama)
    streak = 0

    for log in logs:
        if log["status"] == "OFFLINE":
            streak += 1
        else:
            break

    return {
        "total": total,
        "offline": offline,
        "slow": slow,
        "streak": streak,
        "ever_online": any(
            log["status"] in ("ONLINE", "SLOW") for log in logs
        ),
        "avg_latency": (
            round(sum(latencies) / len(latencies), 2)
            if latencies else None
        ),
        "max_latency": max(latencies) if latencies else None,
        "avg_loss": (
            round(sum(losses) / len(losses), 2)
            if losses else 0
        ),
        "avg_jitter": (
            round(sum(jitters) / len(jitters), 2)
            if jitters else 0
        ),
        "instability": (
            round(slow / len(logs) * 100, 1) if logs else 0
        ),
    }


def _analyse(device, logs, siblings, parent, trace):
    status = device["status"] or "UNKNOWN"
    stats = _stats(logs)

    sibling_down = [
        sibling for sibling in siblings
        if sibling["status"] in ("OFFLINE", "SLOW")
    ]

    segment_wide = (
        len(siblings) > 0
        and len(sibling_down) >= max(1, int(len(siblings) * 0.6))
    )

    parent_down = (
        parent is not None
        and parent["status"] in ("OFFLINE", "SLOW")
    )

    # -----------------------------------------------------
    # PERANGKAT TIDAK DAPAT DIJANGKAU
    # -----------------------------------------------------

    if status == "OFFLINE":

        if parent_down:
            return _result(
                "UPLINK_DOWN",
                "Gangguan pada perangkat induk (uplink)",
                f"Perangkat induk {parent['location']} "
                f"({parent['ip_address']}) juga sedang bermasalah. "
                "Selama jalur induk terputus, seluruh perangkat di "
                "bawahnya ikut tidak dapat dijangkau, sehingga "
                "perangkat ini kemungkinan besar bukan sumber masalah.",
                "UPLINK",
                2,
                [
                    f"Periksa terlebih dahulu {parent['location']} "
                    "sebelum memeriksa perangkat ini.",
                    "Pastikan kabel uplink dari router ke switch "
                    "terpasang dan lampu indikator port menyala.",
                    "Restart perangkat induk bila perlu, lalu tunggu "
                    "1-2 menit dan amati apakah perangkat di bawahnya "
                    "kembali online.",
                    "Bila perangkat induk tetap mati, hubungi teknisi "
                    "jaringan untuk pemeriksaan perangkat keras.",
                ],
                stats,
            )

        if segment_wide:
            names = ", ".join(
                sibling["location"] for sibling in sibling_down[:3]
            )

            return _result(
                "SEGMENT_DOWN",
                "Gangguan menyebar pada satu segmen/area",
                "Perangkat lain di area yang sama juga bermasalah "
                f"({names}). Pola ini menunjukkan gangguan terjadi "
                "pada perangkat distribusi (switch/AP induk) atau "
                "suplai listrik area tersebut, bukan pada satu "
                "perangkat saja.",
                "SEGMENT",
                2,
                [
                    "Periksa switch distribusi dan sumber listrik "
                    "pada area tersebut.",
                    "Pastikan tidak ada pemadaman listrik atau MCB "
                    "yang turun di ruang panel.",
                    "Periksa kabel uplink dari switch area ke router "
                    "utama.",
                    "Jika switch menggunakan PoE, periksa apakah "
                    "adaptor PoE masih berfungsi.",
                ],
                stats,
            )

        if not stats["ever_online"]:
            return _result(
                "NEVER_REACHED",
                "Perangkat belum pernah berhasil dijangkau",
                "Sejak didaftarkan, perangkat ini tidak pernah "
                "merespons ping. Umumnya penyebabnya adalah IP yang "
                "salah ketik, perangkat belum aktif, berada di "
                "segmen jaringan yang berbeda, atau ICMP diblokir "
                "oleh firewall perangkat.",
                "LOCAL",
                2,
                [
                    "Cek ulang penulisan IP Address perangkat.",
                    "Pastikan perangkat berada pada subnet yang dapat "
                    "dijangkau dari server monitoring.",
                    "Coba ping manual dari komputer yang satu jaringan "
                    "dengan perangkat tersebut.",
                    "Periksa apakah perangkat memblokir ICMP; bila ya, "
                    "aktifkan balasan ping pada konfigurasinya.",
                ],
                stats,
            )

        if stats["streak"] <= 2:
            return _result(
                "INTERMITTENT",
                "Koneksi putus-nyambung",
                "Perangkat baru saja tidak merespons setelah "
                "sebelumnya normal. Gangguan singkat seperti ini "
                "biasanya disebabkan kabel/konektor longgar, port "
                "switch bermasalah, atau perangkat sedang restart.",
                "LOCAL",
                2,
                [
                    "Periksa kabel LAN dan konektor RJ45 pada kedua "
                    "ujung, pastikan terkunci.",
                    "Pindahkan kabel ke port switch lain untuk menguji "
                    "apakah portnya bermasalah.",
                    "Amati 5-10 menit ke depan; jika berulang, "
                    "jadwalkan penggantian kabel.",
                    "Periksa apakah perangkat mengalami restart "
                    "berulang karena suplai listrik tidak stabil.",
                ],
                stats,
            )

        return _result(
            "DEVICE_DOWN",
            "Perangkat mati atau terputus dari jaringan",
            f"Perangkat gagal merespons {stats['streak']} kali "
            "berturut-turut sementara perangkat lain di sekitarnya "
            "masih normal. Ini mengarah pada masalah lokal pada "
            "perangkat itu sendiri.",
            "LOCAL",
            2,
            [
                "Pastikan perangkat menyala dan adaptor listriknya "
                "terpasang.",
                "Periksa kabel LAN dari perangkat ke switch.",
                "Restart perangkat (cabut daya 10 detik, pasang "
                "kembali).",
                "Bila tetap mati setelah restart, lakukan pengecekan "
                "fisik ke lokasi perangkat.",
            ],
            stats,
        )

    # -----------------------------------------------------
    # PERANGKAT LAMBAT
    # -----------------------------------------------------

    if status == "SLOW":

        bottleneck_text = ""

        if trace and trace["bottleneck_ip"]:
            bottleneck_text = (
                f" Pelacakan jalur menunjukkan lonjakan latency "
                f"terbesar pada hop {trace['bottleneck_hop']} "
                f"({trace['bottleneck_label']} - "
                f"{trace['bottleneck_ip']})."
            )

        if stats["avg_loss"] >= 10:
            return _result(
                "PACKET_LOSS",
                "Kehilangan paket (packet loss) tinggi",
                f"Rata-rata {stats['avg_loss']}% paket hilang pada "
                "pengecekan terakhir. Packet loss biasanya "
                "disebabkan kualitas kabel yang menurun, konektor "
                "berkarat, port switch bermasalah, atau interferensi "
                "sinyal pada perangkat nirkabel." + bottleneck_text,
                "LOCAL",
                1,
                [
                    "Ganti kabel LAN yang menghubungkan perangkat dan "
                    "uji kembali.",
                    "Pindahkan koneksi ke port switch yang berbeda.",
                    "Untuk access point, periksa interferensi kanal "
                    "WiFi dan ubah ke kanal yang lebih lengang.",
                    "Periksa apakah ada perangkat yang mengirim "
                    "trafik berlebihan (broadcast storm).",
                ],
                stats,
            )

        if stats["avg_jitter"] >= 20:
            return _result(
                "JITTER",
                "Koneksi tidak stabil (jitter tinggi)",
                f"Variasi waktu respons mencapai {stats['avg_jitter']} "
                "ms. Jaringan masih terhubung namun kualitasnya naik "
                "turun. Umumnya terjadi karena bandwidth dipakai "
                "bersama secara berlebihan atau sinyal nirkabel yang "
                "tidak stabil." + bottleneck_text,
                "SEGMENT" if segment_wide else "LOCAL",
                1,
                [
                    "Periksa penggunaan bandwidth pada jam sibuk "
                    "melalui MikroTik.",
                    "Terapkan pembatasan bandwidth (queue) untuk "
                    "trafik non-prioritas seperti unduhan besar.",
                    "Untuk WiFi, kurangi jumlah pengguna per access "
                    "point atau tambah AP di area padat.",
                    "Pastikan posisi access point tidak terhalang "
                    "dinding beton atau perangkat elektronik lain.",
                ],
                stats,
            )

        if segment_wide:
            return _result(
                "SEGMENT_CONGESTION",
                "Perlambatan merata pada satu area",
                "Beberapa perangkat di area yang sama sama-sama "
                "melambat. Ini mengarah pada kepadatan trafik di "
                "jalur distribusi area tersebut, bukan pada satu "
                "perangkat." + bottleneck_text,
                "SEGMENT",
                1,
                [
                    "Periksa beban trafik pada switch distribusi area "
                    "tersebut.",
                    "Pastikan kabel uplink area menggunakan kecepatan "
                    "yang memadai (minimal gigabit).",
                    "Atur pembagian bandwidth per area pada router "
                    "utama.",
                    "Pertimbangkan memecah area menjadi beberapa "
                    "segmen/VLAN bila jumlah pengguna terus bertambah.",
                ],
                stats,
            )

        return _result(
            "HIGH_LATENCY",
            "Waktu respons di atas batas normal",
            f"Latency rata-rata {stats['avg_latency']} ms, melebihi "
            f"batas wajar {LATENCY_NORMAL} ms untuk jaringan lokal. "
            "Perangkat masih dapat diakses namun terasa lambat oleh "
            "pengguna." + bottleneck_text,
            "PATH" if trace and trace["bottleneck_ip"] else "LOCAL",
            1,
            [
                "Jalankan fitur Lacak Jalur untuk memastikan titik "
                "perlambatan berada di perangkat mana.",
                "Periksa beban CPU dan memori perangkat bila "
                "memungkinkan.",
                "Kurangi jumlah perangkat yang terhubung ke satu "
                "access point.",
                "Jika perlambatan terjadi hanya pada jam tertentu, "
                "kemungkinan besar penyebabnya kepadatan trafik.",
            ],
            stats,
        )

    # -----------------------------------------------------
    # NORMAL
    # -----------------------------------------------------

    if stats["instability"] >= 25:
        return _result(
            "UNSTABLE_HISTORY",
            "Normal, namun riwayatnya tidak stabil",
            f"Saat ini perangkat normal, tetapi "
            f"{stats['instability']}% pemeriksaan terakhir sempat "
            "melambat. Perlu diamati agar gangguan tidak berulang.",
            "LOCAL",
            0,
            [
                "Pantau perangkat ini pada jam sibuk.",
                "Periksa kondisi kabel dan konektor sebagai langkah "
                "pencegahan.",
                "Catat jam terjadinya perlambatan untuk memudahkan "
                "penelusuran pola.",
            ],
            stats,
        )

    return _result(
        "NORMAL",
        "Kondisi normal",
        "Perangkat merespons dengan baik dan latency berada dalam "
        f"batas wajar (di bawah {LATENCY_NORMAL} ms).",
        "NONE",
        0,
        [
            "Tidak ada tindakan yang diperlukan.",
            "Lanjutkan pemantauan rutin.",
        ],
        stats,
    )


def _result(code, title, detail, scope, severity, recommendations, stats):
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "scope": scope,
        "severity": severity,
        "recommendations": recommendations,
        "stats": stats,
    }


def store_diagnosis(connection, device_id, diagnosis):
    connection.execute(
        """
        UPDATE devices SET
            cause_code      = ?,
            cause_title     = ?,
            cause_detail    = ?,
            cause_scope     = ?,
            severity        = ?,
            recommendations = ?
        WHERE id = ?
        """,
        (
            diagnosis["code"],
            diagnosis["title"],
            diagnosis["detail"],
            diagnosis["scope"],
            diagnosis["severity"],
            json.dumps(diagnosis["recommendations"]),
            device_id,
        ),
    )


# ---------------------------------------------------------
# Analisa sebaran titik gangguan
# ---------------------------------------------------------

def hotspots():
    """
    Daftar titik gangguan diurutkan dari yang paling parah,
    lengkap dengan keterangan lokasi fisiknya.
    """

    connection = get_db()

    rows = connection.execute(
        """
        SELECT d.*, p.location AS parent_location,
               t.bottleneck_hop, t.bottleneck_ip, t.bottleneck_label
        FROM devices d
        LEFT JOIN devices p ON d.parent_id = p.id
        LEFT JOIN path_traces t ON t.device_id = d.id
        WHERE d.status IN ('OFFLINE', 'SLOW')
        ORDER BY
            CASE d.status WHEN 'OFFLINE' THEN 0 ELSE 1 END,
            d.latency DESC
        """
    ).fetchall()

    result = []

    for row in rows:
        place = ", ".join(
            value
            for value in (row["building"], row["floor"], row["room"])
            if value
        )

        result.append({
            "id": row["id"],
            "location": row["location"],
            "ip_address": row["ip_address"],
            "device_type": row["device_type"],
            "place": place or "Lokasi fisik belum diisi",
            "building": row["building"],
            "floor": row["floor"],
            "room": row["room"],
            "parent_location": row["parent_location"],
            "status": row["status"],
            "latency": row["latency"],
            "packet_loss": row["packet_loss"],
            "jitter": row["jitter"],
            "cause_title": row["cause_title"],
            "cause_detail": row["cause_detail"],
            "cause_scope": row["cause_scope"],
            "recommendations": json.loads(row["recommendations"] or "[]"),
            "bottleneck_hop": row["bottleneck_hop"],
            "bottleneck_ip": row["bottleneck_ip"],
            "bottleneck_label": row["bottleneck_label"],
            "checked_at": row["checked_at"],
        })

    connection.close()

    return result


def area_summary():
    """Rekap kondisi jaringan per gedung/lantai."""

    connection = get_db()

    rows = connection.execute(
        """
        SELECT
            COALESCE(NULLIF(building, ''), 'Belum dikelompokkan')
                AS area,
            COALESCE(NULLIF(floor, ''), '-') AS lantai,
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'ONLINE'  THEN 1 ELSE 0 END) AS online,
            SUM(CASE WHEN status = 'SLOW'    THEN 1 ELSE 0 END) AS slow,
            SUM(CASE WHEN status = 'OFFLINE' THEN 1 ELSE 0 END) AS offline,
            AVG(latency) AS avg_latency
        FROM devices
        GROUP BY area, lantai
        ORDER BY offline DESC, slow DESC, area ASC
        """
    ).fetchall()

    connection.close()

    return [
        {
            "area": row["area"],
            "lantai": row["lantai"],
            "total": row["total"],
            "online": row["online"],
            "slow": row["slow"],
            "offline": row["offline"],
            "avg_latency": (
                round(row["avg_latency"], 2)
                if row["avg_latency"] is not None
                else None
            ),
            "health": (
                round(row["online"] / row["total"] * 100)
                if row["total"] else 0
            ),
        }
        for row in rows
    ]
