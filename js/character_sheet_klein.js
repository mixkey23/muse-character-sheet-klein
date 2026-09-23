// [2026-09-19] Sibling of Muse-CharacterSheet-Director's JS - same confirm/
// re-roll session-state UI architecture, ported as-is (it's entirely
// model-family-agnostic: DOM widgets, state_json persistence, sizing fixes).
// Only the pose aspect ratios, the override->widget map (no LoRA widgets
// here), and the node/extension names differ from the Krea2 version.
const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

const POSE_LABELS = ["Portrait (close-up)", "Front", "Left profile", "Right profile", "Back"];
const POSE_ASPECT = ["544 / 976", "544 / 1792", "544 / 1792", "544 / 1792", "544 / 1792"];
const DEFAULT_SEEDS = [41001, 41002, 41003, 41004, 41005];

function defaultState() {
  return {
    seeds: [...DEFAULT_SEEDS],
    confirmed: [false, false, false, false, false],
    prompts: ["", "", "", "", ""],
    previews: [null, null, null, null, null],
    // [2026-09-20] Tracks which poses currently have an active edit applied
    // (the instruction text, or null) - purely client-side, Python never
    // echoes this back. It's what "New seed" checks to decide whether to
    // re-roll the active edit (see rerollBtn.onclick) or do a normal base
    // regeneration. Cleared whenever the base prompt is changed via "Apply
    // prompt", since that's a deliberate "start fresh" action.
    editInstructions: [null, null, null, null, null],
    status: null,
    action: null,
  };
}

function hideWidget(widget) {
  if (!widget) return;
  widget.hidden = true;
  widget.options ??= {};
  widget.options.hidden = true;
  widget.computeSize = () => [0, -4];
  widget.draw = () => {};
  if (widget.element) widget.element.style.display = "none";
}

function setWidgetValue(widget, value) {
  if (!widget) return;
  widget.value = value;
  widget.callback?.(value);
}

// [2026-09-19] No LoRA widgets on this node (Klein's own tested pipeline
// doesn't use any) - model_override only dims unet_name.
const OVERRIDE_WIDGET_MAP = {
  model_override: ["unet_name"],
  clip_override: ["clip_name"],
  vae_override: ["vae_name"],
};

function applyOverrideState(node) {
  for (const [inputName, widgetNames] of Object.entries(OVERRIDE_WIDGET_MAP)) {
    const input = (node.inputs || []).find((inp) => inp.name === inputName);
    const connected = !!input?.link;
    for (const wName of widgetNames) {
      const widget = (node.widgets || []).find((w) => w.name === wName);
      if (!widget) continue;
      widget.disabled = connected;
      widget.options ??= {};
      widget.options.disabled = connected;
    }
  }
  node.setDirtyCanvas?.(true, true);
}

function enableCanvasZoomOverDOM(root) {
  root.addEventListener(
    "wheel",
    (event) => {
      const target = event.target;
      const interactive = target?.closest?.("textarea, select, input, button, [contenteditable='true']");
      if (interactive) return;
      const canvas = app?.canvas?.canvas;
      if (!canvas) return;
      event.preventDefault();
      event.stopPropagation();
      canvas.dispatchEvent(
        new WheelEvent("wheel", {
          deltaX: event.deltaX,
          deltaY: event.deltaY,
          deltaZ: event.deltaZ,
          deltaMode: event.deltaMode,
          clientX: event.clientX,
          clientY: event.clientY,
          bubbles: true,
          cancelable: true,
        })
      );
    },
    { passive: false }
  );
}

function getState(stateWidget) {
  try {
    const parsed = JSON.parse(stateWidget.value || "{}");
    return {
      seeds: Array.isArray(parsed.seeds) && parsed.seeds.length === 5 ? parsed.seeds : [...DEFAULT_SEEDS],
      confirmed:
        Array.isArray(parsed.confirmed) && parsed.confirmed.length === 5
          ? parsed.confirmed
          : [false, false, false, false, false],
      prompts:
        Array.isArray(parsed.prompts) && parsed.prompts.length === 5
          ? parsed.prompts
          : ["", "", "", "", ""],
      previews:
        Array.isArray(parsed.previews) && parsed.previews.length === 5
          ? parsed.previews
          : [null, null, null, null, null],
      editInstructions:
        Array.isArray(parsed.editInstructions) && parsed.editInstructions.length === 5
          ? parsed.editInstructions
          : [null, null, null, null, null],
      status: parsed.status || null,
      action: parsed.action || null,
    };
  } catch (e) {
    return defaultState();
  }
}

