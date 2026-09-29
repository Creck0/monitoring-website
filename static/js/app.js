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

        const showForm = ref(false);
        const editing = ref(null);
        const form = ref(blankForm());

        const detail = ref(null);
        const detailLogs = ref([]);
        const detailDiagnosis = ref(null);
        const trace = ref(null);
        const tracing = ref(false);

        const reportDate = ref(new Date().toISOString().slice(0, 10));
        const reportPreview = ref(null);
        const reportLoading = ref(false);

        /* Support / teknisi */
        const technicians = ref([]);
        const techForm = ref({ name: "", contact: "" });
        const techSaving = ref(false);

        /* Notifikasi Telegram */
        const tg = ref(null);
        const tgAccounts = ref([]);
        const tgToken = ref("");
        const tgForm = ref({ name: "", chat_id: "" });
        const tgSaving = ref(false);
        const tgTesting = ref(null);

        let chart = null;
        let timer = null;
        let notifyTimer = null;

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

                devices.value = d;
                summary.value = s;
                spots.value = h;
                areas.value = a;

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
            trace.value = null;

            try {
                const [l, diagnosis] = await Promise.all([
                    api(`/api/devices/${device.id}/logs?limit=60`),
                    api(`/api/devices/${device.id}/diagnosis`)
                ]);

                detailLogs.value = l.slice().reverse();
                detailDiagnosis.value = diagnosis;

                await nextTick();
                drawChart();

                trace.value = await api(
                    `/api/devices/${device.id}/trace`
                ).catch(() => null);
            } catch (error) {
                toast(error.message, "error");
            }
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

            chart = new Chart(canvas.getContext("2d"), {
                type: "line",
                data: {
                    labels: detailLogs.value.map(l => l.checked_at.slice(11, 19)),
                    datasets: [{
                        label: "Latency (ms)",
                        data: detailLogs.value.map(l => l.latency),
                        borderColor: "#2563eb",
                        backgroundColor: "rgba(37, 99, 235, .12)",
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
                            grid: { color: dark ? "#253450" : "#eef2f8" },
                            ticks: { maxTicksLimit: 8, color: "#6b7a92" }
                        },
                        y: {
                            beginAtZero: true,
                            grid: { color: dark ? "#253450" : "#eef2f8" },
                            ticks: { color: "#6b7a92" },
                            title: { display: true, text: "ms", color: "#6b7a92" }
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

        /* ---------- Support / teknisi ---------- */

        async function loadTechnicians() {
            try {
                technicians.value = await api("/api/technicians");
            } catch (error) {
                toast(error.message, "error");
            }
        }

        async function addTechnician() {
            if (techSaving.value) return;
            techSaving.value = true;

            try {
                await api("/api/technicians", {
                    method: "POST", body: techForm.value
                });

                techForm.value = { name: "", contact: "" };
                toast("Teknisi berhasil ditambahkan.");
                await loadTechnicians();
            } catch (error) {
                toast(error.message, "error");
            } finally {
                techSaving.value = false;
            }
        }

        async function removeTechnician(tech) {
            if (!confirm(`Hapus teknisi "${tech.name}" dari daftar?`)) return;

            try {
                await api(`/api/technicians/${tech.id}`, { method: "DELETE" });
                toast("Teknisi dihapus.");
                await loadTechnicians();
            } catch (error) {
                toast(error.message, "error");
            }
        }

        /* ---------- Notifikasi Telegram ---------- */

        async function loadTelegram() {
            try {
                const [settings, accounts] = await Promise.all([
                    api("/api/telegram/settings"),
                    api("/api/telegram/accounts")
                ]);

                tg.value = settings;
                tgAccounts.value = accounts;
            } catch (error) {
                toast(error.message, "error");
            }
        }

        async function saveTelegramToken() {
            if (!tgToken.value.trim()) {
                toast("Isi token bot terlebih dahulu.", "error");
                return;
            }

            tgSaving.value = true;

            try {
                const result = await api("/api/telegram/settings", {
                    method: "POST", body: { token: tgToken.value }
                });

                tgToken.value = "";
                tg.value = result;
                toast(result.warning || "Token bot tersimpan.",
                      result.warning ? "error" : "success");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tgSaving.value = false;
            }
        }

        async function toggleTelegramNotify() {
            if (!tg.value) return;

            try {
                tg.value = await api("/api/telegram/settings", {
                    method: "POST",
                    body: { notify_enabled: !tg.value.notify_enabled }
                });

                toast(tg.value.notify_enabled
                    ? "Notifikasi Telegram diaktifkan."
                    : "Notifikasi Telegram dimatikan.");
            } catch (error) {
                toast(error.message, "error");
            }
        }

        async function addTelegramAccount() {
            if (tgSaving.value) return;
            tgSaving.value = true;

            try {
                await api("/api/telegram/accounts", {
                    method: "POST", body: tgForm.value
                });

                tgForm.value = { name: "", chat_id: "" };
                toast("Akun Telegram berhasil ditambahkan.");
                tgAccounts.value = await api("/api/telegram/accounts");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tgSaving.value = false;
            }
        }

        async function removeTelegramAccount(account) {
            if (!confirm(
                `Hapus akun "${account.name}"? ` +
                "Akun ini tidak akan menerima notifikasi lagi."
            )) return;

            try {
                await api(`/api/telegram/accounts/${account.id}`, {
                    method: "DELETE"
                });
                toast("Akun Telegram dihapus.");
                tgAccounts.value = await api("/api/telegram/accounts");
            } catch (error) {
                toast(error.message, "error");
            }
        }

        async function testTelegramAccount(account) {
            tgTesting.value = account.id;

            try {
                const result = await api(
                    `/api/telegram/accounts/${account.id}/test`,
                    { method: "POST" }
                );
                toast(result.message);
            } catch (error) {
                toast(error.message, "error");
            } finally {
                tgTesting.value = null;
            }
        }

        /* ---------- Tema ---------- */

        function toggleTheme() {
            theme.value = theme.value === "dark" ? "light" : "dark";
            localStorage.setItem("netmon-theme", theme.value);
            document.documentElement.dataset.theme = theme.value;
            if (chart) drawChart();
        }

        /* ---------- Notifikasi otomatis (tiap 30 detik) ---------- */

        const NOTIFY_INTERVAL = 30000;

        function notifyOffline() {
            const down = spots.value.filter(x => x.status === "OFFLINE");
            if (!down.length) return;

            const names = down.slice(0, 3)
                .map(x => `${x.location} (${x.ip_address})`).join(", ");
            const more = down.length > 3
                ? ` dan ${down.length - 3} lainnya` : "";
            const message =
                `${down.length} perangkat terputus: ${names}${more}`;

            toast(message, "error");

            // Notifikasi sistem (push) bila diizinkan browser
            if ("Notification" in window &&
                Notification.permission === "granted") {
                try {
                    new Notification("Gangguan Jaringan", {
                        body: message, tag: "netmon-offline"
                    });
                } catch (e) { /* diabaikan */ }
            }
        }

        function askNotifyPermission() {
            if ("Notification" in window &&
                Notification.permission === "default") {
                Notification.requestPermission();
            }
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

        const tokenPlaceholder = computed(() => {
            if (tg.value && tg.value.token_source === "env") {
                return "Token diatur lewat environment TELEGRAM_BOT_TOKEN";
            }

            return tg.value && tg.value.configured
                ? "Isi untuk mengganti token"
                : "123456789:AAE...";
        });

        const parents = computed(() =>
            devices.value.filter(d => d.id !== editing.value)
        );

        /* ---------- Lifecycle ---------- */

        onMounted(async () => {
            document.documentElement.dataset.theme = theme.value;
            await refresh(false);
            loadTechnicians();
            timer = setInterval(refresh, 10000);
            notifyTimer = setInterval(notifyOffline, NOTIFY_INTERVAL);
            // Izin notifikasi sistem hanya bisa diminta lewat interaksi
            // pengguna, jadi diminta saat klik pertama.
            window.addEventListener("click", askNotifyPermission,
                                    { once: true });
        });

        onBeforeUnmount(() => {
            clearInterval(timer);
            clearInterval(notifyTimer);
        });

        watch(view, value => {
            if (value === "logs") loadLogs();
            if (value === "report") loadPreview();
            if (value === "support") loadTechnicians();
            if (value === "telegram") loadTelegram();
            sidebarOpen.value = false;
        });

        watch(logFilter, loadLogs);
        watch(reportDate, loadPreview);

        const config = window.APP_CONFIG || {};

        return {
            interval: config.interval || 10,
            username: config.username || "pengguna",
            view, sidebarOpen, theme, devices, summary, spots, areas, logs,
            toasts, loading, search, logFilter, showForm, editing,
            form, detail, detailLogs, detailDiagnosis, trace, tracing,
            reportDate, reportPreview, reportLoading,
            technicians, techForm, techSaving,
            tg, tgAccounts, tgToken, tgForm, tgSaving, tgTesting,
            tokenPlaceholder,
            addTechnician, removeTechnician, saveTelegramToken,
            toggleTelegramNotify, addTelegramAccount,
            removeTelegramAccount, testTelegramAccount,
            filtered, offlineCount, healthScore, parents,
            DEVICE_TYPES, statusClass, statusText, formatMs,
            refresh, loadLogs, openCreate, openEdit, saveDevice,
            removeDevice, openDetail, closeDetail, runTrace, loadPreview,
            download, toggleTheme, toast
        };
    }
}).mount("#app");
