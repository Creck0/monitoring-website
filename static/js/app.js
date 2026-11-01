/* =========================================================
   NETWORK MONITORING - FRONTEND (Vue 3 + Chart.js)
   ========================================================= */

const { createApp, ref, computed, onMounted, onBeforeUnmount, watch, nextTick } = Vue;

/* ---------- Helper ---------- */

async function api(url, options = {}) {
    const config = Object.assign({ headers: {} }, options);

    if (config.body && !(config.body instanceof FormData)) {
        config.headers["Content-Type"] = "application/json";
        config.body = JSON.stringify(config.body);
    }

    const response = await fetch(url, config);

    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Sesi berakhir");
    }

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(data.error || "Terjadi kesalahan pada server.");
    }

    return data;
}

function statusClass(status) {
    return {
        ONLINE: "badge-online",
        SLOW: "badge-slow",
        OFFLINE: "badge-offline"
    }[status] || "badge-unknown";
}

function statusText(status) {
    return {
        ONLINE: "Normal",
        SLOW: "Lambat",
        OFFLINE: "Terputus"
    }[status] || "Belum dicek";
}

function formatMs(value) {
    return value === null || value === undefined ? "-" : `${value} ms`;
}

const DEVICE_TYPES = [
    "Router", "Switch", "Access Point", "Server", "CCTV", "PC/Client", "Lainnya"
];

/* =========================================================
   KOMPONEN: Diagnosa (penyebab + saran)
   ========================================================= */

const CauseBox = {
    props: ["title", "detail", "scope", "recommendations", "compact"],
    template: `
      <div class="cause-box">
        <div class="cause-title">
          Dugaan penyebab: {{ title || 'Belum dianalisa' }}
          <span v-if="scope && scope !== 'NONE'" class="chip"
                style="margin-left:6px">{{ scopeLabel }}</span>
        </div>
        <div style="color:var(--muted)">{{ detail }}</div>
        <div v-if="recommendations && recommendations.length"
             style="margin-top:8px">
          <b style="font-size:12.5px">Saran penanganan:</b>
          <ol class="suggestions">
            <li v-for="(item, index) in visible" :key="index">{{ item }}</li>
          </ol>
        </div>
      </div>
    `,
    computed: {
        visible() {
            if (!this.recommendations) return [];
            return this.compact
                ? this.recommendations.slice(0, 2)
                : this.recommendations;
        },
        scopeLabel() {
            return {
                LOCAL: "Masalah lokal perangkat",
                SEGMENT: "Masalah satu area",
                UPLINK: "Masalah perangkat induk",
                PATH: "Masalah di jalur"
            }[this.scope] || this.scope;
        }
    }
};

/* =========================================================
   APLIKASI UTAMA
   ========================================================= */

