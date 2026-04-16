// Echo 655 Control UI
// Single-page app talking to the FastAPI backend at /api/*

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  connected: false,
  srcPlates: [],         // PlateInfo[]
  dstPlates: [],         // PlateInfo[]
  currentSrc: "",        // current loaded src plate type name
  currentDst: "",
  parsedPicklist: null,  // last parsed picklist
  // Gripper position per side: "in" (retracted) or "out" (extended).
  // Tracked locally based on user actions — reset to "in" on connect.
  // The DIO flag that corresponds to gripper-extended state isn't yet
  // confirmed from captures; until it is, this is advisory. The Echo will
  // still reject invalid calls via SOAP Fault if the state is wrong.
  gripper: { src: "in", dst: "in" },
};

// ---------- HTTP helpers ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) msg = body.detail;
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}
const apiGet = (p) => api(p);
const apiPost = (p, body) =>
  api(p, { method: "POST", body: body ? JSON.stringify(body) : undefined });

// ---------- Status bar ----------

function setStatus(text, kind = "ready") {
  const el = $("#sb-status");
  el.textContent = text;
  el.className = "sb-status " + (kind === "ready" ? "" : kind);
}

async function busy(label, fn) {
  setStatus(label, "busy");
  try {
    const r = await fn();
    setStatus("Ready", "ready");
    return r;
  } catch (e) {
    setStatus(e.message || "Error", "error");
    log(`ERROR: ${e.message}`);
    throw e;
  }
}

// ---------- Log pane ----------

function log(line) {
  const el = $("#log");
  el.textContent += line + "\n";
  el.scrollTop = el.scrollHeight;
  // Cap log size
  const max = 2000;
  const lines = el.textContent.split("\n");
  if (lines.length > max) {
    el.textContent = lines.slice(-max).join("\n");
  }
}

$("#log-clear").addEventListener("click", () => {
  $("#log").textContent = "";
});

// ---------- SSE event stream ----------

let evtSrc = null;

function openEventStream() {
  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource("/api/events");
  evtSrc.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "echo_event") {
        log(msg.payload);
      } else if (msg.type === "hello") {
        log("[ui] SSE connected");
      } else if (msg.type === "connected") {
        log(`[ui] Connected to ${msg.ip}`);
      } else if (msg.type === "disconnected") {
        log("[ui] Disconnected");
      }
    } catch {}
  };
  evtSrc.onerror = () => {
    // Browser will auto-reconnect
  };
}

// ---------- Connect flow ----------

$("#connect-btn").addEventListener("click", async () => {
  const ip = $("#connect-ip").value.trim();
  if (!ip) return;
  const errEl = $("#connect-error");
  errEl.textContent = "";
  $("#connect-btn").disabled = true;
  try {
    const r = await apiPost("/api/connect", { ip });
    onConnected(r);
  } catch (e) {
    errEl.textContent = e.message || "Connect failed";
  } finally {
    $("#connect-btn").disabled = false;
  }
});

$("#disconnect-btn").addEventListener("click", async () => {
  try {
    await apiPost("/api/disconnect");
  } catch {}
  state.connected = false;
  $("#app").classList.add("hidden");
  $("#connect-overlay").classList.remove("overlay");  // noop
  $("#connect-overlay").style.display = "flex";
});

async function onConnected(r) {
  state.connected = true;
  $("#connect-overlay").style.display = "none";
  $("#app").classList.remove("hidden");
  $("#sb-instrument").textContent = r.info.instrument_status ? `${r.ip}` : r.ip;
  $("#sb-serial").textContent = r.info.serial_number || "—";
  $("#sb-version").textContent = r.info.software_version || "—";
  openEventStream();

  // Assume both grippers retracted on connect. If another client has
  // driven the Echo otherwise, the user will see the Eject call fault
  // and can manually retract.
  state.gripper.src = "in";
  state.gripper.dst = "in";
  updateGripperButtons();

  // Load labware list and current plates
  await Promise.all([loadPlateLists(), refreshSidebar()]);
  startPolling();
}

// ---------- Tab management ----------

$$(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".tab-panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("#tab-" + t.dataset.tab).classList.add("active");
  })
);

// ---------- Controls ----------

$("#home-btn").addEventListener("click", async () => {
  await busy("Homing…", () => apiPost("/api/home"));
});

$("#dry-btn").addEventListener("click", async () => {
  await busy("Drying plate…", () => apiPost("/api/dry", { dry_type: "TWO_PASS" }));
});

