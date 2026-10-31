"""
Pelacakan jalur jaringan (traceroute).

Modul ini menjawab pertanyaan: "kalau jaringan melambat,
melambatnya di titik mana?"

Caranya dengan menelusuri setiap hop (perangkat perantara)
menuju IP tujuan, mengukur latency tiap hop, lalu mencari
lonjakan latency terbesar antar hop. Hop dengan lonjakan
terbesar itulah titik yang memperlambat jalur.
"""

import json
import platform
import re
import subprocess

from .db import get_db, current_time

IS_WINDOWS = platform.system().lower() == "windows"

MAX_HOPS = 15

IP_PATTERN = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

RTT_PATTERN = re.compile(r"([\d.,]+)\s*ms", re.IGNORECASE)

# Lonjakan latency (ms) yang dianggap sebagai titik lambat
DELTA_THRESHOLD = 25.0


def _build_command(ip_address):
    if IS_WINDOWS:
        return [
            "tracert", "-d",
            "-h", str(MAX_HOPS),
            "-w", "1000",
            ip_address,
        ]

    return [
        "traceroute", "-n",
        "-q", "2",
        "-w", "1",
        "-m", str(MAX_HOPS),
        ip_address,
    ]


def _parse_output(output):
    hops = []

    for line in output.splitlines():
        stripped = line.strip()

        if not stripped or not stripped[0].isdigit():
            continue

        hop_number = int(stripped.split()[0])

        ip_match = IP_PATTERN.search(stripped)

        rtt_values = [
            float(value.replace(",", "."))
            for value in RTT_PATTERN.findall(stripped)
        ]

        hops.append({
            "hop": hop_number,
            "ip": ip_match.group(1) if ip_match else None,
            "latency": (
                round(sum(rtt_values) / len(rtt_values), 2)
                if rtt_values
                else None
            ),
            "timeout": not rtt_values,
        })

    return hops


def _label_hops(hops):
    """Cocokkan IP tiap hop dengan perangkat yang terdaftar."""

    connection = get_db()

    rows = connection.execute(
        "SELECT ip_address, location, building, floor, room, device_type "
        "FROM devices"
    ).fetchall()

    connection.close()

    known = {}

    for row in rows:
        parts = [
            value
            for value in (row["building"], row["floor"], row["room"])
            if value
        ]

        known[row["ip_address"]] = {
            "label": row["location"],
            "place": ", ".join(parts) if parts else None,
            "device_type": row["device_type"] or "-",
        }

    for hop in hops:
        info = known.get(hop["ip"])

        if info:
            hop["label"] = info["label"]
            hop["place"] = info["place"]
            hop["device_type"] = info["device_type"]
            hop["registered"] = True

        else:
            hop["label"] = _guess_role(hop)
            hop["place"] = None
            hop["device_type"] = "-"
            hop["registered"] = False

    return hops


def _guess_role(hop):
    """Perkiraan peran hop bila IP-nya belum terdaftar."""

    if not hop["ip"]:
        return "Hop tidak merespons"

    if hop["hop"] == 1:
        return "Gateway / Router lokal"

    if hop["ip"].startswith(("10.", "192.168.", "172.")):
        return "Perangkat jaringan internal"

    return "Jaringan ISP / eksternal"


def analyse_hops(hops):
    """Hitung selisih latency antar hop dan tentukan titik lambat."""

    previous = 0.0
    bottleneck = None

    for hop in hops:
        if hop["latency"] is None:
            hop["delta"] = None
            continue

        delta = round(hop["latency"] - previous, 2)

        hop["delta"] = delta
        previous = hop["latency"]

        if bottleneck is None or delta > bottleneck["delta"]:
            bottleneck = hop

    if bottleneck and bottleneck["delta"] < DELTA_THRESHOLD:
        # Tidak ada lonjakan berarti: jalur relatif sehat.
        return hops, None

    return hops, bottleneck