function setState(stateWidget, state) {
  setWidgetValue(stateWidget, JSON.stringify(state));
}

function describeStatus(status) {
  switch (status) {
    case "generating":
      return "Generating poses…";
    case "ready":
      return "All poses generated — review, confirm or re-roll each one.";
    case "not_all_confirmed":
      return "Confirm every pose before building the final sheet.";
    case "finalized":
      return "Done — character sheet ready below.";
    default:
      return "Queue the node to generate all five poses.";
  }
}

function populateSelect(select, widget, fallbackOptions) {
  const options = widget?.options?.values || fallbackOptions || [];
  select.innerHTML = "";
  for (const opt of options) {
    const el = document.createElement("option");
    el.value = opt;
    el.textContent = opt;
    select.appendChild(el);
  }
  if (widget) select.value = widget.value;
}

function buildFaceDetailBox(faceWidgets) {
  const { faceDetail, faceDetailType, faceDetailSampler, faceDetailScheduler, faceDetailDenoise } = faceWidgets;

  const box = document.createElement("div");
  box.style.cssText =
    "display:flex;flex-direction:column;gap:6px;padding:8px;background:#111114;" +
    "border:1px solid #3f3f46;border-radius:6px;box-sizing:border-box;";

  const headerRow = document.createElement("label");
  headerRow.style.cssText = "display:flex;align-items:center;gap:6px;font-weight:600;font-size:12px;color:#d4d4d8;cursor:pointer;";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = !!faceDetail?.value;
  toggle.style.cssText = "width:14px;height:14px;cursor:pointer;";
  const headerText = document.createElement("span");
  headerText.textContent = "Face Detail";
  headerRow.appendChild(toggle);
  headerRow.appendChild(headerText);
  box.appendChild(headerRow);

  const fieldsRow = document.createElement("div");
  fieldsRow.style.cssText = "display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:6px;";
  box.appendChild(fieldsRow);

  function makeField(labelText) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;flex-direction:column;gap:2px;";
    const label = document.createElement("div");
    label.textContent = labelText;
    label.style.cssText = "font-size:9px;color:#71717a;text-transform:uppercase;letter-spacing:0.03em;";
    wrap.appendChild(label);
    fieldsRow.appendChild(wrap);
    return wrap;
  }

  const selectStyle =
    "width:100%;padding:3px 4px;border-radius:4px;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;font-size:11px;box-sizing:border-box;";

  const detectWrap = makeField("Detect");
  const detectSelect = document.createElement("select");
  detectSelect.style.cssText = selectStyle;
  populateSelect(detectSelect, faceDetailType, ["face", "hand", "person"]);
  detectWrap.appendChild(detectSelect);

  const samplerWrap = makeField("Sampler");
  const samplerSelect = document.createElement("select");
  samplerSelect.style.cssText = selectStyle;
  populateSelect(samplerSelect, faceDetailSampler);
  samplerWrap.appendChild(samplerSelect);

  const schedulerWrap = makeField("Scheduler");
  const schedulerSelect = document.createElement("select");
  schedulerSelect.style.cssText = selectStyle;
  populateSelect(schedulerSelect, faceDetailScheduler);
  schedulerWrap.appendChild(schedulerSelect);

  const denoiseWrap = makeField("Denoise");
  const denoiseInput = document.createElement("input");
  denoiseInput.type = "number";
  denoiseInput.min = "0";
  denoiseInput.max = "1";
  denoiseInput.step = "0.01";
  denoiseInput.value = faceDetailDenoise ? faceDetailDenoise.value : 0.3;
  denoiseInput.style.cssText = selectStyle;
  denoiseWrap.appendChild(denoiseInput);

  function setEnabled(enabled) {
    for (const el of [detectSelect, samplerSelect, schedulerSelect, denoiseInput]) {
      el.disabled = !enabled;
      el.style.opacity = enabled ? "1" : "0.45";
    }
  }
  setEnabled(toggle.checked);

  toggle.onchange = () => {
    setWidgetValue(faceDetail, toggle.checked);
    setEnabled(toggle.checked);
  };
  detectSelect.onchange = () => setWidgetValue(faceDetailType, detectSelect.value);
  samplerSelect.onchange = () => setWidgetValue(faceDetailSampler, samplerSelect.value);
  schedulerSelect.onchange = () => setWidgetValue(faceDetailScheduler, schedulerSelect.value);
  denoiseInput.onchange = () => {
    const v = Math.min(1, Math.max(0, parseFloat(denoiseInput.value) || 0));
    denoiseInput.value = v;
    setWidgetValue(faceDetailDenoise, v);
  };

  function sync() {
    toggle.checked = !!faceDetail?.value;
    if (faceDetailType) detectSelect.value = faceDetailType.value;
    if (faceDetailSampler) samplerSelect.value = faceDetailSampler.value;
    if (faceDetailScheduler) schedulerSelect.value = faceDetailScheduler.value;
    if (faceDetailDenoise) denoiseInput.value = faceDetailDenoise.value;
    setEnabled(toggle.checked);
  }

  return { box, sync };
}