$("#pump-toggle").addEventListener("change", async (e) => {
  await busy("Setting pump…", () =>
    apiPost("/api/coupling-fluid/pump", { value: e.target.checked })
  );
});

$$("input[name=nozzle]").forEach((r) =>
  r.addEventListener("change", async (e) => {
    await busy("Setting nozzle…", () =>
      apiPost("/api/coupling-fluid/nozzle", { value: e.target.value === "up" })
    );
  })
);

$$("input[name=pumpdir]").forEach((r) =>
  r.addEventListener("change", async (e) => {
    await busy("Setting pump direction…", () =>
      apiPost("/api/coupling-fluid/pump-dir", {
        value: e.target.value === "normal",
      })
    );
  })
);

$("#ionizer-toggle").addEventListener("change", async (e) => {
  await busy("Setting ionizer…", () =>
    apiPost("/api/ionizer", { value: e.target.checked })
  );
});

$("#vacuum-toggle").addEventListener("change", async (e) => {
  await busy("Setting vacuum pump…", () =>
    apiPost("/api/vacuum/pump", { value: e.target.checked })
  );
});

$("#door-open").addEventListener("click", async () => {
  await busy("Opening door…", () => apiPost("/api/door/open"));
  await refreshSidebar();
});

$("#door-close").addEventListener("click", async () => {
  await busy("Closing door…", () => apiPost("/api/door/close"));
  await refreshSidebar();
});

// ---------- Gripper button state ----------
// Enforce:
//   - Eject (extend) disabled while gripper is already out
//   - Load (retract) disabled while gripper is in
//   - Dest Eject additionally disabled while Source gripper is out
//     (Echo state interlock: dest gripper can't extend with src out —
//      returns SOAP Fault "Unable to proceed. Source plate gripper must
//      be inside the instrument.")

function updateGripperButtons() {
  const srcOut = state.gripper.src === "out";
  const dstOut = state.gripper.dst === "out";

  const btns = {
    extendSrc: document.querySelector('[data-action="extend-src"]'),
    loadSrc:   document.querySelector('[data-action="load-src-prompt"]'),
    extendDst: document.querySelector('[data-action="extend-dst"]'),
    loadDst:   document.querySelector('[data-action="load-dst-prompt"]'),
  };
  if (!btns.extendSrc) return;

  // Source gripper
  btns.extendSrc.disabled = srcOut;
  btns.extendSrc.title = srcOut ? "Source gripper is already extended" : "";
  btns.loadSrc.disabled = !srcOut;
  btns.loadSrc.title = srcOut ? "" : "Extend (Eject) the gripper first";

  // Destination gripper, with src-out interlock
  btns.extendDst.disabled = dstOut || srcOut;
  btns.extendDst.title = srcOut
    ? "Source gripper must be retracted first"
    : (dstOut ? "Destination gripper is already extended" : "");
  btns.loadDst.disabled = !dstOut;
  btns.loadDst.title = dstOut ? "" : "Extend (Eject) the gripper first";
}

// Plate action buttons in sidebar.
// Eject = extend gripper only (user removes the plate).
// Load = open modal asking what's on the stage now, then retract.
$$("[data-action]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const a = btn.dataset.action;
    if (a === "extend-src") {
      await busy("Extending source gripper…", () =>
        apiPost("/api/plates/src/extend")
      );
      state.gripper.src = "out";
      updateGripperButtons();
      await refreshSidebar();
    } else if (a === "extend-dst") {
      await busy("Extending destination gripper…", () =>
        apiPost("/api/plates/dst/extend")
      );
      state.gripper.dst = "out";
      updateGripperButtons();
      await refreshSidebar();
    } else if (a === "load-src-prompt") {
      openLoadModal("src");
    } else if (a === "load-dst-prompt") {
      openLoadModal("dst");
    }
  })
);

// ---------- Load modal ----------
// Opens after the user has ejected (extended gripper) and physically
// placed/removed the plate. Asks what's now on the stage, then retracts
// the gripper declaring that plate type (or None for empty).

function openLoadModal(kind) {
  const modal = $("#load-modal");
  const title = $("#load-title");
  const select = $("#load-plate-type");
  title.textContent =
    kind === "src" ? "Source Plate — Retract Gripper" : "Destination Plate — Retract Gripper";
  const plates = kind === "src" ? state.srcPlates : state.dstPlates;
  // "None" first (empty / ejected), then all known plate types
  const options = [
    `<option value="None">None (empty / no plate)</option>`,
    ...plates.map((p) => `<option value="${p.name}">${p.name}</option>`),
  ];
  select.innerHTML = options.join("");
  modal.dataset.kind = kind;
  modal.classList.remove("hidden");
}

