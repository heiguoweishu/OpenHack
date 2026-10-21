/* OpenHack 前端 —— 全部通过 SSE 接收 worker 的实时输出 */
(() => {
  "use strict";

  // 开局先保持透明，等 /api/glassmode 判定后再决定显不显合成壁纸（避免跳变）
  document.body.classList.add("boot");

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // 首次进入时的查询参数要在 show() 改写 hash 之前抓下来，否则会被抹掉
  const BOOT_QUERY = new URLSearchParams((location.hash.split("?")[1]) || "");

  const state = {
    health: null,
    disks: [],
    volumes: [],
    analysis: null,
    report: null,
    selectedDisk: null,
    efiSources: [],
    installing: false,
  };

  /* ---------------------------------------------------------- 基础 */

  function toast(msg, kind = "info") {
    const wrap = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast";
    const icons = {
      info: '<svg viewBox="0 0 24 24" fill="none" stroke="#007aff" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>',
      ok: '<svg viewBox="0 0 24 24" fill="none" stroke="#34c759" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 5-5"/></svg>',
      warn: '<svg viewBox="0 0 24 24" fill="none" stroke="#ff9f0a" stroke-width="2" stroke-linecap="round"><path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
      error: '<svg viewBox="0 0 24 24" fill="none" stroke="#ff3b30" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
    };
    el.innerHTML = (icons[kind] || icons.info) + `<div>${esc(msg)}</div>`;
    wrap.appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 320);
    }, 4600);
  }

  let logSeq = 0;

  function logLine(msg, level = "info") {
    const box = $("#console");
    if (!box) return;
    const t = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const row = document.createElement("div");
    row.className = "l-" + level;
    row.innerHTML = `<span class="t">${t}</span> ${esc(msg)}`;
    box.appendChild(row);
    if (box.children.length > 1600) box.removeChild(box.firstChild);
    // 只有在用户已经翻到底部时才自动跟随，避免打断向上翻阅
    if (box.scrollTop + box.clientHeight >= box.scrollHeight - 60) box.scrollTop = box.scrollHeight;
    logSeq++;
  }

  function human(bytes) {
    const b = Number(bytes || 0);
    if (!b) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`;
  }

  /* ---------------------------------------------------------- 视图 */

  const TITLES = {
    home: ["概览", "先看看这台机器的状态，再决定走哪条路"],
    efi: ["一键生成 EFI", "扫描硬件 → 判定兼容性 → 生成 OpenCore 引导"],
    install: ["无损安装到本机", "在现有硬盘上腾出空间，植入 EFI 与恢复镜像"],
    logs: ["运行日志", "所有操作的完整输出"],
    about: ["致谢 · 免责 · 许可", "这个工具借用了哪些开源成果，以及你需要承担什么"],
  };

  /* 致谢名单。借用了别人的劳动，就把出处和许可写清楚，
     而不是散落在界面各处——主界面只讲怎么用。 */
  const CREDITS = [
    {
      name: "OpCore-Simplify",
      use: "硬件兼容性判定、SMBIOS 机型匹配、内核扩展与 ACPI 补丁选择、config.plist 生成",
      lic: "BSD-3-Clause",
      nc: false,
    },
    {
      name: "Hardware-Sniffer",
      use: "硬件报告采集与 ACPI 原始表导出",
      lic: "BSD-3-Clause",
      nc: false,
    },
    {
      name: "OpenCorePkg / Acidanthera",
      use: "引导加载器本体与 Lilu、VirtualSMC、AppleALC、WhateverGreen 等内核扩展",
      lic: "BSD-3-Clause",
      nc: false,
    },
    {
      name: "macrecovery (vit9696)",
      use: "从 Apple 官方服务器获取恢复镜像的协议，含 chunklist 的 RSA 验签",
      lic: "BSD-3-Clause",
      nc: false,
    },
    {
      name: "1401 / NullMoth",
      use: "NVIDIA 显卡的 macOS 驱动移植，以及磁盘操作脚本的防劫持范式",
      lic: "PolyForm Noncommercial",
      nc: true,
    },
    {
      name: "Dortania OpenCore Install Guide",
      use: "补丁选择规则、各机型支持矩阵与安装流程",
      lic: "知识共享 (CC)",
      nc: false,
    },
  ];

  /** 从 hash 里取视图名，忽略 "?disk=1" 这类附加参数 */
  function viewFromHash() {
    const raw = (location.hash || "").replace("#", "");
    const name = raw.split("?")[0];
    return TITLES[name] ? name : "home";
  }

  function show(view) {
    if (!TITLES[view]) view = "home";
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
    $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === view));
    const t = TITLES[view] || TITLES.home;
    $("#pageTitle").textContent = t[0];
    $("#pageSub").textContent = t[1];
    if (view === "about") renderCredits();
    // 用 hash 记住当前位置：刷新或从浏览器返回时不会跳回首页
    if (location.hash !== "#" + view) {
      history.replaceState(null, "", "#" + view);
    }
  }

  $$(".nav-item").forEach((n) => n.addEventListener("click", () => show(n.dataset.view)));
  $$("[data-goto]").forEach((el) => el.addEventListener("click", () => show(el.dataset.goto)));

  /* ---------------------------------------------------------- SSE */

  function stream(url, { onEvent, onDone, onError } = {}) {
    const es = new EventSource(url);
    let ended = false;
    const finish = () => {
      if (ended) return;
      ended = true;
      try { es.close(); } catch (_) {}
      onDone && onDone();
    };
    es.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      onEvent && onEvent(ev);
      if (ev.t === "done") finish();
    };
    es.addEventListener("end", finish);
    es.onerror = () => {
      finish();
      onError && onError();
    };
    return () => finish();
  }

  function setProgress(barSel, pctSel, labelSel, ev) {
    const bar = $(barSel), pct = $(pctSel), lab = $(labelSel);
    if (!bar) return;
    if (ev.value != null) {
      bar.style.width = ev.value + "%";
      if (pct) pct.textContent = ev.value + "%";
    }
    if (ev.label) lab && (lab.textContent = ev.label);
  }

  function renderSteps(container, step) {
    if (!container || !step || !step.name) return;
    const key = "steps:" + container.id;
    const known = (window[key] ||= []);
    if (!known.includes(step.name)) known.push(step.name);
    container.innerHTML = known.map((n, i) => {
      const cls = i === known.length - 1 ? "active" : "done";
      return `<span class="step-pill ${cls}"><span class="n">${i + 1}</span>${esc(n)}</span>`;
    }).join("");
  }

  function markStepsDone(container) {
    if (!container) return;
    $$("#" + container.id + " .step-pill").forEach((p) => {
      p.classList.remove("active");
      p.classList.add("done");
    });
  }

  /* ---------------------------------------------------------- 致谢页 */

  function renderCredits() {
    const box = $("#creditList");
    if (!box || box.dataset.done) return;
    box.innerHTML = CREDITS.map((c) => `
      <div class="credit-item">
        <div style="min-width:0">
          <span class="ci-name">${esc(c.name)}</span>
          <div class="ci-use">${esc(c.use)}</div>
        </div>
        <span class="ci-lic${c.nc ? " nc" : ""}">${esc(c.lic)}</span>
      </div>`).join("");
    box.dataset.done = "1";
  }

  /* ---------------------------------------------------------- 外观与退出 */

  const wpKey = "glasshack.wallpaper";

  // 真实桌面背景：启动器抓了用户桌面壁纸，这里把它铺到 .wallpaper 层上。
  // 加载成功才加 real-desktop（显示真实壁纸），失败就退回合成渐变（不加这个类即可）。
  function loadDesktopWallpaper() {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => { document.body.classList.add("real-desktop"); resolve(true); };
      img.onerror = () => resolve(false);
      img.src = "/api/desktop-wallpaper?t=" + Date.now();
    });
  }

  function applyWallpaper(on) {
    document.body.classList.remove("boot");
    document.body.classList.toggle("no-wallpaper", !on);
    const t = $("#wallpaperToggle");
    if (t) t.checked = !!on;
    try {
      localStorage.setItem(wpKey, on ? "1" : "0");
    } catch (_) {}
  }

  // 是否跑在原生外壳里（native/GlassHack.exe 的 WebView2）。
  // 那种模式下玻璃是操作系统画的，页面不该再铺壁纸图或渐变 —— 否则正好把材质盖住。
  const IN_WEBVIEW = !!(window.chrome && window.chrome.webview);

  const wpToggle = $("#wallpaperToggle");
  if (IN_WEBVIEW) {
    // 原生模式：直接结束"开局防闪"状态，玻璃交给 DWM
    document.body.classList.remove("boot");
    if (wpToggle) wpToggle.checked = true;
  } else if (wpToggle) {
    let saved = null;
    try {
      saved = localStorage.getItem(wpKey);
    } catch (_) {}
    // 默认开：液态玻璃（毛玻璃卡片透出真实桌面壁纸）就是产品外观。
    // 只有用户主动关过（saved === "0"）才恢复为干净模式，否则一律玻璃态。
    const want = saved !== "0";
    // 先等真实壁纸加载判定，再决定显不显壁纸层，避免先闪渐变再跳壁纸
    loadDesktopWallpaper().then(() => applyWallpaper(want));
    wpToggle.addEventListener("change", () => applyWallpaper(wpToggle.checked));
  }

  // 原生外壳里拖窗口：WebView2 不认 Electron 的 -webkit-app-region，
  // 所以这里判断"该不该拖"，然后发消息让 C++ 端真正发起标题栏拖拽。
  // 用 capture 阶段 + 排除表单控件，按钮/输入框的点击不会被吞掉。
  if (IN_WEBVIEW) {
    document.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const t = e.target;
      if (!t || !t.closest(".main-head")) return;
      if (t.closest("button, input, select, textarea, a, .chip, label")) return;
      window.chrome.webview.postMessage("__drag");
    }, true);
  }

  $("#btnQuit").addEventListener("click", async () => {
    if (!confirm("结束本地服务并退出？正在进行中的操作会被中断。")) return;
    try {
      await fetch("/api/shutdown");
    } catch (_) {}
    document.body.innerHTML =
      '<div style="height:100vh;display:grid;place-items:center;color:#6e6e73;font-size:14px">' +
      "已退出，可以关闭这个窗口了。</div>";
  });

  /* ---------------------------------------------------------- 体检 */

  async function loadHealth() {
    try {
      const r = await fetch("/api/health");
      const d = await r.json();
      state.health = d;
      renderHealth(d);
    } catch (e) {
      $("#checkList").innerHTML = `<div class="note note-error">体检失败：${esc(e.message)}</div>`;
    }
  }

  function renderHealth(d) {
    $("#adminChip").textContent = d.admin ? "管理员模式" : "非管理员";
    $("#adminChip").style.background = d.admin ? "rgba(52,199,89,.16)" : "rgba(255,159,10,.18)";
    $("#adminChip").style.color = d.admin ? "#248a3d" : "#a06400";

    const items = (d.check && d.check.items) || [];
    $("#checkList").innerHTML = items.length ? items.map((i) => {
      const icons = {
        ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="m5 13 4 4L19 7"/></svg>',
        warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
        blocker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
      };
      return `<div class="check ${i.level}">
        <div class="check-icon">${icons[i.level] || icons.ok}</div>
        <div style="flex:1;min-width:0">
          <b>${esc(i.title)}</b>
          <div class="val">${esc(i.value)}</div>
          <div class="detail">${esc(i.detail)}</div>
        </div>
      </div>`;
    }).join("") : `<div class="empty">没有可显示的检查项</div>`;

    const m = d.machine || {};
    const rows = [
      ["机型", `${m.manufacturer || ""} ${m.model || ""}`.trim() || "—"],
      ["主板", m.board || "—"],
      ["处理器", m.cpu || "—"],
      ["核心/线程", m.cores ? `${m.cores} 核 / ${m.threads} 线程` : "—"],
      ["内存", m.memoryText || "—"],
      ["BIOS", m.bios || "—"],
      ["显卡", (m.gpus || []).join("、") || "—"],
      ["磁盘", (d.disks || []).map((x) => `#${x.number} ${x.name} ${human(x.size)}`).join("<br>") || "—"],
    ];
    $("#hwList").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
  }

  $("#btnRefresh").addEventListener("click", () => {
    toast("正在重新体检…");
    loadHealth();
  });

  /* ---------------------------------------------------------- 路线一：扫描 + 分析 */

  $("#btnScan").addEventListener("click", () => {
    const btn = $("#btnScan");
    btn.disabled = true;
    $("#scanProgressWrap").style.display = "";
    $("#scanStatus").textContent = "";
    logLine("开始扫描本机硬件…", "info");

    stream("/api/scan", {
      onEvent(ev) {
        if (ev.t === "progress") setProgress("#scanBar", "#scanPct", "#scanLabel", ev);
        else if (ev.t === "step") renderSteps($("#efiSteps"), ev.step || { name: ev.name });
        else if (ev.t === "log") logLine(ev.msg, ev.level);
        else if (ev.t === "result") {
          state.report = ev.data.reportPath;
          state.acpi = ev.data.acpiDir;
          toast(`硬件报告已生成（${ev.data.acpiTables || 0} 份 ACPI 表）`, "ok");
          analyze();
        } else if (ev.t === "done" && ev.exit !== 0) {
          toast("扫描失败，请查看日志", "error");
        }
      },
      onDone() { btn.disabled = false; },
    });
  });

  $("#btnLoadReport").addEventListener("click", () => {
    const p = prompt("硬件报告 Report.json 的完整路径：", state.report || "");
    if (!p) return;
    state.report = p.trim();
    analyze();
  });

  // 支持 #efi?report=<路径> 直接载入并分析，省掉每次弹框输路径
  const bootReport = BOOT_QUERY.get("report");
  if (bootReport) {
    state.report = bootReport;
    show("efi");
    analyze();
  }

  function analyze() {
    if (!state.report) return;
    $("#scanStatus").textContent = "正在分析兼容性…";
    stream("/api/analyze?report=" + encodeURIComponent(state.report), {
      onEvent(ev) {
        if (ev.t === "progress") setProgress("#scanBar", "#scanPct", "#scanLabel", ev);
        else if (ev.t === "log") logLine(ev.msg, ev.level);
        else if (ev.t === "result") {
          state.analysis = ev.data;
          renderAnalysis(ev.data);
          $("#scanStatus").textContent = "分析完成";
          toast("兼容性分析完成", "ok");
        } else if (ev.t === "done") {
          if (ev.exit !== 0) $("#scanStatus").textContent = "分析失败";
        }
      },
    });
  }

  function renderAnalysis(d) {
    $("#efiConfigCard").style.display = "";
    $("#efiIntroCard").style.display = "none";

    // 版本下拉
    const sel = $("#macosVersion");
    sel.innerHTML = (d.options || []).map((o) =>
      `<option value="${esc(o.darwin)}"${o.darwin === String(d.suggested).split(".")[0] ? " selected" : ""}>${esc(o.name)}</option>`
    ).join("");
    if (d.suggested && ![].some.call(sel.options, (o) => o.value === String(d.suggested).split(".")[0])) {
      sel.insertAdjacentHTML("afterbegin",
        `<option value="${esc(d.suggested)}" selected>${esc(d.suggestedName || d.suggested)}（推荐）</option>`);
    }
    $("#versionHint").textContent = d.nativeRange
      ? (d.thirdPartyGpu
          // 有第三方驱动时不能再说"原生"：上游引擎本来判这块卡不支持，
          // 区间是 NullMoth 的能力撑出来的，两者必须分开说。
          ? `macOS 原生支持到 ${d.nativeRange[1]}；显卡由 NullMoth 第三方驱动覆盖到 macOS 26，推荐 ${d.suggestedName || d.suggested}。`
          : `硬件原生支持 macOS ${d.nativeRange[0]} ~ ${d.nativeRange[1]}，推荐 ${d.suggestedName || d.suggested}。`)
      : "";

    // 显卡提示。三种情况必须分开，不然界面会自相矛盾：
    //   ① NullMoth 认领了这张 N 卡 → 显卡能用（第三方驱动），绝不能说"没有可驱动显卡"；
    //   ② 上游判不支持且没有第三方兜底 → 如实说装完没图形界面，并给可行路径；
    //   ③ 核显有没有，用 CPU 型号后缀直接判定（F/KF 后缀没有），
    //      别再丢一句"若带 F 后缀则没有核显"让用户自己去猜。
    const cpuName = (d.cpu && d.cpu["Processor Name"]) || "";
    const cpuTail = cpuName.trim().split(/[\s-]+/).pop() || "";
    const cpuSuffix = cpuTail.replace(/\d+/g, "").toUpperCase();
    const igpuMissing = cpuSuffix.includes("F");

    if (d.gpuBlocked && !d.thirdPartyGpu) {
      const cpuPart = igpuMissing
        ? `你的 CPU（${esc(cpuName)}）型号后缀是 <b>${esc(cpuSuffix)}</b>，不带核显，`
          + `所以这台机器目前没有任何 macOS 能驱动的图形设备。`
        : `你的 CPU（${esc(cpuName)}）带核显 —— 进 BIOS 打开 iGPU、把显示器接到主板视频口，`
          + `按核显输出走也能有图形界面。`;
      $("#gpuWarn").innerHTML = `
      <div class="note note-warn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 9v5M12 17.5v.5"/><circle cx="12" cy="12" r="9"/></svg>
        <div>
          <b>这块独显不在 macOS 的支持范围内。</b>${cpuPart}
          生成的 EFI 仍可正常用，但装完不会有图形界面（只能当无头服务器）。
          想要图形界面，三条路：换一块受支持的 AMD 显卡（RX 6600 / 6800 / 6900 XT 等）、
          换带核显的 CPU 并启用 iGPU、或直接按无头服务器用途装。
        </div>
      </div>`;
    } else {
      $("#gpuWarn").innerHTML = "";
    }

    // NullMoth 驱动：让一批 NVIDIA 卡能跑 macOS 15 / 26。实测过的和"仅在支持表内"必须区别对待。
    const nv = d.nullmoth || {};
    if (nv.state === "tested") {
      $("#gpuWarn").innerHTML += `
        <div class="note note-ok">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m5 13 4 4L19 7"/></svg>
          <div><b>NullMoth NVIDIA 驱动（实测支持，第三方驱动）。</b>${esc(nv.text)}。
          这不是 macOS 原生支持 —— 上游引擎本来判这块卡不支持，能跑是因为有这套驱动。
          它构建了 macOS 15 Sequoia 与 macOS 26 Tahoe 两个版本，默认推荐较新的 26，版本下拉里可改。
          macOS 装好后需运行安装介质上的 <code>NullMoth/README.txt</code> 里的步骤装上驱动，才有图形输出。</div>
        </div>`;
    } else if (nv.state === "listed") {
      $("#gpuWarn").innerHTML += `
        <div class="note note-warn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 9v5M12 17.5v.5"/><circle cx="12" cy="12" r="9"/></svg>
          <div>
            <b>NullMoth NVIDIA 驱动（支持表内，未实测，第三方驱动）。</b>${esc(nv.text)}。
            支持表来自 NVIDIA 自家的 open-gpu-kernel-modules 610.57.04，
            但 NullMoth 只实测过表里的一个型号，所以这是"可尝试"而不是"保证可用"。<br>
            该驱动构建了 macOS 15 与 macOS 26 两个版本，默认推荐 26，版本下拉里可改。
            装好后需按 <code>NullMoth/README.txt</code> 手动装驱动。
          </div>
        </div>`;
    }

    // 别写成"已经按你的硬件勾选好"：勾选是构建时才算的，分析阶段还没跑，说死了会误导
    $("#acpiHint").textContent = `引擎内置 ${(d.acpiPatches || []).length} 个补丁，选"自动"时由引擎按 CPU / 主板在构建阶段挑选。`;

    // 驱动包开关的提示要跟实际硬件对上，别让用户勾了一个不会生效的选项
    const hint = $("#stageDriverHint");
    if (d.nullmoth && d.nullmoth.state === "none") {
      hint.textContent = "未检测到 NullMoth 支持的 NVIDIA 独显，不会放入驱动包。";
      $("#stageDriver").checked = false;
    } else if (d.nullmoth) {
      hint.textContent = `将放入驱动包：${d.nullmoth.text}`;
      $("#stageDriver").checked = true;
    }

    renderDevices(d.devices || []);
  }

  function renderDevices(devices) {
    $("#deviceList").innerHTML = devices.map((x) => {
      const labels = { ok: "支持", oclp: "需 OCLP", unsupported: "不支持", unknown: "未知" };
      return `<div class="dev">
        <span class="dot ${x.state}"></span>
        <div class="dev-body">
          <b>${esc(x.name)}</b>
          <small>${esc(x.deviceId)} · ${esc(x.note || labels[x.state] || "")}</small>
        </div>
        <span class="dev-type">${esc(x.type)}</span>
      </div>`;
    }).join("") || `<div class="empty">没有检测到需要关注的设备</div>`;
  }

  /* ---------------------------------------------------------- 路线一：构建 */

  $("#btnBuild").addEventListener("click", () => {
    if (!state.report) return toast("请先扫描或载入硬件报告", "warn");
    const btn = $("#btnBuild");
    btn.disabled = true;
    $("#efiBuildCard").style.display = "";
    $("#efiResultCard").style.display = "none";
    window["steps:buildSteps"] = [];
    $("#buildSteps").innerHTML = "";
    show("efi");
    logLine("开始构建 EFI…", "info");

    const cfg = {
      macosVersion: $("#macosVersion").value,
      acpiDir: state.acpi || "",
      prefs: { audioStrategy: $("#audioStrategy").value },
      allowUnsupportedGpu: true,
      reportPath: state.report || "",
    };
    // ACPI 补丁怎么传，这里有个必须小心的坑：
    //   分析阶段并没有跑过引擎的 select_acpi_patches（那是构建时才做的），所以
    //   state.analysis.acpiPatches 里的 checked 全是 False —— 一旦把它 filter 出来
    //   传给后端，后端会理解成"用户要全部关闭"，结果 EFI 里一个 SSDT 都没有。
    //   所以 auto 必须**不传**这个字段，让引擎自己按 CPU / 主板挑。
    const preset = $("#acpiPreset").value;
    cfg.acpiPreset = preset;
    if (preset === "none") cfg.acpiPatches = [];
    else if (preset === "all") cfg.acpiPatches = (state.analysis?.acpiPatches || []).map((p) => p.name);
    // preset === "auto"：两个字都别给，走后端自动判定

    stream("/api/build?report=" + encodeURIComponent(state.report) + "&cfg=" + encodeURIComponent(JSON.stringify(cfg)), {
      onEvent(ev) {
        if (ev.t === "progress") setProgress("#buildBar", "#buildPct", "#buildLabel", ev);
        else if (ev.t === "step") renderSteps($("#buildSteps"), ev);
        else if (ev.t === "log") logLine(ev.msg, ev.level);
        else if (ev.t === "result") {
          markStepsDone($("#buildSteps"));
          $("#buildBar").parentElement.classList.add("done");
          renderEfiResult(ev.data);
          toast("EFI 生成完成", "ok");
        } else if (ev.t === "done" && ev.exit !== 0) {
          toast("构建失败，请查看日志", "error");
        }
      },
      onDone() { btn.disabled = false; },
    });
  });

  function renderEfiResult(d) {
    $("#efiResultCard").style.display = "";
    $("#efiOutPath").textContent = d.output || "";
    $("#efiStats").innerHTML = [
      ["文件数", d.files],
      ["总大小", human(d.size)],
      ["kext", d.kexts?.length ?? 0],
      ["ACPI", d.acpi?.length ?? 0],
    ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
    $("#kextList").innerHTML = (d.kexts || []).map((k) => `<span class="chip">${esc(k)}</span>`).join("") ||
      `<span class="muted" style="font-size:12px">无</span>`;
    $("#acpiList").innerHTML = (d.acpi || []).map((k) => `<span class="chip">${esc(k)}</span>`).join("") ||
      `<span class="muted" style="font-size:12px">无</span>`;
    if (!d.hasConfig) toast("未生成 config.plist，EFI 可能不完整", "warn");
    loadEfiSources();
  }

  $("#btnOpenEfi").addEventListener("click", async () => {
    const r = await fetch("/api/open?path=" + encodeURIComponent($("#efiOutPath").textContent || ""));
    const d = await r.json();
    if (!d.ok) toast(d.error || "打开失败", "error");
  });

  $("#btnToInstall").addEventListener("click", () => {
    show("install");
    loadDisks();
    toast("已选中刚生成的 EFI 作为安装来源", "ok");
  });

  /* ---------------------------------------------------------- 路线二：磁盘 */

  async function loadDisks() {
    $("#diskList").innerHTML = `<div class="empty"><div class="spinner dark" style="margin:0 auto"></div><br>正在读取磁盘…</div>`;
    try {
      const [dr, vr, sr] = await Promise.all([
        fetch("/api/disks").then((r) => r.json()),
        fetch("/api/shrink").then((r) => r.json()),
        fetch("/api/efi/list").then((r) => r.json()),
      ]);
      state.disks = dr.disks || [];
      state.volumes = vr.volumes || [];
      state.efiSources = sr.sources || [];
      renderDisks();
      renderVolumeOptions();
      renderEfiSourceOptions();
      renderRecoveryVersions();
      // 支持 #install?disk=1 直达某块盘的规划面板，省得每次手点
      const want = BOOT_QUERY.get("disk");
      if (want != null && state.disks.some((d) => String(d.disk) === want)) {
        state.selectedDisk = Number(want);
        renderDisks();
        onDiskSelected();
      }
    } catch (e) {
      $("#diskList").innerHTML = `<div class="note note-error">读取磁盘失败：${esc(e.message)}</div>`;
    }
  }

  /* GPT 类型 GUID。注意 PowerShell 返回的是带花括号的 "{c12a7328-...}"，
     直接拿常量比会全部落空 —— 后果是 ESP 和 APFS 分区被当成普通数据卷标错颜色。 */
  const GPT = {
    ESP: "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    MSR: "e3c9e316-0b5c-4db8-817d-f92df00215ae",
    WINRE: "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
    APFS: "7c3457ef-0000-11aa-aa11-00306543ecac",
    HFS: "48465300-0000-11aa-aa11-00306543ecac",
    BASIC: "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
  };
  const gptNorm = (s) => String(s || "").toLowerCase().replace(/^\{/, "").replace(/\}$/, "");

  const PART_LABEL = {
    "p-win": "Windows",
    "p-data": "数据",
    "p-apple": "Apple",
    "p-efi": "EFI",
    "p-free": "系统保留",
  };

  function partClass(p) {
    const t = gptNorm(p.gptType);
    if (t === GPT.ESP) return "p-efi";
    if (t === GPT.APFS || t === GPT.HFS) return "p-apple";
    if (t === GPT.MSR || t === GPT.WINRE) return "p-free";
    if (String(p.letter || "").toUpperCase() === "C") return "p-win";
    return "p-data";
  }

  function partKind(p) {
    const t = gptNorm(p.gptType);
    if (t === GPT.ESP) return "EFI 系统分区";
    if (t === GPT.MSR) return "Microsoft 保留";
    if (t === GPT.WINRE) return "Windows 恢复";
    if (t === GPT.APFS) return "Apple APFS";
    if (t === GPT.HFS) return "Apple HFS+";
    return (p.volume?.fs || p.type || "数据分区");
  }

  function renderDisks() {
    const list = state.disks;
    if (!list.length) {
      $("#diskList").innerHTML = `<div class="empty">没有检测到磁盘</div>`;
      return;
    }
    $("#diskList").innerHTML = `<div class="grid grid-2">` + list.map((d) => {
      const total = d.size || 1;
      const used = (d.parts || []).reduce((a, p) => a + (p.size || 0), 0);
      const unalloc = Math.max(0, total - used);
      const buckets = {};
      (d.parts || []).forEach((p) => {
        const c = partClass(p);
        buckets[c] = (buckets[c] || 0) + (p.size || 0);
      });
      const order = ["p-win", "p-data", "p-apple", "p-efi", "p-free"];
      const bars = order.filter((c) => buckets[c] > 0)
        .map((c) => `<span class="${c}" style="width:${(buckets[c] / total * 100).toFixed(3)}%"></span>`)
        .join("");
      const unallocBar = unalloc > 0
        ? `<span class="p-void" style="width:${(unalloc / total * 100).toFixed(3)}%"></span>` : "";

      const legend = order.filter((c) => buckets[c] > 0)
        .map((c) => `<span><i class="${c}-dot"></i>${PART_LABEL[c]} ${human(buckets[c])}</span>`).join("")
        + (unalloc > 0 ? `<span><i class="p-void-dot"></i>未分配 ${human(unalloc)}</span>` : "");

      const sh = state.volumes.filter((v) => (d.parts || []).some(
        (p) => String(p.letter || "").toUpperCase() === v.letter));
      const shrinkable = sh.reduce((a, v) => a + (v.maximal || 0), 0);

      const partRows = (d.parts || []).map((p) => {
        const cls = partClass(p);
        return `<div class="part-row">
          <span class="part-n">#${p.number}</span>
          <span class="part-dot ${cls}"></span>
          <span class="part-kind">${esc(partKind(p))}</span>
          <span class="part-size tabular">${human(p.size)}</span>
          <span class="part-letter">${esc(p.letter ? p.letter + ":" : "")}</span>
        </div>`;
      }).join("");

      const blocks = (d.issues || []).map((i) =>
        `<div class="note note-error" style="margin:10px 0 0;font-size:11.5px"><div><b>${esc(i.title)}</b>：${esc(i.detail)}</div></div>`).join("");
      const warns = (d.warnings || []).map((i) =>
        `<div class="note note-warn" style="margin:8px 0 0;font-size:11.5px"><div><b>${esc(i.title)}</b>：${esc(i.detail)}</div></div>`).join("");

      return `<div class="card glass disk-card${state.selectedDisk === d.disk ? " selected" : ""}" data-disk="${d.disk}">
        <div class="disk-title"><b>${esc(d.name)}</b><span class="n">磁盘 ${d.disk}</span>
          ${d.gpt ? '<span class="tag-ok">GPT</span>' : '<span class="tag-bad">MBR</span>'}
        </div>
        <div class="disk-meta">${human(d.size)} · ${(d.parts || []).length} 个分区 ·
          ${shrinkable > 0 ? `可压缩出 <b>${human(shrinkable)}</b>` : (unalloc > 0 ? `尾部未分配 ${human(unalloc)}` : "空间紧张")}
        </div>
        <div class="bar">${bars}${unallocBar}</div>
        <div class="legend">${legend}</div>
        <div class="part-list">${partRows}</div>
        ${blocks}${warns}
      </div>`;
    }).join("") + `</div>`;

    $$(".disk-card").forEach((el) => el.addEventListener("click", () => {
      state.selectedDisk = Number(el.dataset.disk);
      renderDisks();
      onDiskSelected();
    }));
  }

  function onDiskSelected() {
    const d = state.disks.find((x) => x.disk === state.selectedDisk);
    if (!d) return;
    $("#planCard").style.display = "";
    $("#instRunCard").style.display = "";
    $("#instSteps").innerHTML = `<span class="step-pill active"><span class="n">1</span>选择磁盘</span>
      <span class="step-pill"><span class="n">2</span>规划</span><span class="step-pill"><span class="n">3</span>执行</span>`;
    // 只显示属于该盘的卷
    renderVolumeOptions();
    updateSummary();
    if ((d.issues || []).length) toast("该磁盘存在阻塞项，请先处理", "warn");
  }

  function renderVolumeOptions() {
    const sel = $("#shrinkVolume");
    const vols = state.volumes.filter((v) => v.maximal > 512 * 1024 * 1024);
    sel.innerHTML = vols.map((v) => {
      const tag = v.isSystem ? "（Windows 系统）" : "";
      return `<option value="${esc(v.letter)}" data-max="${v.maximal}">${esc(v.letter)}: ${esc(v.label || "本地磁盘")} — 可压缩 ${human(v.maximal)}${tag}</option>`;
    }).join("") || `<option value="">没有可压缩的 NTFS 卷</option>`;
    updateShrinkRange();
  }

  function renderEfiSourceOptions() {
    const sel = $("#efiSource");
    const keep = sel.value;
    // 下拉里只放短标签，完整路径放到提示行 —— 一个几十字的绝对路径塞在 select 里必定被截断
    sel.innerHTML = `<option value="">（跳过，只建分区）</option>` +
      state.efiSources.map((s) =>
        `<option value="${esc(s.path)}"${s.path === keep ? " selected" : ""}>${esc(s.short || s.label)}</option>`).join("");
    if (state.efiSources.length) {
      if (!keep) sel.selectedIndex = 1;
      const chosen = state.efiSources.find((s) => s.path === sel.value) || state.efiSources[0];
      $("#efiSourceHint").textContent = `来源：${chosen.path}`;
    } else {
      $("#efiSourceHint").textContent = "还没有生成过 EFI，可以先走「一键生成 EFI」那条路线。";
    }
  }

  function renderRecoveryVersions() {
    fetch("/api/recovery/versions").then((r) => r.json()).then((d) => {
      const sel = $("#recoveryVersion");
      const keep = sel.value;
      sel.innerHTML = (d.versions || []).map((v) =>
        `<option value="${v.darwin}"${String(keep) === String(v.darwin) ? " selected" : ""}>${esc(v.name)}</option>`).join("");
      if (!keep && sel.options.length) sel.selectedIndex = 0;
    }).catch(() => {});
  }

  function loadEfiSources() {
    fetch("/api/efi/list").then((r) => r.json()).then((d) => {
      state.efiSources = d.sources || [];
      renderEfiSourceOptions();
    }).catch(() => {});
  }

  /* ---- 空间规划联动 ---- */

  function updateShrinkRange() {
    const mode = $("#shrinkMode").value;
    const volField = $("#shrinkVolumeField"), sizeField = $("#shrinkSizeField");
    if (mode === "tail") {
      volField.style.display = "none";
      const d = state.disks.find((x) => x.disk === state.selectedDisk);
      if (d) {
        const mb = Math.floor((d.tailFree || 0) / 1024 / 1024);
        $("#shrinkHint").textContent = `该盘尾部有 ${human(d.tailFree)} 未分配空间。`;
        if (mb > 0) {
          sizeField.style.display = "";
          const el = $("#shrinkSize");
          el.min = Math.min(20480, mb); el.max = Math.max(mb, 20480); el.value = mb;
          $("#shrinkSizeLabel").textContent = human(d.tailFree);
        }
      }
      updateSummary();
      return;
    }
    volField.style.display = "";
    sizeField.style.display = "";
    const opt = $("#shrinkVolume").selectedOptions[0];
    const max = Number(opt?.dataset?.max || 0);
    const maxMb = Math.floor(max / 1024 / 1024);
    const el = $("#shrinkSize");
    el.min = 20480;
    el.max = Math.max(maxMb, 20480);
    // 默认的建议值：够装系统 + 留余量，但不超过可压缩上限
    const suggest = Math.min(Math.max(102400, Math.floor(maxMb * 0.7)), maxMb);
    el.value = Math.min(suggest, maxMb);
    $("#shrinkSizeLabel").textContent = human(Number(el.value) * 1024 * 1024);
    $("#shrinkHint").textContent = opt
      ? `${opt.textContent.trim().split("—")[0].trim()} 最多可压缩出 ${human(max)}（由 diskpart 精确测得，已扣除页面文件等不可移动区域）。`
      : "";
    updateSummary();
  }

  function updateSummary() {
    const box = $("#planSummary");
    if (!box) return;
    const shrinkMb = Number($("#shrinkSize").value || 0);
    const espMb = Number($("#espSize").value || 1536);
    const mode = $("#installSizeMode").value;
    const installMb = mode === "custom" ? Number($("#installSizeMb").value || 0) : Math.max(0, shrinkMb - espMb);
    const need = espMb + installMb;
    const rows = [
      ["EFI 分区", human(espMb * 1024 * 1024)],
      ["macOS 安装分区", mode === "custom" ? human(installMb * 1024 * 1024) : `剩余全部 ≈ ${human(installMb * 1024 * 1024)}`],
      ["合计需要", human(need * 1024 * 1024)],
      ["本次腾出", human(shrinkMb * 1024 * 1024)],
    ];
    const ok = shrinkMb > 0 && need <= shrinkMb;
    box.innerHTML = `<dl class="kv" style="margin:6px 0 12px">` +
      rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + `</dl>` +
      (shrinkMb <= 0 ? `<div class="note note-warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/></svg><div>请先在上方选择要压缩的分区并设定腾出多少空间。</div></div>`
        : (ok ? `<div class="note note-ok"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m5 13 4 4L19 7"/></svg><div>空间规划通过。<b>现有分区的数据不会丢失</b> —— 只会把某个 NTFS 分区的尾部往回收。</div></div>`
          : `<div class="note note-error"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg><div>腾出的空间不足：需要 ${human(need * 1024 * 1024)}，当前只规划了 ${human(shrinkMb * 1024 * 1024)}。请调大压缩量或缩小分区规划。</div></div>`));
  }

  $("#shrinkMode").addEventListener("change", updateShrinkRange);
  $("#shrinkVolume").addEventListener("change", updateShrinkRange);
  $("#shrinkSize").addEventListener("input", () => {
    $("#shrinkSizeLabel").textContent = human(Number($("#shrinkSize").value) * 1024 * 1024);
    updateSummary();
  });
  $("#espSize").addEventListener("change", updateSummary);
  $("#installSizeMode").addEventListener("change", (e) => {
    $("#installSizeMb").style.display = e.target.value === "custom" ? "" : "none";
    updateSummary();
  });
  $("#installSizeMb").addEventListener("input", updateSummary);
  $("#skipRecovery").addEventListener("change", updateSummary);
  $("#efiSource").addEventListener("change", () => {
    const s = state.efiSources.find((x) => x.path === $("#efiSource").value);
    $("#efiSourceHint").textContent = s ? `来源：${s.path}` : "不会写入任何 EFI 文件。";
  });

  /* ---------------------------------------------------------- 路线二：执行 */

  /* ---- 执行按钮的形态 ----
     idle    红色「确认执行」
     running 红色禁用「正在执行…」
     ok      绿色「已执行完成」（禁用）+ 出现「重新执行」「重新规划」
     fail    红色「重试执行」

     之前执行完只是把按钮重新 enable 回来，红色的「确认执行」原样留着：
     用户看不出"已经做完了"，容易以为没开始而反复点。 */
  const INSTALL_ICON_PLAY =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l14 9-14 9z"/></svg>';
  const INSTALL_ICON_CHECK =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4 4L19 7"/></svg>';

  let installLastCfg = null;      // 上次成功跑完的配置，「重新执行」直接复用

  function setInstallState(kind) {
    const btn = $("#btnInstall");
    if (kind === "ok") {
      btn.className = "btn btn-ok";
      btn.innerHTML = INSTALL_ICON_CHECK + " 已执行完成";
      btn.disabled = true;
    } else if (kind === "running") {
      btn.className = "btn btn-danger";
      btn.innerHTML = INSTALL_ICON_PLAY + " 正在执行…";
      btn.disabled = true;
    } else if (kind === "fail") {
      btn.className = "btn btn-danger";
      btn.innerHTML = INSTALL_ICON_PLAY + " 重试执行（会改动分区）";
      btn.disabled = false;
    } else {
      btn.className = "btn btn-danger";
      btn.innerHTML = INSTALL_ICON_PLAY + " 确认执行（会改动分区）";
      btn.disabled = false;
    }
    const done = kind === "ok";
    $("#btnReinstall").style.display = done ? "" : "none";
    $("#btnReplan").style.display = done ? "" : "none";
  }

  function collectInstallCfg() {
    const d = state.disks.find((x) => x.disk === state.selectedDisk);
    if (!d) { toast("请先选择目标磁盘", "warn"); return null; }
    if ((d.issues || []).length) { toast("该磁盘存在阻塞项，无法继续", "error"); return null; }

    const shrinkMb = Number($("#shrinkSize").value || 0);
    if (shrinkMb <= 0) { toast("请先规划要腾出的空间", "warn"); return null; }

    const cfg = {
      diskNumber: state.selectedDisk,
      espSizeMb: Number($("#espSize").value || 1536),
      installSizeMb: $("#installSizeMode").value === "custom"
        ? Number($("#installSizeMb").value || 0) : null,
      shrink: $("#shrinkMode").value === "shrink"
        ? { letter: $("#shrinkVolume").value, mb: shrinkMb } : null,
      efiSource: $("#efiSource").value || "",
      darwin: Number($("#recoveryVersion").value || 24),
      skipRecovery: $("#skipRecovery").checked,
      // NullMoth 驱动包：解析出来的硬件里有受支持的 N 卡时才随分区一起放
      reportPath: state.report || "",
      stageDriver: $("#stageDriver").checked,
    };

    const need = cfg.espSizeMb + (cfg.installSizeMb || 0);
    if (need > shrinkMb) { toast("空间不足，请调整规划", "error"); return null; }
    return cfg;
  }

  function confirmAndRunInstall(cfg, rerun) {
    const d = state.disks.find((x) => x.disk === cfg.diskNumber);
    const head = rerun
      ? `按上次的设置重新执行一次（磁盘 ${cfg.diskNumber}）：\n\n`
      : `即将在「磁盘 ${cfg.diskNumber} — ${d ? d.name : ""}」上执行：\n\n`;
    const body =
      `· 新建一个 ${cfg.espSizeMb} MB 的 EFI 分区\n` +
      (cfg.shrink ? `· 把 ${cfg.shrink.letter}: 压缩 ${human((cfg.shrink.mb || 0) * 1024 * 1024)}（卷内数据不变）\n` : "") +
      `· 新建一个 macOS 安装分区\n` +
      (cfg.efiSource ? `· 写入 EFI 文件\n` : "") +
      (cfg.skipRecovery ? "" : `· 下载并写入 Apple 恢复镜像\n`);
    const tail = rerun
      ? `\n已建好的分区会被复用 / 合并覆盖。若磁盘已无剩余空间可压缩，这一步会失败 —— 那是正常的，请改用「重新规划」。`
      : `\n现有分区不会被删除。确认继续？`;
    if (!confirm(head + body + tail)) return false;
    runInstall(cfg);
    return true;
  }

  function runInstall(cfg) {
    state.installing = true;
    setInstallState("running");
    $("#btnCancelInstall").style.display = "";
    $("#instDoneCard").style.display = "none";
    window["steps:instStepsBar"] = [];
    $("#instStepsBar").innerHTML = "";
    $("#instBar").parentElement.classList.remove("done");
    setProgress("#instBar", "#instPct", "#instLabel", { value: 0, label: "准备中…" });
    show("install");
    logLine("开始无损安装流程…", "warn");

    let ok = false;
    stream("/api/install?cfg=" + encodeURIComponent(JSON.stringify(cfg)), {
      onEvent(ev) {
        if (ev.t === "progress") setProgress("#instBar", "#instPct", "#instLabel", ev);
        else if (ev.t === "step") renderSteps($("#instStepsBar"), ev);
        else if (ev.t === "log") logLine(ev.msg, ev.level);
        else if (ev.t === "result") {
          ok = true;
          installLastCfg = cfg;
          markStepsDone($("#instStepsBar"));
          $("#instBar").parentElement.classList.add("done");
          setProgress("#instBar", "#instPct", "#instLabel", { value: 100, label: "完成" });
          renderInstallResult(ev.data);
          // 结果卡片通常在当前视口之外，光靠一条 toast 很容易被忽略 —— 主动滚过去
          const card = $("#instDoneCard");
          if (card) setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "center" }), 80);
          toast("无损安装完成：EFI 分区已就绪", "ok");
          logLine("✔ 无损安装已完成：EFI 分区与 macOS 安装分区均已就绪", "ok");
        } else if (ev.t === "done" && ev.exit !== 0) {
          ok = false;
          toast("安装未完成，请查看日志", "error");
        }
      },
      onDone() {
        state.installing = false;
        $("#btnCancelInstall").style.display = "none";
        setInstallState(ok ? "ok" : "fail");
      },
    });
  }

  $("#btnInstall").addEventListener("click", () => {
    if (state.installing) return;
    const cfg = collectInstallCfg();
    if (cfg) confirmAndRunInstall(cfg, false);
  });

  $("#btnReinstall").addEventListener("click", () => {
    if (state.installing) return;
    if (!installLastCfg) return toast("没有可复用的上次设置，请用「重新规划」", "warn");
    confirmAndRunInstall(installLastCfg, true);
  });

  $("#btnReplan").addEventListener("click", () => {
    if (state.installing) return;
    $("#instDoneCard").style.display = "none";
    $("#instRunCard").style.display = "none";
    setInstallState("idle");
    const card = $("#planCard");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
    toast("已回到空间规划，改完再点「确认执行」", "info");
  });

  $("#btnCancelInstall").addEventListener("click", () => {
    toast("已请求取消（会等待当前步骤结束）", "warn");
    location.reload();
  });

  function renderInstallResult(d) {
    $("#instDoneCard").style.display = "";
    const rows = [
      ["EFI 分区", `#${d.espPartition}（${human(d.espSize)}）`],
      ["安装分区", d.installPartition ? `#${d.installPartition}` : "未创建"],
      ["恢复镜像", d.recoveryInstalled ? "已写入 Apple 官方恢复镜像" : "未写入"],
      ["NullMoth 驱动", d.driverInstalled ? "已放入 EFI 分区" : "未放入"],
    ];
    const driverNote = d.driverInstalled ? `
      <div class="note note-warn" style="margin-top:12px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 9v5M12 17.5v.5"/><circle cx="12" cy="12" r="9"/></svg>
        <div><b>显卡驱动要等 macOS 装好后才装。</b>EFI 分区的 <code>NullMoth\\README.txt</code>
        里写了三条命令，在 macOS 终端依次执行，重启后显卡才有输出。
        在那之前屏幕是黑的是正常的，不代表失败。</div>
      </div>` : "";
    $("#instResult").innerHTML = `<dl class="kv">` +
      rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + `</dl>` + driverNote + `
      <div class="divider"></div>
      <div class="note note-info"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg><div>
        <b>下一步：</b>重启电脑，进 BIOS 的启动菜单（华硕通常是 F8，微星 F11，戴尔 F12，惠普 F9），
        选择新出现的 EFI 启动项 → 进入 macOS 恢复环境 →
        打开「磁盘工具」把新分区<b>抹掉为 APFS</b> → 再选择「重新安装 macOS」。<br><br>
        如果没能出现启动项，EFI 分区里已经放了 <code>EFI/BOOT/BOOTx64.efi</code> 兜底文件，
        某些主板需要在 BIOS 里手动指定该文件启动。
      </div></div>`;
  }

  /* ---------------------------------------------------------- 日志 */

  $("#btnClearLog").addEventListener("click", () => { $("#console").innerHTML = ""; });

  $("#btnCopyLog").addEventListener("click", async () => {
    const text = Array.from($("#console").children)
      .map((n) => n.textContent.replace(/^\S+\s/, "")).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("日志已复制", "ok");
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      toast("日志已复制", "ok");
    }
  });

  /* ---------------------------------------------------------- 启动 */

  loadHealth();
  loadDisks();
  logLine("OpenHack 已就绪", "ok");
  show(viewFromHash());
  window.addEventListener("hashchange", () => show(viewFromHash()));
  window.addEventListener("error", (e) => logLine("前端错误：" + e.message, "error"));
})();
