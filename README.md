# Sistem Monitoring Jaringan — Diskominfo

Prototype sistem monitoring jaringan untuk memantau router, switch, dan
access point berdasarkan IP Address. Sistem memeriksa perangkat secara
berkala, **menunjukkan di titik mana jaringan melambat**, **menjelaskan
dugaan penyebabnya beserta saran penanganan**, dan menghasilkan
**laporan PDF/Excel siap cetak**.

> Dikembangkan sebagai bagian dari proyek
> *Rancang Bangun Prototype Sistem Monitoring Jaringan pada Dinas Kominfo*.

---

## Daftar Isi

1. [Fitur Utama](#fitur-utama)
2. [Teknologi](#teknologi)
3. [Cara Setup](#cara-setup)
4. [Menjalankan Program](#menjalankan-program)
5. [Panduan Penggunaan](#panduan-penggunaan)
6. [Struktur Proyek](#struktur-proyek)
7. [Daftar API](#daftar-api)
8. [Konfigurasi](#konfigurasi)
9. [Troubleshooting](#troubleshooting)
10. [Catatan Penting](#catatan-penting)

---

## Fitur Utama

### 1. Lokasi titik gangguan, bukan sekadar IP merah

Setiap perangkat kini menyimpan **gedung, lantai, ruangan, jenis
perangkat, dan perangkat induk (uplink)**. Saat sebuah IP berstatus
merah, dashboard langsung menyebutkan letak fisiknya:

```
AP Ruang Pelayanan  •  TERPUTUS
📍 Gedung B, Lantai 1, Ruang Pelayanan
🌐 192.168.1.11    ⬆️ Induk: Switch Distribusi Lt.1
```

Halaman **Titik Gangguan** mengurutkan seluruh perangkat bermasalah dari
yang paling parah, dan tabel **Kondisi per Area** menunjukkan sebaran
gangguan per gedung/lantai.

### 2. Pelacakan jalur (traceroute) — "melambatnya di hop mana?"

Sistem menelusuri setiap hop menuju perangkat tujuan, mengukur latency
tiap hop, lalu mencari **lonjakan latency terbesar antar hop**. Hop
dengan lonjakan terbesar ditandai merah sebagai titik perlambatan. IP
hop yang sudah terdaftar otomatis diterjemahkan menjadi nama dan lokasi
perangkat.

```
Hop 1  Router Utama MikroTik   192.168.1.1   Gedung A, Lt.1, R. Server    2.1 ms  (+2.1)
Hop 2  Switch Distribusi Lt.1  192.168.1.2   Gedung A, Lt.1, R. Panel    64.6 ms  (+62.5)  ← titik lambat
Hop 3  AP Ruang Rapat          192.168.1.10  Gedung A, Lt.2, R. Rapat   148.6 ms  (+84.0)
```

### 3. Analisa penyebab dan saran penanganan otomatis

Mesin diagnosa membaca 20 pemeriksaan terakhir (latency, packet loss,
jitter, jumlah kegagalan beruntun) ditambah kondisi perangkat induk dan
perangkat tetangga, lalu menyimpulkan salah satu pola berikut:

| Kode | Dugaan penyebab |
|---|---|
| `UPLINK_DOWN` | Gangguan pada perangkat induk (uplink) |
| `SEGMENT_DOWN` | Gangguan menyebar pada satu segmen/area |
| `NEVER_REACHED` | Belum pernah terjangkau (IP salah / ICMP diblokir) |
| `INTERMITTENT` | Koneksi putus-nyambung (kabel/konektor/port) |
| `DEVICE_DOWN` | Perangkat mati atau terputus |
| `PACKET_LOSS` | Kehilangan paket tinggi |
| `JITTER` | Koneksi tidak stabil |
| `SEGMENT_CONGESTION` | Perlambatan merata satu area |
| `HIGH_LATENCY` | Waktu respons di atas batas normal |
| `UNSTABLE_HISTORY` | Normal, tetapi riwayatnya tidak stabil |
| `NORMAL` | Kondisi normal |

Setiap dugaan disertai penjelasan dan 3–4 langkah penanganan konkret
dalam bahasa yang bisa dipahami petugas non-teknis.

### 4. Laporan PDF, Excel, dan CSV

- **PDF** — A4 landscape, kop instansi, kotak ringkasan, tabel rekap
  berwarna, bagian analisa penyebab per perangkat, catatan, nomor
  halaman, dan kolom tanda tangan. Tinggal `Ctrl + P`.
- **Excel (.xlsx)** — berwarna, freeze pane, filter otomatis, dua
  lembar: *Rekap Harian* dan *Analisa & Saran*.
- **CSV** — untuk diolah kembali.

### 5. Konversi berkas (CSV → Excel / PDF)

Halaman **Konversi Berkas** menerima file `.csv` atau `.xlsx`
(drag-and-drop) dan mengubahnya menjadi Excel rapi atau PDF siap cetak.
Pemisah kolom (`,` `;` tab `|`) dideteksi otomatis, dan teks berupa
angka dikonversi menjadi numerik agar bisa dihitung di Excel.

### 6. Pengukuran yang lebih akurat

Versi sebelumnya mengukur waktu eksekusi perintah ping. Versi ini
membaca **nilai RTT langsung dari keluaran ping**, mengirim 4 paket per
pemeriksaan, dan menghitung **packet loss** serta **jitter**.

---

## Teknologi

| Lapisan | Teknologi |
|---|---|
| Backend | Python 3 + Flask (REST API) |
| Frontend | **Vue 3** (Composition API, SPA) + **Chart.js** |
| Styling | Design system CSS sendiri (CSS variables, mode gelap) |
| Database | SQLite (WAL mode) |
| Laporan | openpyxl (Excel) + ReportLab (PDF) |
| Pemeriksaan | ICMP ping + traceroute/tracert |

Library Vue dan Chart.js **disertakan di dalam proyek**
(`static/vendor/`), jadi aplikasi tetap berjalan tanpa koneksi internet.
Tidak ada proses build — tidak perlu Node.js atau npm.

---

## Cara Setup

### Prasyarat

- **Python 3.9 atau lebih baru**
- **pip**
- Perintah `ping` (bawaan Windows dan Linux)
- Perintah `tracert` (Windows, bawaan) atau `traceroute` (Linux)

Cek versi Python:

```bash
python --version
```

Jika perintah `python` tidak dikenali, coba `py --version` (Windows) atau
`python3 --version` (Linux/macOS).

### Langkah 1 — Masuk ke folder proyek

```bash
cd network-monitoring
```

### Langkah 2 — Buat virtual environment (disarankan)

**Windows:**

```bash
python -m venv venv
venv\Scripts\activate
```

**Linux / macOS:**

```bash
python3 -m venv venv
source venv/bin/activate
```

### Langkah 3 — Install dependensi

```bash
pip install -r requirements.txt
```

Isi `requirements.txt`:

```
Flask==3.1.1
openpyxl==3.1.5
reportlab==4.2.5
```

### Langkah 4 — Install traceroute (khusus Linux)

Windows sudah membawa `tracert` secara bawaan. Untuk Linux:

```bash
sudo apt update && sudo apt install traceroute
```

Tanpa ini, seluruh fitur lain tetap berjalan; hanya tombol
*Lacak Jalur* yang akan menampilkan pesan bahwa perintahnya tidak
tersedia.

### Langkah 5 — (Opsional) Set secret key

Untuk penggunaan jangka panjang agar sesi login tidak hilang saat server
di-restart:

**Windows:**

```bash
set SECRET_KEY=kunci-rahasia-anda
```

**Linux / macOS:**

```bash
export SECRET_KEY=kunci-rahasia-anda
```

---

## Menjalankan Program

```bash
python app.py
```

Keluaran yang diharapkan:

```
==========================================================
        NETWORK MONITORING - DISKOMINFO
==========================================================

[SETUP] Akun admin default dibuat -> admin / admin123 (segera ganti password ini).
[MONITORING] Interval = 10 detik

Dashboard           : http://127.0.0.1:5000
Interval monitoring : 10 detik
Batas latency normal: 30 ms

Tekan CTRL+C untuk menghentikan server.
```

Buka **http://127.0.0.1:5000** di browser.

**Akun bawaan:** `admin` / `admin123` — segera ganti setelah login
pertama.

Database `monitoring.db` dibuat otomatis jika belum ada. Jika Anda
memakai database dari versi lama, kolom-kolom baru **ditambahkan otomatis
tanpa menghapus data** (lihat log `[MIGRASI]` saat pertama dijalankan).

### Agar bisa diakses dari komputer lain

Buka `app.py`, ubah:

```python
HOST = "0.0.0.0"
```

Lalu akses melalui `http://<ip-server>:5000` dan pastikan port 5000
diizinkan di firewall.

---

## Panduan Penggunaan

### Menambahkan perangkat

**Perangkat → + Tambah perangkat**, lalu isi:

| Field | Contoh | Keterangan |
|---|---|---|
| Nama perangkat * | AP Ruang Rapat | wajib |
| IP Address * | 192.168.1.10 | wajib, harus unik |
| Jenis perangkat | Access Point | Router/Switch/AP/Server/CCTV/… |
| Perangkat induk | Switch Distribusi Lt.1 | uplink perangkat ini |
| Gedung / Area | Gedung A | |
| Lantai | Lantai 2 | |
| Ruangan | Ruang Rapat | |
| Keterangan | Dekat pintu masuk | |

> **Penting:** mengisi gedung/lantai/ruangan dan perangkat induk adalah
> kunci dari fitur lokasi gangguan. Tanpa itu sistem tidak bisa
> membedakan "satu perangkat bermasalah" dengan "satu area bermasalah",
> dan tidak bisa menyebutkan letak fisik titik merah.

Urutan pengisian yang disarankan: router utama dulu (tanpa induk), lalu
switch (induk = router), lalu AP/CCTV (induk = switch).

### Melacak titik perlambatan

**Perangkat → Detail → Lacak jalur sekarang**, atau dari halaman
**Titik Gangguan → Detail & lacak jalur**. Proses memerlukan beberapa
detik. Hop dengan latar merah adalah titik perlambatan.

### Mencetak laporan

**Laporan → pilih tanggal → PDF siap cetak.** PDF terbuka di tab baru,
tekan `Ctrl + P`. Tombol *Excel* dan *CSV* mengunduh berkas langsung.

### Mengonversi berkas

**Konversi Berkas →** letakkan file CSV/XLSX → pilih format tujuan →
*Konversi sekarang*.

### Lain-lain

- **Notifikasi**: tombol di sidebar; saat aktif, perangkat yang baru
  terputus memunculkan notifikasi di pojok kanan bawah.
- **Mode gelap**: tombol di sidebar, tersimpan di browser.
- **Pencarian**: kolom cari di halaman Perangkat mencakup nama, IP,
  gedung, lantai, dan ruangan.

---

## Struktur Proyek

```
network-monitoring/
├── app.py                      # Flask: route, REST API, autentikasi
├── requirements.txt
├── monitoring.db               # SQLite (dibuat/dimigrasi otomatis)
│
├── monitoring/                 # Modul inti
│   ├── __init__.py
│   ├── db.py                   # Koneksi, skema, migrasi otomatis
│   ├── pinger.py               # Ping: RTT, packet loss, jitter, status
│   ├── tracer.py               # Traceroute + deteksi hop penyebab lambat
│   ├── diagnostics.py          # Analisa penyebab + saran + rekap area
│   ├── monitor.py              # Loop monitoring latar belakang (paralel)
│   └── reports.py              # Laporan CSV / XLSX / PDF + konverter
│
├── templates/
│   ├── index.html              # Kerangka SPA (template Vue)
│   └── login.html              # Halaman login
│
└── static/
    ├── css/app.css             # Design system
    ├── js/app.js               # Aplikasi Vue 3
    └── vendor/
        ├── vue.global.prod.js  # Vue 3 (offline)
        └── chart.umd.js        # Chart.js (offline)
```

### Skema database

**devices** — `id`, `location`, `ip_address`, `description`,
`device_type`, `building`, `floor`, `room`, `parent_id`, `status`,
`latency`, `packet_loss`, `jitter`, `checked_at`, `cause_code`,
`cause_title`, `cause_detail`, `cause_scope`, `recommendations`,
`severity`

**monitoring_logs** — `id`, `device_id`, `status`, `latency`,
`packet_loss`, `jitter`, `checked_at`

**path_traces** — `device_id`, `hops_json`, `bottleneck_hop`,
`bottleneck_ip`, `bottleneck_label`, `bottleneck_delta`, `created_at`

**users** — `id`, `username`, `password_hash`, `created_at`

---

## Daftar API

Seluruh endpoint memerlukan sesi login.

| Method | Endpoint | Keterangan |
|---|---|---|
| GET | `/api/devices` | Daftar perangkat + status + dugaan penyebab |
| POST | `/api/devices` | Tambah perangkat |
| PUT | `/api/devices/<id>` | Ubah perangkat |
| DELETE | `/api/devices/<id>` | Hapus perangkat beserta riwayatnya |
| GET | `/api/summary` | Ringkasan total/online/lambat/offline |
| GET | `/api/hotspots` | Titik gangguan + lokasi fisik + saran |
| GET | `/api/areas` | Rekap kondisi per gedung/lantai |
| GET | `/api/devices/<id>/diagnosis` | Analisa penyebab satu perangkat |
| POST | `/api/devices/<id>/trace` | Jalankan traceroute |
| GET | `/api/devices/<id>/trace` | Hasil traceroute terakhir |
| GET | `/api/devices/<id>/logs?limit=` | Riwayat satu perangkat |
| GET | `/api/logs?limit=&status=` | Riwayat semua perangkat |
| GET | `/api/report/preview?date=` | Pratinjau laporan (JSON) |
| GET | `/api/report/daily?date=&format=` | Unduh laporan (`csv`/`xlsx`/`pdf`) |
| POST | `/api/tools/convert` | Konversi berkas (form-data: `file`, `target`, `title`) |
| POST | `/api/account/password` | Ganti password |

---

## Konfigurasi

**`app.py`**

```python
HOST = "127.0.0.1"   # "0.0.0.0" agar bisa diakses dari jaringan
PORT = 5000
```

**`monitoring/monitor.py`**

```python
MONITOR_INTERVAL = 10     # jeda antar siklus pemeriksaan (detik)
MAX_WORKERS      = 12     # jumlah ping paralel
LOG_RETENTION    = 2000   # log tersimpan per perangkat
```

**`monitoring/pinger.py`**

```python
LATENCY_NORMAL = 30       # <= 30 ms  -> ONLINE
LATENCY_SLOW   = 100      # <= 100 ms -> SLOW, di atas itu dianggap gangguan
PING_COUNT     = 4        # paket per pemeriksaan
```

**`monitoring/tracer.py`**

```python
MAX_HOPS        = 15
DELTA_THRESHOLD = 25.0    # lonjakan (ms) yang dianggap titik lambat
```

**`monitoring/reports.py`**

```python
INSTANSI     = "Dinas Komunikasi dan Informatika"
SUB_INSTANSI = "Sistem Monitoring Jaringan"
```

---

## Troubleshooting

| Masalah | Penyebab & solusi |
|---|---|
| `ModuleNotFoundError: No module named 'flask'` | Virtual environment belum aktif atau dependensi belum diinstall. Jalankan `pip install -r requirements.txt`. |
| Semua perangkat OFFLINE padahal hidup | Firewall perangkat memblokir ICMP, atau server monitoring berada di subnet yang berbeda. Uji dengan `ping <ip>` dari terminal server. |
| Tombol *Lacak jalur* menampilkan pesan error | `traceroute` belum terpasang (Linux). Install dengan `sudo apt install traceroute`. |
| PDF tidak terbuka | Pop-up diblokir browser. Izinkan pop-up untuk alamat dashboard. |
| Tampilan berantakan / halaman kosong | Cache browser. Tekan `Ctrl + Shift + R`. |
| `Address already in use` | Port 5000 dipakai proses lain. Ubah `PORT` di `app.py`. |
| Lupa password admin | Hentikan server, hapus baris user dari tabel `users` di `monitoring.db`, jalankan ulang — akun default dibuat kembali. |
| Monitoring terasa berat | Perbesar `MONITOR_INTERVAL` atau kecilkan `PING_COUNT`. |

---

## Catatan Penting

Status `OFFLINE` berarti **IP tujuan tidak membalas ICMP dari server
monitoring** — tidak selalu berarti perangkat rusak atau internet mati.
Kemungkinan penyebab: perangkat mati, kabel terputus, firewall memblokir
ICMP, perubahan konfigurasi, atau masalah routing antar jaringan.

Dugaan penyebab dan saran penanganan pada sistem ini disusun otomatis
dari **pola data ping** (latency, packet loss, jitter, kondisi perangkat
induk dan tetangga). Hasilnya bersifat **dugaan** dan tetap memerlukan
verifikasi di lapangan sebelum tindakan perbaikan dilakukan.

Prototype ini menggunakan server pengembangan bawaan Flask. Untuk
penggunaan produksi jangka panjang, disarankan dijalankan melalui WSGI
server seperti Waitress (Windows) atau Gunicorn (Linux), dengan
`SECRET_KEY` yang diset permanen.

---

## Pengembangan Selanjutnya

- Monitoring bandwidth dan penggunaan CPU/memori via SNMP
- Notifikasi melalui Telegram / WhatsApp / Email
- Monitoring real-time dengan WebSocket
- Multi-user dengan peran (admin / operator / pemantau)
- Peta denah gedung dengan penanda titik gangguan
- Agent monitoring pada lokasi terpisah

---

**Status:** Prototype / Development
**Instansi:** Dinas Komunikasi dan Informatika (Diskominfo)