$("#load-cancel").addEventListener("click", () =>
  $("#load-modal").classList.add("hidden")
);

$("#load-go").addEventListener("click", async () => {
  const modal = $("#load-modal");
  const kind = modal.dataset.kind;
  const plateType = $("#load-plate-type").value;
  modal.classList.add("hidden");
  const endpoint =
    kind === "src" ? "/api/plates/src/retract" : "/api/plates/dst/retract";
  const label =
    plateType === "None"
      ? `Retracting ${kind} gripper (empty)…`
      : `Retracting ${kind} gripper with ${plateType}…`;
  try {
    const r = await busy(label, () =>
      apiPost(endpoint, { plate_type: plateType })
    );
    // Only flip gripper state on success — if retract fails the gripper
    // may still be out.
    state.gripper[kind] = "in";
    updateGripperButtons();
    if (r.barcode && r.barcode.startsWith("Barcode Reading Error")) {
      log(`[ui] ${kind} retract: no barcode read (plate may be empty)`);
    } else if (r.barcode) {
      log(`[ui] ${kind} retract: barcode=${r.barcode}`);
    }
  } finally {
    await refreshSidebar();
  }
});

// ---------- Labware tab ----------

async function loadPlateLists() {
  const [src, dst] = await Promise.all([
    apiGet("/api/plates/src"),
    apiGet("/api/plates/dst"),
  ]);
  state.srcPlates = src.plates;
  state.dstPlates = dst.plates;

  renderPlateTable("#src-plate-table tbody", src.plates);
  renderPlateTable("#dst-plate-table tbody", dst.plates);

  // Populate selects
  const fillSelect = (sel, list) => {
    sel.innerHTML = list
      .map((p) => `<option value="${p.name}">${p.name}</option>`)
      .join("");
  };
  fillSelect($("#survey-plate"), src.plates);
  fillSelect($("#transfer-src-type"), src.plates);
  fillSelect($("#transfer-dst-type"), dst.plates);
  fillSelect($("#picklist-src-type"), src.plates);
  fillSelect($("#picklist-dst-type"), dst.plates);
}

function renderPlateTable(tbodySel, plates) {
  $(tbodySel).innerHTML = plates
    .map(
      (p) =>
        `<tr><td>${p.name}</td><td>${p.rows * p.cols}</td><td>${p.fluid || ""}</td>` +
        `<td>${p.plate_format || ""}</td><td>${p.well_capacity || ""}</td></tr>`
    )
    .join("");
}

// ---------- Survey tab ----------

$("#survey-btn").addEventListener("click", async () => {
  const plateType = $("#survey-plate").value;
  const r = await busy("Running survey (~23 s)…", () =>
    apiPost("/api/survey", { plate_type: plateType })
  );
  renderSurvey(r);
});

