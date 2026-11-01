"""
Mesin pengecekan koneksi (ICMP ping).

Berbeda dengan versi sebelumnya yang hanya mengukur waktu
eksekusi perintah, modul ini membaca langsung nilai RTT dari
keluaran ping sehingga hasilnya jauh lebih akurat, sekaligus
menghitung packet loss dan jitter.
"""

import ipaddress
import os
import platform
import re
import statistics
import subprocess

def _env_number(name, default):
    """Baca angka dari environment variable; pakai default bila kosong/salah."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Ambang batas status (ms). Bisa diubah lewat environment variable,
# misalnya bila server monitoring berada di cloud dan terhubung ke
# jaringan kantor lewat VPN (latency dasarnya lebih tinggi).
LATENCY_NORMAL = _env_number("LATENCY_NORMAL", 30)
LATENCY_SLOW = _env_number("LATENCY_SLOW", 100)

# Jumlah paket per pengecekan
PING_COUNT = 4

IS_WINDOWS = platform.system().lower() == "windows"

RTT_PATTERN = re.compile(
    r"(?:time|waktu)[=<]\s*([\d.,]+)\s*ms",
    re.IGNORECASE,
)


def is_valid_ip(value):
    """True hanya untuk alamat IPv4 yang sah, misalnya 192.168.1.10."""

    if not isinstance(value, str):
        return False

    try:
        ipaddress.IPv4Address(value.strip())
        return True
    except ValueError:
        return False


def _build_command(ip_address):
    if IS_WINDOWS:
        return [
            "ping", "-n", str(PING_COUNT),
            "-w", "1000", ip_address,
        ]

    return [
        "ping", "-c", str(PING_COUNT),
        "-W", "1", "-i", "0.3", ip_address,
    ]


def ping_device(ip_address):
    """
    Kembalikan dict hasil pengukuran:
    latency (rata-rata ms), packet_loss (%), jitter (ms),
    samples (list RTT), reachable (bool).
    """

    empty = {
        "latency": None,
        "packet_loss": 100.0,
        "jitter": None,
        "samples": [],
        "reachable": False,
    }

    # Lindungi dari nilai aneh (mis. "abc" atau berawalan "-")
    # agar tidak pernah diteruskan ke perintah ping.
    if not is_valid_ip(ip_address):
        print(f"[PING SKIP] IP tidak valid: {ip_address!r}")
        return empty

    try:
        result = subprocess.run(
            _build_command(ip_address),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PING_COUNT * 2 + 4,
        )

    except Exception as error:
        print(f"[PING ERROR] {ip_address}: {error}")
        return empty

    output = (result.stdout or "") + (result.stderr or "")

    samples = [
        float(value.replace(",", "."))
        for value in RTT_PATTERN.findall(output)
    ]

    if not samples:
        return empty

    received = len(samples)

    packet_loss = round(
        (PING_COUNT - received) / PING_COUNT * 100, 2
    )

    jitter = (
        round(statistics.pstdev(samples), 2)
        if received > 1
        else 0.0
    )

    return {
        "latency": round(sum(samples) / received, 2),
        "packet_loss": packet_loss,
        "jitter": jitter,
        "samples": samples,
        "reachable": True,
    }


def determine_status(measurement):
    """Tentukan status perangkat berdasarkan hasil pengukuran."""

    if not measurement["reachable"]:
        return "OFFLINE"

    latency = measurement["latency"]
    packet_loss = measurement["packet_loss"] or 0

    # Kehilangan paket besar diperlakukan sebagai gangguan,
    # meskipun sebagian paket masih terjawab.
    if packet_loss >= 50:
        return "OFFLINE"

    if packet_loss > 0 or latency > LATENCY_SLOW:
        return "SLOW"

    if latency <= LATENCY_NORMAL:
        return "ONLINE"

    return "SLOW"