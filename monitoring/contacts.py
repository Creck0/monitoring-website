"""
Data teknisi (nama + nomor HP) dan validasinya.

Aturan nomor HP: hanya boleh berisi angka 0-9 (tanpa spasi,
tanda plus, atau tanda hubung), panjang 9 sampai 15 digit.
"""
import re

from .db import current_time

PHONE_PATTERN = re.compile(r"[0-9]{9,15}", re.ASCII)
NAME_MAX_LENGTH = 60

PHONE_ERROR = (
    "No. HP hanya boleh berisi angka (tanpa spasi, tanda + atau -) "
    "sepanjang 9 sampai 15 digit."
)


def wa_number(phone):
    """
    Ubah nomor lokal menjadi format internasional tanpa tanda plus,
    sebagaimana dibutuhkan tautan WhatsApp (wa.me).
        0812xxxx  -> 62812xxxx
        812xxxx   -> 62812xxxx
        62812xxxx -> 62812xxxx
    """
    if phone.startswith("62"):
        return phone
    if phone.startswith("0"):
        return "62" + phone[1:]
    if phone.startswith("8"):
        return "62" + phone
    return phone


def validate_technician(data):
    """Kembalikan (nama, nomor_hp, pesan_error)."""
    name = " ".join(str(data.get("name") or "").split())
    phone = str(data.get("phone") or "").strip()

    if not name:
        return None, None, "Nama teknisi wajib diisi."
    if len(name) > NAME_MAX_LENGTH:
        return None, None, (
            f"Nama teknisi maksimal {NAME_MAX_LENGTH} karakter."
        )
    if not phone:
        return None, None, "No. HP wajib diisi."
    if not PHONE_PATTERN.fullmatch(phone):
        return None, None, PHONE_ERROR

    return name, phone, None


def serialize(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "wa_number": wa_number(row["phone"]),
    }


def list_technicians(connection):
    rows = connection.execute(
        "SELECT id, name, phone FROM technicians ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [serialize(row) for row in rows]


def is_duplicate(connection, phone, ignore_id=None):
    """Cek apakah nomor (setelah dinormalkan) sudah terdaftar."""
    target = wa_number(phone)
    for row in connection.execute(
        "SELECT id, phone FROM technicians"
    ).fetchall():
        if ignore_id is not None and row["id"] == ignore_id:
            continue
        if wa_number(row["phone"]) == target:
            return True
    return False


def create_technician(connection, name, phone):
    cursor = connection.execute(
        "INSERT INTO technicians (name, phone, created_at) VALUES (?, ?, ?)",
        (name, phone, current_time()),
    )
    connection.commit()
    return cursor.lastrowid