function renderSurvey(r) {
  // Stats
  const vols = r.wells.map((w) => w.volume_nL).filter((v) => v > 0);
  if (vols.length === 0) {
    $("#survey-stats").textContent = `${r.wells.length} wells reported but no volumes.`;
  } else {
    const min = Math.min(...vols), max = Math.max(...vols);
    const avg = vols.reduce((s, v) => s + v, 0) / vols.length;
    const sd = Math.sqrt(vols.reduce((s, v) => s + (v - avg) ** 2, 0) / vols.length);
    const cv = avg ? ((sd / avg) * 100).toFixed(1) : "0";
    $("#survey-stats").innerHTML =
      `<b>${r.plate_type}</b> ${r.rows}×${r.cols} | ` +
      `wells: ${r.wells.length}/${r.total_wells} | ` +
      `min: ${min.toFixed(1)} nL, max: ${max.toFixed(1)} nL, ` +
      `avg: ${avg.toFixed(1)} nL, SD: ${sd.toFixed(1)} nL, CV: ${cv}%`;
  }

  // Grid
  const wrap = $("#survey-grid-wrap");
  const cols = r.cols;
  const wellByRC = new Map();
  r.wells.forEach((w) => wellByRC.set(`${w.row},${w.col}`, w));
  const grid = document.createElement("div");
  grid.className = "well-grid";
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(22px, 1fr))`;
  for (let row = 0; row < r.rows; row++) {
    for (let col = 0; col < cols; col++) {
      const w = wellByRC.get(`${row},${col}`);
      const cell = document.createElement("div");
      cell.className = "well";
      if (!w || w.volume_nL === 0) {
        cell.classList.add("empty");
        cell.title = w ? `${w.name}: empty` : "";
        cell.textContent = "";
      } else {
        cell.classList.add("good");
        cell.title = `${w.name}: ${w.volume_nL.toFixed(1)} nL (${w.fluid})`;
        cell.textContent = Math.round(w.volume_nL);
      }
      grid.appendChild(cell);
    }
  }
  wrap.innerHTML = "";
  wrap.appendChild(grid);
}

// ---------- Transfer tab (manual) ----------

function addTransferRow(tbodySel, src = "", dst = "", vol = "") {
  const tr = document.createElement("tr");
  tr.innerHTML =
    `<td><input class="src" value="${src}"></td>` +
    `<td><input class="dst" value="${dst}"></td>` +
    `<td><input class="vol" type="number" step="0.5" value="${vol}"></td>` +
    `<td><button class="small ghost remove">×</button></td>`;
  $(tbodySel).appendChild(tr);
  tr.querySelector(".remove").addEventListener("click", () => tr.remove());
  return tr;
}

$("#transfer-add-row").addEventListener("click", () =>
  addTransferRow("#transfer-table tbody")
);

// Seed with two empty rows
addTransferRow("#transfer-table tbody");
addTransferRow("#transfer-table tbody");

$("#transfer-run").addEventListener("click", async () => {
  const rows = $$("#transfer-table tbody tr");
  const transfers = rows
    .map((tr) => ({
      src: tr.querySelector(".src").value.trim(),
      dst: tr.querySelector(".dst").value.trim(),
      volume_nL: parseFloat(tr.querySelector(".vol").value),
    }))
    .filter((t) => t.src && t.dst && !isNaN(t.volume_nL));
  if (transfers.length === 0) {
    alert("No valid transfer rows.");
    return;
  }
  const body = {
    src_plate_type: $("#transfer-src-type").value,
    dst_plate_type: $("#transfer-dst-type").value,
    transfers,
    do_survey: $("#transfer-do-survey").checked,
    close_door: $("#transfer-close-door").checked,
    protocol_name: "ui-manual",
  };
  const r = await busy("Running transfer…", () => apiPost("/api/transfer", body));
  renderTransferResult("#transfer-result", r);
});

function renderTransferResult(sel, r) {
  const el = $(sel);
  let html =
    `<div class="stats"><b>${r.succeeded ? "OK" : "FAILED"}:</b> ${r.status} — ` +
    `${r.transfers.length} transferred, ${r.skipped.length} skipped</div>`;
  if (r.transfers.length) {
    html += `<table class="plate-table"><thead><tr>` +
      `<th>Src</th><th>Dst</th><th>Requested nL</th><th>Actual nL</th><th>Fluid</th><th>%</th></tr></thead><tbody>` +
      r.transfers
        .map(
          (t) =>
            `<tr><td>${t.source}</td><td>${t.dest}</td>` +
            `<td>${t.volume_nL.toFixed(1)}</td>` +
            `<td>${t.actual_volume_nL.toFixed(1)}</td>` +
            `<td>${t.fluid}</td><td>${t.composition.toFixed(1)}</td></tr>`
        )
        .join("") +
      `</tbody></table>`;
  }
  if (r.skipped.length) {
    html += `<div class="stats"><b>Skipped:</b></div>` +
      `<table class="plate-table"><thead><tr>` +
      `<th>Src</th><th>Dst</th><th>nL</th><th>Reason</th></tr></thead><tbody>` +
      r.skipped
        .map(
          (s) =>
            `<tr><td>${s.source}</td><td>${s.dest}</td>` +
            `<td>${s.volume_nL.toFixed(1)}</td><td>${s.reason}</td></tr>`
        )
        .join("") +
      `</tbody></table>`;
  }
  el.innerHTML = html;
}

// ---------- Picklist tab ----------

$("#picklist-parse-btn").addEventListener("click", async () => {
  const f = $("#picklist-file").files[0];
  if (!f) { alert("Pick a file first."); return; }
  const form = new FormData();
  form.append("file", f);
  const res = await fetch("/api/picklist/parse", { method: "POST", body: form });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    alert(`Parse failed: ${body.detail}`);
    return;
  }
  const parsed = await res.json();
  state.parsedPicklist = parsed;
  renderPicklistPreview(parsed);
  $("#picklist-run").disabled = parsed.transfers.length === 0;
});

function renderPicklistPreview(parsed) {
  const n = parsed.transfers.length;
  let summary = `<b>${parsed.filename}</b>: ${n} transfers`;
  if (parsed.src_plate_type) summary += ` | CSV source plate: ${parsed.src_plate_type}`;
  if (parsed.dst_plate_type) summary += ` | CSV dest plate: ${parsed.dst_plate_type}`;
  const totalNL = parsed.transfers.reduce((s, t) => s + t.volume_nL, 0);
  summary += ` | total volume: ${totalNL.toFixed(1)} nL`;
  $("#picklist-summary").innerHTML = summary;

  // Pre-select plate types from CSV if present
  if (parsed.src_plate_type) {
    const sel = $("#picklist-src-type");
    if ([...sel.options].some((o) => o.value === parsed.src_plate_type)) {
      sel.value = parsed.src_plate_type;
    }
  }
  if (parsed.dst_plate_type) {
    const sel = $("#picklist-dst-type");
    if ([...sel.options].some((o) => o.value === parsed.dst_plate_type)) {
      sel.value = parsed.dst_plate_type;
    }
  }

  const rows = parsed.transfers
    .slice(0, 500) // cap preview
    .map(
      (t, i) =>
        `<tr><td>${i + 1}</td><td>${t.src}</td><td>${t.dst}</td><td>${t.volume_nL}</td></tr>`
    )
    .join("");
  $("#picklist-table tbody").innerHTML = rows;
}

$("#picklist-run").addEventListener("click", async () => {
  if (!state.parsedPicklist) return;
  const body = {
    src_plate_type: $("#picklist-src-type").value,
    dst_plate_type: $("#picklist-dst-type").value,
    transfers: state.parsedPicklist.transfers,
    do_survey: $("#picklist-do-survey").checked,
    close_door: true,
    protocol_name: "ui-picklist",
  };
  const r = await busy(
    `Running picklist (${body.transfers.length} transfers)…`,
    () => apiPost("/api/transfer", body)
  );
  renderTransferResult("#picklist-result", r);
});

// ---------- Sidebar live status ----------

async function refreshSidebar() {
  try {
    const r = await apiGet("/api/status");
    if (!r.connected) return;
    const cur = await apiGet("/api/plates/current");
    state.currentSrc = cur.src;
    state.currentDst = cur.dst;
    $("#src-plate-name").textContent =
      (cur.src && cur.src !== "None") ? cur.src : "None";
    $("#dst-plate-name").textContent =
      (cur.dst && cur.dst !== "None") ? cur.dst : "None";

    $("#src-plate-led").className = "led " +
      (r.dio.SPP === 1 ? "on" : "");
    $("#dst-plate-led").className = "led " +
      (r.dio.DPP === 1 ? "on" : "");

    renderStatusGrid(r.dio);
    $("#temp-coupling").textContent = r.dio.coupling_fluid_temp.toFixed(1);
    $("#temp-system").textContent = r.dio.rf_subsystem_temp.toFixed(1);
  } catch (e) {
    // silently skip (might be in the middle of a blocking op)
  }
}

function renderStatusGrid(dio) {
  // DIO flags and how to interpret them (True = the "good" state for most)
  const items = [
    ["Coupling Fluid", dio.CFE],
    ["Motor At Pos", dio.MAP],
    ["Source Plate", dio.SPP === 1],
    ["Dest Plate", dio.DPP === 1],
    ["Coupling Lvl", dio.raw.CDAP === "True"],
    ["Air Pressure", dio.raw.IBUP === "True"],
    ["Fluid Door", dio.raw.DFC === "True"],
    ["Focus Cal", dio.raw.FCD !== "True"],
  ];
  $("#status-grid").innerHTML = items
    .map(
      ([label, ok]) =>
        `<div class="status-item"><span>${label}</span>` +
        `<span class="led ${ok ? "on" : ""}"></span></div>`
    )
    .join("");
}

let pollTimer = null;
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshSidebar, 5000);
}

// ---------- Init ----------

(async () => {
  // If already connected (page reload), re-attach
  try {
    const r = await apiGet("/api/status");
    if (r.connected) {
      onConnected({ info: r.info, ip: r.ip });
    }
  } catch {}
})();