createApp({
    components: { CauseBox },

    setup() {
        /* ---------- State ---------- */

        const view = ref("dashboard");
        const sidebarOpen = ref(false);
        const theme = ref(localStorage.getItem("netmon-theme") || "light");

        const devices = ref([]);
        const summary = ref({});
        const spots = ref([]);
        const areas = ref([]);
        const logs = ref([]);
        const toasts = ref([]);

        const loading = ref(true);
        const search = ref("");
        const logFilter = ref("");
        const alertOn = ref(localStorage.getItem("netmon-alert") === "1");

        const showForm = ref(false);
        const editing = ref(null);
        const form = ref(blankForm());

        const detail = ref(null);
        const detailLogs = ref([]);
        const detailLoading = ref(false);
        const detailError = ref("");
        const detailDiagnosis = ref(null);
        const trace = ref(null);
        const tracing = ref(false);

        const reportDate = ref(new Date().toISOString().slice(0, 10));
        const reportPreview = ref(null);
        const reportLoading = ref(false);

        const convertFile = ref(null);
        const convertTarget = ref("xlsx");
        const convertTitle = ref("");
        const converting = ref(false);
        const dragging = ref(false);

        /* Teknisi */
        const technicians = ref([]);
        const showTechForm = ref(false);
        const editingTech = ref(null);
        const techForm = ref({ name: "", phone: "" });
        const savingTech = ref(false);
        const contactDevice = ref(null);

        /* Bot Telegram */
        const tg = ref({
            enabled: false, has_token: false, token_masked: "",
            token_source: "", chat_id: "",
            notify_slow: false, notify_recovery: true,
            repeat_seconds: 30
        });
        const tgForm = ref({
            token: "", chat_id: "", enabled: false,
            notify_slow: false, notify_recovery: true,
            repeat_seconds: 30
        });
        const tgBusy = ref("");
        const detectedChats = ref([]);

        let chart = null;
        let timer = null;

        function blankForm() {
            return {
                location: "", ip_address: "", description: "",
                device_type: "Access Point", building: "",
                floor: "", room: "", parent_id: ""
            };
        }

        /* ---------- Toast ---------- */

        function toast(message, type = "success") {
            const id = Date.now() + Math.random();
            toasts.value.push({ id, message, type });
            setTimeout(() => {
                toasts.value = toasts.value.filter(t => t.id !== id);
            }, 3800);
        }

        /* ---------- Pemuatan data ---------- */

        async function refresh(silent = true) {
            try {
                const [d, s, h, a] = await Promise.all([
                    api("/api/devices"),
                    api("/api/summary"),
                    api("/api/hotspots"),
                    api("/api/areas")
                ]);

                const previousOffline = spots.value
                    .filter(x => x.status === "OFFLINE")
                    .map(x => x.id);

                devices.value = d;
                summary.value = s;
                spots.value = h;
                areas.value = a;

                if (alertOn.value && !silent) return;

                if (alertOn.value) {
                    h.filter(x => x.status === "OFFLINE" &&
                        !previousOffline.includes(x.id))
                     .forEach(x => toast(
                        `${x.location} (${x.ip_address}) terputus - ${x.place}`,
                        "error"
                     ));
                }
            } catch (error) {
                if (!silent) toast(error.message, "error");
            } finally {
                loading.value = false;
            }
        }

        async function loadLogs() {
            try {
                logs.value = await api(
                    `/api/logs?limit=150&status=${logFilter.value}`
                );
            } catch (error) {
                toast(error.message, "error");
            }
        }

        /* ---------- CRUD perangkat ---------- */

        function openCreate() {
            editing.value = null;
            form.value = blankForm();
            showForm.value = true;
        }

        function openEdit(device) {
            editing.value = device.id;
            form.value = {
                location: device.location || "",
                ip_address: device.ip_address || "",
                description: device.description || "",
                device_type: device.device_type || "Access Point",
                building: device.building || "",
                floor: device.floor || "",
                room: device.room || "",
                parent_id: device.parent_id || ""
            };
            showForm.value = true;
        }

        async function saveDevice() {
            try {
                if (editing.value) {
                    await api(`/api/devices/${editing.value}`, {
                        method: "PUT", body: form.value
                    });
                    toast("Perangkat berhasil diperbarui.");
                } else {
                    await api("/api/devices", {
                        method: "POST", body: form.value
                    });
                    toast("Perangkat berhasil ditambahkan.");
                }

                showForm.value = false;
                await refresh(false);
            } catch (error) {
                toast(error.message, "error");
            }
        }

        async function removeDevice(device) {
            if (!confirm(
                `Hapus "${device.location}" beserta seluruh riwayatnya?`
            )) return;

            try {
                await api(`/api/devices/${device.id}`, { method: "DELETE" });
                toast("Perangkat dihapus.");
                if (detail.value && detail.value.id === device.id) {
                    closeDetail();
                }
                await refresh(false);
            } catch (error) {
                toast(error.message, "error");
            }
        }

        /* ---------- Detail perangkat ---------- */

        async function openDetail(device) {
            detail.value = device;
            detailLogs.value = [];
            detailDiagnosis.value = null;
            detailError.value = "";
            detailLoading.value = true;
            trace.value = null;

            if (chart) {
                chart.destroy();
                chart = null;
            }

            /* Riwayat dan analisa dimuat TERPISAH: kegagalan salah satunya
               tidak boleh membuat grafik ikut kosong. */
            const [logsResult, diagnosisResult] = await Promise.allSettled([
                api(`/api/devices/${device.id}/logs?limit=60`),
                api(`/api/devices/${device.id}/diagnosis`)
            ]);

            /* Jendela sudah ditutup / diganti perangkat lain saat memuat */
            if (!detail.value || detail.value.id !== device.id) return;

            detailLoading.value = false;

            if (logsResult.status === "fulfilled") {
                detailLogs.value = logsResult.value.slice().reverse();
                await nextTick();
                if (detailLogs.value.length) drawChart();
            } else {
                detailError.value = logsResult.reason?.message ||
                    "Terjadi kesalahan pada server.";
            }

            if (diagnosisResult.status === "fulfilled") {
                detailDiagnosis.value = diagnosisResult.value;
            } else {
                toast(
                    "Analisa penyebab gagal dimuat: " +
                    (diagnosisResult.reason?.message || "kesalahan server"),
                    "error"
                );
            }

            trace.value = await api(
                `/api/devices/${device.id}/trace`
            ).catch(() => null);
        }

        function closeDetail() {
            detail.value = null;
            if (chart) { chart.destroy(); chart = null; }
        }

        async function runTrace() {
            if (!detail.value) return;

            tracing.value = true;

            try {
                trace.value = await api(
                    `/api/devices/${detail.value.id}/trace`,
                    { method: "POST" }
                );
                toast("Pelacakan jalur selesai.");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tracing.value = false;
            }
        }

        function drawChart() {
            const canvas = document.getElementById("latencyChart");
            if (!canvas) return;

            if (chart) chart.destroy();

            const dark = theme.value === "dark";
            const gridColor = dark ? "#2a3a57" : "#e5e9ef";
            const tickColor = dark ? "#a9b8d1" : "#53627a";

            chart = new Chart(canvas.getContext("2d"), {
                type: "line",
                data: {
                    labels: detailLogs.value.map(l => l.checked_at.slice(11, 19)),
                    datasets: [{
                        label: "Latency (ms)",
                        data: detailLogs.value.map(l => l.latency),
                        borderColor: "#2255ad",
                        backgroundColor: "rgba(34, 85, 173, .10)",
                        fill: true,
                        tension: .35,
                        spanGaps: false,
                        pointRadius: 2,
                        pointBackgroundColor: detailLogs.value.map(l =>
                            l.status === "OFFLINE" ? "#dc2626"
                                : l.status === "SLOW" ? "#ea580c" : "#16a34a"
                        )
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                afterLabel: ctx => {
                                    const log = detailLogs.value[ctx.dataIndex];
                                    return [
                                        `Status: ${statusText(log.status)}`,
                                        `Packet loss: ${log.packet_loss ?? 0}%`,
                                        `Jitter: ${log.jitter ?? 0} ms`
                                    ];
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: gridColor },
                            ticks: { maxTicksLimit: 8, color: tickColor }
                        },
                        y: {
                            beginAtZero: true,
                            grid: { color: gridColor },
                            ticks: { color: tickColor },
                            title: { display: true, text: "ms", color: tickColor }
                        }
                    }
                }
            });
        }

        /* ---------- Laporan ---------- */

        async function loadPreview() {
            reportLoading.value = true;

            try {
                reportPreview.value = await api(
                    `/api/report/preview?date=${reportDate.value}`
                );
            } catch (error) {
                toast(error.message, "error");
            } finally {
                reportLoading.value = false;
            }
        }

        function download(format) {
            const url =
                `/api/report/daily?date=${reportDate.value}&format=${format}`;

            if (format === "pdf") {
                window.open(url, "_blank");
            } else {
                window.location.href = url;
            }
        }

        /* ---------- Konversi berkas ---------- */

        function pickFile(event) {
            const file = event.target.files[0] || null;
            convertFile.value = file;
            if (file && !convertTitle.value) {
                convertTitle.value = file.name
                    .replace(/\.[^.]+$/, "")
                    .replace(/[-_]/g, " ");
            }
        }

        function dropFile(event) {
            dragging.value = false;
            const file = event.dataTransfer.files[0];
            if (file) {
                convertFile.value = file;
                convertTitle.value = file.name.replace(/\.[^.]+$/, "");
            }
        }

        async function runConvert() {
            if (!convertFile.value) {
                toast("Pilih berkas terlebih dahulu.", "error");
                return;
            }

            converting.value = true;

            const data = new FormData();
            data.append("file", convertFile.value);
            data.append("target", convertTarget.value);
            data.append("title", convertTitle.value);

            try {
                const response = await fetch("/api/tools/convert", {
                    method: "POST", body: data
                });

                if (!response.ok) {
                    const error = await response.json().catch(() => ({}));
                    throw new Error(error.error || "Konversi gagal.");
                }

                const blob = await response.blob();
                const url = URL.createObjectURL(blob);

                if (convertTarget.value === "pdf") {
                    window.open(url, "_blank");
                } else {
                    const link = document.createElement("a");
                    link.href = url;
                    link.download =
                        convertFile.value.name.replace(/\.[^.]+$/, "") +
                        "." + convertTarget.value;
                    link.click();
                }

                setTimeout(() => URL.revokeObjectURL(url), 30000);
                toast("Konversi selesai.");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                converting.value = false;
            }
        }

        /* ---------- Tema ---------- */

        function toggleTheme() {
            theme.value = theme.value === "dark" ? "light" : "dark";
            localStorage.setItem("netmon-theme", theme.value);
            document.documentElement.dataset.theme = theme.value;
            if (chart) drawChart();
        }

        function toggleAlert() {
            alertOn.value = !alertOn.value;
            localStorage.setItem("netmon-alert", alertOn.value ? "1" : "0");
            toast(alertOn.value
                ? "Notifikasi gangguan diaktifkan."
                : "Notifikasi gangguan dimatikan.");
        }

        /* ---------- Computed ---------- */

        const filtered = computed(() => {
            const keyword = search.value.toLowerCase().trim();
            if (!keyword) return devices.value;

            return devices.value.filter(device =>
                [device.location, device.ip_address, device.building,
                 device.floor, device.room, device.device_type]
                    .filter(Boolean)
                    .some(value => value.toLowerCase().includes(keyword))
            );
        });

        const offlineCount = computed(() =>
            spots.value.filter(s => s.status === "OFFLINE").length
        );

        const healthScore = computed(() => {
            const total = summary.value.total || 0;
            if (!total) return 100;
            return Math.round((summary.value.online || 0) / total * 100);
        });

        const parents = computed(() =>
            devices.value.filter(d => d.id !== editing.value)
        );

        /* ---------- Teknisi ---------- */

        async function loadTechnicians() {
            try {
                technicians.value = await api("/api/technicians");
            } catch (error) {
                toast(error.message, "error");
            }
        }

        function sanitizePhone() {
            /* \D hanya cocok dengan selain 0-9 sehingga huruf, spasi,
               tanda +/- dan angka non-latin ikut terbuang. */
            techForm.value.phone = String(techForm.value.phone || "")
                .replace(/\D/g, "")
                .slice(0, 15);
        }

        function onlyDigitsKey(event) {
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            if (event.key.length === 1 && !/^[0-9]$/.test(event.key)) {
                event.preventDefault();
            }
        }

        const techFormValid = computed(() =>
            techForm.value.name.trim().length > 0 &&
            /^[0-9]{9,15}$/.test(techForm.value.phone)
        );

        const phoneHint = computed(() => {
            const length = techForm.value.phone.length;
            if (!length) return "Hanya angka, 9 sampai 15 digit. Contoh: 081234567890";
            if (length < 9) return `Kurang ${9 - length} digit lagi (minimal 9 digit).`;
            return `${length} digit`;
        });

        function openTechCreate() {
            editingTech.value = null;
            techForm.value = { name: "", phone: "" };
            showTechForm.value = true;
        }

        function openTechEdit(tech) {
            editingTech.value = tech.id;
            techForm.value = { name: tech.name, phone: tech.phone };
            showTechForm.value = true;
        }

        async function saveTech() {
            if (!techFormValid.value || savingTech.value) return;
            savingTech.value = true;

            try {
                if (editingTech.value) {
                    await api(`/api/technicians/${editingTech.value}`, {
                        method: "PUT", body: techForm.value
                    });
                    toast("Data teknisi diperbarui.");
                } else {
                    await api("/api/technicians", {
                        method: "POST", body: techForm.value
                    });
                    toast("Teknisi berhasil ditambahkan.");
                }

                showTechForm.value = false;
                await loadTechnicians();
            } catch (error) {
                toast(error.message, "error");
            } finally {
                savingTech.value = false;
            }
        }

        async function removeTech(tech) {
            if (!confirm(`Hapus teknisi "${tech.name}"?`)) return;

            try {
                await api(`/api/technicians/${tech.id}`, { method: "DELETE" });
                toast("Teknisi dihapus.");
                await loadTechnicians();
            } catch (error) {
                toast(error.message, "error");
            }
        }

        /* ---------- Hubungi teknisi ---------- */

        function contactMessage(device, tech) {
            const lines = [
                `Halo ${tech.name}, mohon bantuan penanganan gangguan jaringan.`,
                "",
                `Perangkat: ${device.location}`,
                `IP Address: ${device.ip_address}`,
                `Lokasi: ${device.place || "belum diisi"}`,
                `Status: ${statusText(device.status)}`
            ];

            if (device.latency !== null && device.latency !== undefined) {
                lines.push(`Latency: ${formatMs(device.latency)}`);
            }
            if (device.cause_title) {
                lines.push(`Dugaan penyebab: ${device.cause_title}`);
            }
            if (device.checked_at) {
                lines.push(`Waktu pengecekan: ${device.checked_at}`);
            }

            lines.push("", "Terima kasih.");
            return lines.join("\n");
        }

        function waLink(tech, device) {
            return `https://wa.me/${tech.wa_number}?text=` +
                encodeURIComponent(contactMessage(device, tech));
        }

        function telLink(tech) {
            return `tel:+${tech.wa_number}`;
        }

        async function openContact(device) {
            if (!device) return;
            contactDevice.value = device;
            await loadTechnicians();
        }

        function closeContact() {
            contactDevice.value = null;
        }

        function goAddTechnician() {
            closeContact();
            if (detail.value) closeDetail();
            view.value = "settings";
            openTechCreate();
        }

        /* ---------- Bot Telegram ---------- */

        async function loadTelegram() {
            try {
                const data = await api("/api/settings/telegram");
                tg.value = data;
                tgForm.value = {
                    token: "",
                    chat_id: data.chat_id || "",
                    enabled: data.enabled,
                    notify_slow: data.notify_slow,
                    notify_recovery: data.notify_recovery,
                    repeat_seconds: data.repeat_seconds
                };
            } catch (error) {
                toast(error.message, "error");
            }
        }

        async function saveTelegram() {
            tgBusy.value = "save";
            try {
                const data = await api("/api/settings/telegram", {
                    method: "PUT", body: tgForm.value
                });
                tg.value = data;
                tgForm.value.token = "";
                toast(data.message || "Pengaturan disimpan.");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tgBusy.value = "";
            }
        }

        async function testTelegram() {
            tgBusy.value = "test";
            try {
                const data = await api("/api/settings/telegram/test", {
                    method: "POST",
                    body: {
                        token: tgForm.value.token,
                        chat_id: tgForm.value.chat_id
                    }
                });
                toast(data.message);
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tgBusy.value = "";
            }
        }

        async function detectChat() {
            tgBusy.value = "detect";
            detectedChats.value = [];
            try {
                const data = await api("/api/settings/telegram/detect", {
                    method: "POST",
                    body: { token: tgForm.value.token }
                });

                if (data.chats.length === 1) {
                    tgForm.value.chat_id = data.chats[0].id;
                    toast(`Chat ID ditemukan: ${data.chats[0].name}`);
                } else {
                    detectedChats.value = data.chats;
                    toast("Beberapa chat ditemukan, pilih salah satu.");
                }
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tgBusy.value = "";
            }
        }

        function pickChat(chat) {
            tgForm.value.chat_id = chat.id;
            detectedChats.value = [];
        }

        /* ---------- Lifecycle ---------- */

        onMounted(async () => {
            document.documentElement.dataset.theme = theme.value;
            await refresh(false);
            loadTechnicians();
            timer = setInterval(refresh, 10000);
        });

        onBeforeUnmount(() => clearInterval(timer));

        watch(view, value => {
            if (value === "logs") loadLogs();
            if (value === "report") loadPreview();
            if (value === "settings") {
                loadTechnicians();
                loadTelegram();
            }
            sidebarOpen.value = false;
        });

        watch(logFilter, loadLogs);
        watch(reportDate, loadPreview);

        const config = window.APP_CONFIG || {};

        return {
            interval: config.interval || 10,
            username: config.username || "pengguna",
            view, sidebarOpen, theme, devices, summary, spots, areas, logs,
            toasts, loading, search, logFilter, alertOn, showForm, editing,
            form, detail, detailLogs, detailDiagnosis, trace, tracing,
            detailLoading, detailError,
            reportDate, reportPreview, reportLoading, convertFile,
            convertTarget, convertTitle, converting, dragging,
            filtered, offlineCount, healthScore, parents,
            DEVICE_TYPES, statusClass, statusText, formatMs,
            refresh, loadLogs, openCreate, openEdit, saveDevice,
            removeDevice, openDetail, closeDetail, runTrace, loadPreview,
            download, pickFile, dropFile, runConvert, toggleTheme,
            toggleAlert, toast,
            technicians, showTechForm, editingTech, techForm, savingTech,
            techFormValid, phoneHint, contactDevice, tg, tgForm, tgBusy,
            detectedChats, loadTechnicians, sanitizePhone, onlyDigitsKey,
            openTechCreate, openTechEdit, saveTech, removeTech,
            waLink, telLink, openContact, closeContact, goAddTechnician,
            loadTelegram, saveTelegram, testTelegram, detectChat, pickChat
        };
    }
}).mount("#app");