function buildCharacterSheetUI(node, stateWidget, faceWidgets) {
  const root = document.createElement("div");
  root.style.cssText =
    "display:flex;flex-direction:column;gap:8px;padding:8px 8px 24px 8px;background:#1b1b1f;" +
    "color:#e4e4e7;font-family:-apple-system,Segoe UI,sans-serif;font-size:12px;width:100%;" +
    "box-sizing:border-box;";

  const statusEl = document.createElement("div");
  statusEl.style.cssText = "font-weight:600;color:#9ca3af;min-height:16px;";
  statusEl.textContent = describeStatus(null);
  root.appendChild(statusEl);

  const faceDetailBox = buildFaceDetailBox(faceWidgets);
  root.appendChild(faceDetailBox.box);

  const grid = document.createElement("div");
  grid.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;";
  root.appendChild(grid);

  const panels = [];
  for (let i = 0; i < 5; i++) {
    const card = document.createElement("div");
    card.style.cssText =
      "flex:1 1 150px;min-width:140px;max-width:220px;display:flex;flex-direction:column;gap:4px;" +
      "background:#111114;border:2px solid #2c2c33;border-radius:6px;padding:6px;box-sizing:border-box;";

    const label = document.createElement("div");
    label.textContent = POSE_LABELS[i];
    label.style.cssText = "font-weight:600;font-size:11px;color:#d4d4d8;";
    card.appendChild(label);

    const imgWrap = document.createElement("div");
    imgWrap.style.cssText = `width:100%;aspect-ratio:${POSE_ASPECT[i]};background:#000;border-radius:4px;overflow:hidden;display:flex;align-items:center;justify-content:center;position:relative;`;
    const img = document.createElement("img");
    img.style.cssText = "width:100%;height:100%;object-fit:contain;display:none;";
    imgWrap.appendChild(img);
    const placeholder = document.createElement("div");
    placeholder.textContent = "not generated yet";
    placeholder.style.cssText = "color:#52525b;font-size:10px;text-align:center;padding:4px;";
    imgWrap.appendChild(placeholder);
    card.appendChild(imgWrap);

    const seedLabel = document.createElement("div");
    seedLabel.style.cssText = "font-size:10px;color:#71717a;";
    seedLabel.textContent = `seed ${DEFAULT_SEEDS[i]}`;
    card.appendChild(seedLabel);

    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:4px;";

    const confirmBtn = document.createElement("button");
    confirmBtn.textContent = "Confirm";
    confirmBtn.style.cssText =
      "flex:1;min-height:32px;padding:5px 3px;border-radius:4px;border:1px solid #22c55e;background:#166534;color:#ffffff;cursor:pointer;font-size:12px;";

    const rerollBtn = document.createElement("button");
    rerollBtn.textContent = "\u{1F3B2} New seed";
    rerollBtn.style.cssText =
      "flex:1;min-height:32px;padding:5px 3px;border-radius:4px;border:1px solid #60a5fa;background:#1d4ed8;color:#ffffff;cursor:pointer;font-size:12px;";

    btnRow.appendChild(confirmBtn);
    btnRow.appendChild(rerollBtn);
    card.appendChild(btnRow);

    const promptToggle = document.createElement("button");
    promptToggle.textContent = "Show prompt ▾";
    promptToggle.style.cssText =
      "padding:3px 2px;border-radius:4px;border:1px solid #2c2c33;background:transparent;color:#71717a;cursor:pointer;font-size:10px;";
    card.appendChild(promptToggle);

    const promptWrap = document.createElement("div");
    promptWrap.style.cssText = "display:none;flex-direction:column;gap:4px;";
    const promptArea = document.createElement("textarea");
    promptArea.style.cssText =
      "width:100%;min-height:110px;resize:vertical;padding:4px;border-radius:4px;border:1px solid #3f3f46;" +
      "background:#0b0b0d;color:#d4d4d8;font-size:10px;font-family:inherit;box-sizing:border-box;";
    const applyBtn = document.createElement("button");
    applyBtn.textContent = "Apply prompt (same seed)";
    applyBtn.style.cssText =
      "padding:4px 2px;border-radius:4px;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;cursor:pointer;font-size:11px;";
    // [2026-09-17] Andy: "even if you change it and mess it all up, you can
    // hit default prompt and it will do that." Reverts THIS pose's prompt to
    // its built-in default and regenerates with it - not just a text-field
    // undo, since the default text only genuinely lives server-side
    // (DEFAULT_PROMPTS/POSE_PROMPTS in the Python) and duplicating it here
    // would risk drifting out of sync if that text is ever edited.
    const resetPromptBtn = document.createElement("button");
    resetPromptBtn.textContent = "↺ Reset prompt to default";
    resetPromptBtn.style.cssText =
      "padding:4px 2px;border-radius:4px;border:1px solid #52525b;background:#18181b;color:#a1a1aa;cursor:pointer;font-size:11px;";
    promptWrap.appendChild(promptArea);
    promptWrap.appendChild(applyBtn);
    promptWrap.appendChild(resetPromptBtn);
    card.appendChild(promptWrap);

    // [2026-09-20] Klein is an edit model - this re-edits the pose's OWN
    // already-generated pixels with a short targeted instruction (e.g. "add
    // high heel shoes"), not a fresh regenerate from the original
    // character/pose references. Deliberately a single-line input, not a
    // full textarea, to stay compact - it's meant for short fixes.
    const editWrap = document.createElement("div");
    editWrap.style.cssText = "display:flex;flex-direction:column;gap:4px;margin-top:2px;";
    const editInput = document.createElement("input");
    editInput.type = "text";
    editInput.placeholder = "e.g. add high heel shoes";
    editInput.style.cssText =
      "width:100%;padding:4px;border-radius:4px;border:1px solid #3f3f46;background:#0b0b0d;" +
      "color:#d4d4d8;font-size:10px;font-family:inherit;box-sizing:border-box;";
    const editBtn = document.createElement("button");
    editBtn.textContent = "✏️ Apply edit";
    editBtn.style.cssText =
      "min-height:28px;padding:4px 2px;border-radius:4px;border:1px solid #a78bfa;background:#4c1d95;" +
      "color:#ede9fe;cursor:pointer;font-size:11px;";
    // [2026-09-17] Only shown once this pose actually has an active edit
    // (see refreshConfirmedVisual) - requested via a YouTube comment: New
    // seed re-rolls an active edit rather than reverting it (by design, see
    // reroll_edit), but there was no way back to the plain un-edited pose
    // short of retyping the base prompt into Apply Prompt.
    const resetEditBtn = document.createElement("button");
    resetEditBtn.textContent = "↺ Reset edit";
    resetEditBtn.style.cssText =
      "display:none;min-height:26px;padding:4px 2px;border-radius:4px;border:1px solid #71717a;background:#27272a;" +
      "color:#d4d4d8;cursor:pointer;font-size:11px;";
    editWrap.appendChild(editInput);
    editWrap.appendChild(editBtn);
    editWrap.appendChild(resetEditBtn);
    card.appendChild(editWrap);

    grid.appendChild(card);
    panels.push({
      card, img, placeholder, seedLabel, confirmBtn, rerollBtn,
      promptToggle, promptWrap, promptArea, applyBtn, resetPromptBtn,
      editInput, editBtn, resetEditBtn,
    });
  }

  // [2026-09-20] Applies one edit instruction to every UNCONFIRMED pose in a
  // single click, instead of retyping the same thing into all 5 boxes.
  // Confirmed/locked poses are silently skipped server-side - same lock
  // semantics as everything else here.
  const editAllWrap = document.createElement("div");
  editAllWrap.style.cssText =
    "display:flex;flex-direction:column;gap:4px;padding:8px;background:#111114;" +
    "border:1px solid #3f3f46;border-radius:6px;box-sizing:border-box;";
  const editAllLabel = document.createElement("div");
  editAllLabel.textContent = "Edit all poses";
  editAllLabel.style.cssText = "font-weight:600;font-size:11px;color:#d4d4d8;";
  const editAllInput = document.createElement("input");
  editAllInput.type = "text";
  editAllInput.placeholder = "e.g. make her barefoot in every pose";
  editAllInput.style.cssText =
    "width:100%;padding:5px;border-radius:4px;border:1px solid #3f3f46;background:#0b0b0d;" +
    "color:#d4d4d8;font-size:11px;font-family:inherit;box-sizing:border-box;";
  const editAllBtn = document.createElement("button");
  editAllBtn.textContent = "✏️ Apply edit to all";
  editAllBtn.style.cssText =
    "min-height:32px;padding:6px;border-radius:4px;border:1px solid #a78bfa;background:#4c1d95;" +
    "color:#ede9fe;cursor:pointer;font-size:12px;font-weight:600;";
  editAllWrap.appendChild(editAllLabel);
  editAllWrap.appendChild(editAllInput);
  editAllWrap.appendChild(editAllBtn);

  // [2026-09-23] Lives inside the same bordered edit-all box, directly under
  // "Apply edit to all", instead of floating as its own separate box below -
  // Andy asked for it grouped in with edit-all rather than looking like an
  // unrelated third section.
  const buildBtn = document.createElement("button");
  buildBtn.textContent = "Build final sheet now";
  buildBtn.style.cssText =
    "min-height:32px;padding:6px;border-radius:4px;border:1px solid #c2793f;background:#6b3a1a;color:#f4ddc4;cursor:pointer;font-size:12px;font-weight:600;";
  editAllWrap.appendChild(buildBtn);
  root.appendChild(editAllWrap);

  function queue() {
    app.queuePrompt(0, 1);
  }

  function measureHeight() {
    return Math.max(240, Math.ceil(root.scrollHeight + 8));
  }

  function applySize() {
    const width = Math.max(node.size[0], 820);
    const computed = node.computeSize?.() || [width, measureHeight()];
    node.setSize([width, Math.max(120, computed[1])]);
    node.setDirtyCanvas(true, true);
  }

  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => applySize()).observe(root);
  }

  function resize() {
    requestAnimationFrame(applySize);
  }

  function refreshConfirmedVisual(i, isConfirmed) {
    const p = panels[i];
    p.rerollBtn.disabled = isConfirmed;
    p.applyBtn.disabled = isConfirmed;
    p.promptArea.disabled = isConfirmed;
    p.editInput.disabled = isConfirmed;
    p.editBtn.disabled = isConfirmed;
    p.editBtn.style.opacity = isConfirmed ? "0.45" : "1";
    p.rerollBtn.style.opacity = isConfirmed ? "0.45" : "1";
    // [2026-09-20] Single source of truth for the reroll button's label -
    // read fresh from state every call rather than tracked separately, so it
    // can never drift out of sync with what a click would actually do.
    const hasActiveEdit = !!getState(stateWidget).editInstructions?.[i];
    p.rerollBtn.textContent = hasActiveEdit ? "\u{1F3B2} Re-roll edit" : "\u{1F3B2} New seed";
    p.rerollBtn.title = isConfirmed
      ? "Unconfirm this pose before changing it"
      : hasActiveEdit
      ? "Try the same edit again with a different seed"
      : "Generate a new seed";
    p.resetEditBtn.style.display = hasActiveEdit && !isConfirmed ? "block" : "none";
    p.resetEditBtn.disabled = isConfirmed;
    if (isConfirmed) {
      p.card.style.borderColor = "#22c55e";
      p.confirmBtn.textContent = "✓ Unconfirm";
      p.confirmBtn.style.background = "#14532d";
      p.confirmBtn.style.color = "#bbf7d0";
    } else {
      p.card.style.borderColor = "#2c2c33";
      p.confirmBtn.textContent = "Confirm";
      p.confirmBtn.style.background = "#166534";
      p.confirmBtn.style.color = "#ffffff";
    }
  }

  panels.forEach((p, i) => {
    p.promptArea.value = getState(stateWidget).prompts[i] || "";

    p.promptToggle.onclick = () => {
      const showing = p.promptWrap.style.display !== "none";
      p.promptWrap.style.display = showing ? "none" : "flex";
      p.promptToggle.textContent = showing ? "Show prompt ▾" : "Hide prompt ▴";
      resize();
    };

    p.applyBtn.onclick = () => {
      const state = getState(stateWidget);
      if (state.confirmed[i]) return;
      state.prompts[i] = p.promptArea.value;
      state.confirmed[i] = false;
      // [2026-09-20] Changing the BASE pose description is a deliberate
      // "start fresh" action - clears any active-edit tag so a later New
      // seed goes back to a normal base regeneration, not a reroll of an
      // edit instruction that no longer applies to the new description.
      state.editInstructions[i] = null;
      // [2026-09-17] Explicit action type, NOT null - a null action is read
      // server-side as "bare Queue Prompt", which under seed_mode=random
      // rerolls every unconfirmed pose's seed and regenerates all 5 (that's
      // the intended behavior for an actual bare run - see seed_mode's own
      // history). Apply Prompt is a targeted single-pose edit, not a bare
      // run, so it needs its own action type to stay out of that branch -
      // a YouTube comment caught this: editing just the portrait prompt was
      // regenerating all 5 poses.
      state.action = { type: "apply_prompt", pose: i };
      setState(stateWidget, state);
      refreshConfirmedVisual(i, false);
      statusEl.textContent = `Applying edited prompt to ${POSE_LABELS[i]}…`;
      queue();
    };

    p.resetPromptBtn.onclick = () => {
      const state = getState(stateWidget);
      if (state.confirmed[i]) return;
      state.confirmed[i] = false;
      state.editInstructions[i] = null;
      state.action = { type: "reset_prompt", pose: i };
      setState(stateWidget, state);
      refreshConfirmedVisual(i, false);
      statusEl.textContent = `Resetting ${POSE_LABELS[i]} to its default prompt…`;
      queue();
    };

    p.confirmBtn.onclick = () => {
      // Confirm is a LOCAL toggle only - it must never submit a prompt, not
      // even on the 5th/last pose. See the Krea2 node's JS for the full
      // rationale (two earlier attempts at auto-queueing on confirm were
      // both explicitly rejected).
      const state = getState(stateWidget);
      if (!state.confirmed[i] && !p.img.src) return;
      const nowConfirmed = !state.confirmed[i];
      state.confirmed[i] = nowConfirmed;
      state.action = null;
      setState(stateWidget, state);
      refreshConfirmedVisual(i, nowConfirmed);
      statusEl.textContent = state.confirmed.every(Boolean)
        ? "All poses confirmed — click “Build final sheet now” when ready."
        : describeStatus(getState(stateWidget).status);
    };
    p.rerollBtn.onclick = () => {
      // [2026-09-20] If this pose currently has an active edit, "New seed"
      // re-rolls THAT edit (same instruction, new seed, same source pixels
      // it was originally applied to) instead of reverting to the un-edited
      // base pose - see reroll_edit() in the Python for the full mechanism.
      // Andy's own words: changing an outfit to green and disliking the
      // exact shade shouldn't mean losing the green entirely.
      const state = getState(stateWidget);
      if (state.confirmed[i]) return;
      const hasActiveEdit = !!state.editInstructions[i];
      const newSeed = Math.floor(Math.random() * 2147483647);
      state.seeds[i] = newSeed;
      state.confirmed[i] = false;
      state.action = hasActiveEdit
        ? { type: "reroll_edit", pose: i, seed: newSeed }
        : { type: "reroll", pose: i, seed: newSeed };
      setState(stateWidget, state);
      p.seedLabel.textContent = `seed ${newSeed}`;
      refreshConfirmedVisual(i, false);
      statusEl.textContent = hasActiveEdit
        ? `Re-rolling edit on ${POSE_LABELS[i]}…`
        : `Re-rolling ${POSE_LABELS[i]}…`;
      queue();
    };

    p.editBtn.onclick = () => {
      // [2026-09-20] Re-edits this pose's OWN current pixels with a short
      // instruction - not a fresh regenerate. Doesn't touch state.seeds[i]
      // (the edit pass reuses whatever seed that pose already has; see
      // run()'s "edit" action handling in the Python).
      const state = getState(stateWidget);
      if (state.confirmed[i]) return;
      const instruction = p.editInput.value.trim();
      if (!instruction || !p.img.src) return;
      state.editInstructions[i] = instruction;
      state.action = { type: "edit", pose: i, instruction };
      setState(stateWidget, state);
      refreshConfirmedVisual(i, false);
      statusEl.textContent = `Editing ${POSE_LABELS[i]}…`;
      queue();
    };

    p.resetEditBtn.onclick = () => {
      const state = getState(stateWidget);
      if (state.confirmed[i] || !state.editInstructions[i]) return;
      state.editInstructions[i] = null;
      state.action = { type: "reset_edit", pose: i };
      setState(stateWidget, state);
      refreshConfirmedVisual(i, false);
      statusEl.textContent = `Reverting ${POSE_LABELS[i]} to the original…`;
      queue();
    };
  });

  editAllBtn.onclick = () => {
    const instruction = editAllInput.value.trim();
    if (!instruction) return;
    const state = getState(stateWidget);
    // Optimistic tag on every unlocked pose - Python silently skips any that
    // genuinely have nothing to edit (see apply_edit()), so tagging one that
    // didn't actually change is harmless (New seed would just find nothing
    // to reroll server-side and fall back to a normal base reroll).
    for (let i = 0; i < 5; i++) {
      if (!state.confirmed[i]) state.editInstructions[i] = instruction;
    }
    state.action = { type: "edit_all", instruction };
    setState(stateWidget, state);
    for (let i = 0; i < 5; i++) refreshConfirmedVisual(i, state.confirmed[i]);
    statusEl.textContent = "Editing all unlocked poses…";
    queue();
  };

  buildBtn.onclick = () => {
    const state = getState(stateWidget);
    state.action = { type: "finalize" };
    setState(stateWidget, state);
    statusEl.textContent = "Building final sheet…";
    queue();
  };

  function renderState({ seeds, confirmed, prompts, previews, status }) {
    seeds = seeds || DEFAULT_SEEDS;
    confirmed = confirmed || [false, false, false, false, false];
    prompts = prompts || [];
    previews = previews || [];

    previews.forEach((prev, i) => {
      if (!prev || !panels[i]) return;
      const p = panels[i];
      const url = api.apiURL(
        `/view?filename=${encodeURIComponent(prev.filename)}&subfolder=${encodeURIComponent(prev.subfolder || "")}&type=${prev.type || "temp"}&rand=${Date.now()}`
      );
      p.img.src = url;
      p.img.style.display = "block";
      p.placeholder.style.display = "none";
      p.seedLabel.textContent = `seed ${seeds[i]}`;
      refreshConfirmedVisual(i, !!confirmed[i]);
      if (prompts[i] !== undefined && document.activeElement !== p.promptArea) {
        p.promptArea.value = prompts[i];
      }
    });

    statusEl.textContent = describeStatus(status);
  }

  function minimalImageRef(prev) {
    return prev ? { filename: prev.filename, subfolder: prev.subfolder || "", type: prev.type || "temp" } : null;
  }

  function update(message) {
    if (!message) return;
    const seeds = message.seeds || DEFAULT_SEEDS;
    const isReset = !!(Array.isArray(message.reset) ? message.reset[0] : message.reset);
    const confirmed = isReset ? [false, false, false, false, false] : getState(stateWidget).confirmed;
    const prompts = message.prompts || [];
    const previews = message.pose_previews || [];
    const status = Array.isArray(message.status) ? message.status[0] : message.status;

    renderState({ seeds, confirmed, prompts, previews, status });

    const st = getState(stateWidget);
    st.seeds = seeds;
    st.confirmed = confirmed;
    if (prompts.length === 5) st.prompts = prompts;
    st.previews = previews.map(minimalImageRef);
    // A full sheet finishing wipes the server-side session too (sess.clear()
    // in the Python), so edit_source_image no longer exists there either -
    // clear the client-side tags to match, otherwise the reroll button would
    // keep saying "Re-roll edit" for a pose that has nothing left to reroll.
    if (isReset) st.editInstructions = [null, null, null, null, null];
    st.status = status || null;
    st.action = null;
    setState(stateWidget, st);

    resize();
  }

  function restore() {
    renderState(getState(stateWidget));
    faceDetailBox.sync();
    resize();
  }

  restore();

  return { root, update, restore, resize, measureHeight };
}