def trace_device(device_id, ip_address):
    """Jalankan traceroute, simpan hasilnya, kembalikan ringkasan."""

    try:
        result = subprocess.run(
            _build_command(ip_address),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

        output = (result.stdout or "") + (result.stderr or "")

    except FileNotFoundError:
        return {
            "error":
                "Perintah traceroute/tracert tidak tersedia pada "
                "server ini. Pada Linux install dengan "
                "'sudo apt install traceroute'.",
            "hops": [],
        }

    except Exception as error:
        return {
            "error": f"Gagal melacak jalur: {error}",
            "hops": [],
        }

    hops = _label_hops(_parse_output(output))

    hops, bottleneck = analyse_hops(hops)

    summary = {
        "device_id": device_id,
        "target": ip_address,
        "hops": hops,
        "created_at": current_time(),
        "bottleneck": bottleneck,
        "conclusion": _conclusion(hops, bottleneck),
    }

    _save(device_id, summary)

    return summary


def _conclusion(hops, bottleneck):
    if not hops:
        return (
            "Jalur menuju perangkat tidak dapat dipetakan. "
            "Kemungkinan perangkat berada satu segmen dengan server "
            "monitoring atau ICMP diblokir."
        )

    if bottleneck is None:
        return (
            "Tidak ditemukan lonjakan latency yang berarti pada jalur. "
            "Keterlambatan kemungkinan terjadi pada perangkat tujuan "
            "itu sendiri, bukan pada jalur menuju ke sana."
        )

    place = bottleneck.get("place")

    lokasi = f" ({place})" if place else ""

    return (
        f"Titik perlambatan terdeteksi pada hop {bottleneck['hop']} "
        f"- {bottleneck['label']}{lokasi} "
        f"dengan IP {bottleneck['ip'] or 'tidak diketahui'}. "
        f"Latency naik {bottleneck['delta']} ms pada titik ini, "
        "sehingga penanganan sebaiknya dimulai dari perangkat tersebut."
    )


def _save(device_id, summary):
    bottleneck = summary["bottleneck"] or {}

    connection = get_db()

    connection.execute(
        """
        INSERT INTO path_traces
        (device_id, hops_json, bottleneck_hop, bottleneck_ip,
         bottleneck_label, bottleneck_delta, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            hops_json        = excluded.hops_json,
            bottleneck_hop   = excluded.bottleneck_hop,
            bottleneck_ip    = excluded.bottleneck_ip,
            bottleneck_label = excluded.bottleneck_label,
            bottleneck_delta = excluded.bottleneck_delta,
            created_at       = excluded.created_at
        """,
        (
            device_id,
            json.dumps(summary["hops"]),
            bottleneck.get("hop"),
            bottleneck.get("ip"),
            bottleneck.get("label"),
            bottleneck.get("delta"),
            summary["created_at"],
        ),
    )

    connection.commit()
    connection.close()


def last_trace(device_id):
    connection = get_db()

    row = connection.execute(
        "SELECT * FROM path_traces WHERE device_id = ?",
        (device_id,),
    ).fetchone()

    connection.close()

    if not row:
        return None

    hops = json.loads(row["hops_json"] or "[]")

    bottleneck = None

    if row["bottleneck_hop"]:
        bottleneck = next(
            (hop for hop in hops if hop.get("hop") == row["bottleneck_hop"]),
            {
                "hop": row["bottleneck_hop"],
                "ip": row["bottleneck_ip"],
                "label": row["bottleneck_label"],
                "delta": row["bottleneck_delta"],
                "place": None,
            },
        )

    return {
        "device_id": device_id,
        "hops": hops,
        "bottleneck": bottleneck,
        "bottleneck_hop": row["bottleneck_hop"],
        "bottleneck_ip": row["bottleneck_ip"],
        "bottleneck_label": row["bottleneck_label"],
        "bottleneck_delta": row["bottleneck_delta"],
        "conclusion": _conclusion(hops, bottleneck),
        "created_at": row["created_at"],
    }
