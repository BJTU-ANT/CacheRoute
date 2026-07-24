function $(id) { return document.getElementById(id); }

function safeJsonParse(text) {
  try { return { ok: true, val: JSON.parse(text) }; }
  catch (e) { return { ok: false, err: e.toString() }; }
}

function pretty(obj) {
  try { return JSON.stringify(obj, null, 2); }
  catch { return String(obj); }
}

function showValidation(ok, msg) {
  const el = $("validation");
  el.classList.remove("hidden");
  el.className = ok
    ? "mt-2 text-sm px-3 py-2 rounded-xl border border-emerald-800 bg-emerald-950/40 text-emerald-200"
    : "mt-2 text-sm px-3 py-2 rounded-xl border border-rose-800 bg-rose-950/40 text-rose-200";
  el.textContent = msg;
}

function setResponse(meta, headersObj, bodyText) {
  $("respMeta").textContent = meta;
  $("respHeaders").textContent = headersObj ? pretty(headersObj) : "";
  $("respBody").textContent = bodyText || "";
}

function loadExample() {
  const mode = $("mode").value;
  if (mode === "chat") {
    $("body").value = pretty({
      model: "deepseek-qwen-1_5b",
      messages: [
        { role: "system", content: "You are a helpful assistant." },
        { role: "user", content: "Who are you?" }
      ],
      temperature: 0.7,
      top_p: 0.9,
      max_tokens: 128,
      stream: false
    });
  } else {
    $("body").value = pretty({
      model: "deepseek-qwen-1_5b",
      prompt: "Write a short hello message.",
      temperature: 0.7,
      top_p: 0.9,
      max_tokens: 128,
      stream: false
    });
  }
}

async function apiPost(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const text = await r.text();
  let obj = null;
  try { obj = JSON.parse(text); } catch { obj = { raw: text }; }
  return { ok: r.ok, status: r.status, data: obj };
}

$("btn-load-example").addEventListener("click", (e) => {
  e.preventDefault();
  loadExample();
});

$("mode").addEventListener("change", () => {
  loadExample();
});

// Load the initial example.
loadExample();

$("btn-parse-curl").addEventListener("click", async () => {
  const line = $("curlLine").value.trim();
  if (!line) { $("curlParseMsg").textContent = "Please enter a curl-like command"; return; }

  const res = await apiPost("/ui/api/parse_curl", { line });
  if (!res.ok) {
    $("curlParseMsg").textContent = "Parse failed: " + (res.data.error || "unknown");
    return;
  }
  const p = res.data.parsed;
  $("url").value = p.url || "";
  $("headers").value = pretty(p.headers || {});
  $("body").value = pretty(p.body || {});
  $("curlParseMsg").textContent = "Parsed successfully: populated the form";
});

$("btn-validate").addEventListener("click", async () => {
  const url = $("url").value.trim();
  const hdrText = $("headers").value.trim();
  const bodyText = $("body").value.trim();

  const hdr = safeJsonParse(hdrText);
  if (!hdr.ok) { showValidation(false, "Headers JSON parse failed: " + hdr.err); return; }
  const body = safeJsonParse(bodyText);
  if (!body.ok) { showValidation(false, "Body JSON parse failed: " + body.err); return; }

  const res = await apiPost("/ui/api/validate", { url, headers: hdr.val, body: body.val });
  if (!res.ok) {
    showValidation(false, "Validation API error: " + pretty(res.data));
    return;
  }
  if (res.data.ok) showValidation(true, "Validation passed ✅");
  else showValidation(false, "Validation failed:\n" + (res.data.errors || []).join("\n"));
});

$("btn-send").addEventListener("click", async () => {
  const url = $("url").value.trim();
  const timeout = Number($("timeout").value || "60");

  const hdrText = $("headers").value.trim();
  const bodyText = $("body").value.trim();

  const hdr = safeJsonParse(hdrText);
  if (!hdr.ok) { showValidation(false, "Headers JSON parse failed: " + hdr.err); return; }
  const body = safeJsonParse(bodyText);
  if (!body.ok) { showValidation(false, "Body JSON parse failed: " + body.err); return; }

  showValidation(true, "Sending...");

  const res = await apiPost("/ui/api/send", {
    url,
    timeout,
    headers: hdr.val,
    body: body.val
  });

  if (!res.ok) {
    if (res.data && res.data.error === "validation_failed") {
      showValidation(false, "Pre-send validation failed:\n" + (res.data.errors || []).join("\n"));
    } else {
      showValidation(false, "Send failed: " + pretty(res.data));
    }
    return;
  }

  const status = res.data.status_code;
  const headersObj = res.data.headers;
  const bodyJson = res.data.body_json;
  const bodyTextResp = bodyJson ? pretty(bodyJson) : (res.data.body_text || "");

  setResponse(`HTTP ${status}`, headersObj, bodyTextResp);
  showValidation(true, "Request completed ✅");
});