app.registerExtension({
  name: "Man4Tech.CharacterSheetKlein",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "Man4TechCharacterSheetKlein") return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      const stateWidget = (this.widgets || []).find((w) => w.name === "state_json");
      if (!stateWidget) return result;
      if (!stateWidget.value) stateWidget.value = JSON.stringify(defaultState());
      hideWidget(stateWidget);

      const findWidget = (name) => (this.widgets || []).find((w) => w.name === name);
      const faceWidgets = {
        faceDetail: findWidget("face_detail"),
        faceDetailType: findWidget("face_detail_type"),
        faceDetailSampler: findWidget("face_detail_sampler"),
        faceDetailScheduler: findWidget("face_detail_scheduler"),
        faceDetailDenoise: findWidget("face_detail_denoise"),
      };
      Object.values(faceWidgets).forEach(hideWidget);

      const ui = buildCharacterSheetUI(this, stateWidget, faceWidgets);
      enableCanvasZoomOverDOM(ui.root);
      const domWidget = this.addDOMWidget("character_sheet_klein_ui", "character_sheet_klein_ui", ui.root, {
        serialize: false,
        hideOnZoom: false,
      });
      domWidget.computeSize = (width) => [width, ui.measureHeight()];
      this._man4techCharacterSheetKleinUI = ui;

      setTimeout(() => applyOverrideState(this), 0);
      return result;
    };

    const onConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function (...args) {
      const result = onConnectionsChange?.apply(this, args);
      applyOverrideState(this);
      return result;
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      const result = onExecuted?.apply(this, arguments);
      this._man4techCharacterSheetKleinUI?.update(message);
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      const stateWidget = (this.widgets || []).find((w) => w.name === "state_json");
      if (stateWidget) {
        const state = getState(stateWidget);
        state.action = null;
        setState(stateWidget, state);
      }
      this._man4techCharacterSheetKleinUI?.restore();
      setTimeout(() => applyOverrideState(this), 0);
      return result;
    };
  },
});
