"use strict";

const app = document.querySelector("#app");
const logout = document.querySelector("#logout");
const number = new Intl.NumberFormat("fa-IR");
const wholeNumber = new Intl.NumberFormat("fa-IR", {maximumFractionDigits: 0});
const decimal = new Intl.NumberFormat("fa-IR", {maximumFractionDigits: 2});
const uploadSpeed = {format: value => value > 0 && value < 1 ? "<" + wholeNumber.format(1) : wholeNumber.format(value)};
const date = new Intl.DateTimeFormat("fa-IR", {
  dateStyle: "medium", timeStyle: "long"
});
const escape = value => String(value ?? "").replace(/[&<>"']/g, char =>
  ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);

const errors = {
  relay_invalid_server: "آی‌پی عمومی معتبر سرور خارج را وارد کن.",
  relay_ssh_unreachable: "دستگاه ایران نتوانست به SSH سرور خارج برسد.",
  relay_fingerprint_mismatch: "کلید SSH سرور از زمان تأیید تغییر کرده است. دوباره اتصال را بررسی کن.",
  relay_ssh_credentials: "رمز SSH یا فایل کلید خصوصی را وارد کن. فقط یکی لازم است.",
  relay_invalid_token: "فرمت توکن ربات تلگرام معتبر نیست.",
  relay_ssh_tools_unavailable: "ابزارهای SSH روی دستگاه ایران در دسترس نیستند.",
  relay_setup_failed: "نصب ربات انجام نشد. دسترسی SSH مدیر، Python، systemd و چت خصوصی تلگرام را بررسی کن.",
  relay_remote_permissions: "حساب سرور خارج باید root باشد یا بدون رمز به sudo دسترسی داشته باشد.",
  relay_remote_directory_conflict: "یکی از مسیرهای نصب رله روی سرور خارج با فایل یا پوشهٔ موجود تداخل دارد.",
  relay_remote_health: "سرویس ربات پاسخ نداد. آزاد بودن پورت API محلی را روی سرور خارج بررسی کن.",
  relay_telegram_unavailable: "سرور خارج با تلگرام ارتباط نگرفت. دسترسی HTTPS و توکن ربات را بررسی کن.",
  relay_telegram_webhook: "این ربات از قبل webhook دارد. برای رله از ربات جداگانه استفاده کن.",
  relay_telegram_chat: "چت خصوصی پیدا نشد. به ربات /start بفرست و شناسهٔ عددی چت را بررسی کن.",
  relay_transport_unavailable: "سرویس نصب شد، اما اتصال با حساب محدود رله برقرار نشد. دسترسی SSH حساب mobocloud-relay را بررسی کن.",
  relay_credentials_invalid: "کلید ارتباط با سرور خارج خوانده نشد. رله را دوباره متصل کن.",
  relay_unavailable: "ارتباط با رلهٔ تلگرام برقرار نشد.",
  relay_response_invalid: "پاسخ رلهٔ تلگرام معتبر نیست.",
  relay_setup_in_progress: "راه‌اندازی رله از قبل در جریان است.",
  invalid_length: "بدنهٔ درخواست معتبر نیست.",
  invalid_input: "اطلاعات واردشده را بررسی کنید.",
  invalid_url_or_inactive_interface: "لینک HTTPS و اتصال شبکه را بررسی کنید.",
  interface_unavailable: "رابط شبکه متصل نیست.",
  subscription_speed_policy_disabled: "سرویس تست اشتراک فعال نیست.",
  subscription_ping_disabled: "برای تست پینگ، تست زنده را در تنظیمات فعال کن.",
  feed_refresh_failed: "دریافت اشتراک ناموفق بود.",
  connection_timeout: "مهلت اتصال تمام شد.",
  timeout: "مهلت این اندازه‌گیری تمام شد.",
  budget_exhausted: "این تست در نسخهٔ قبلی به‌خاطر سهمیه اجرا نشد.",
  subscription_upload_unavailable: "نشانی تست آپلود روی سرور تنظیم نشده است.",
  subscription_delete_unavailable: "نسخهٔ در حال اجرای سرور از حذف اشتراک پشتیبانی نمی‌کند.",
  job_pending: "پیکربندی در صف تست است.",
  config_disabled_or_missing: "پیکربندی غیرفعال است.",
  reservation_expired: "مهلت سهمیهٔ تست گذشته است.",
  missing: "اشتراک پیدا نشد.",
  cancelled: "لغو شد",
  interrupted: "ناتمام ماند",
  runner_error: "خطا در اجرای تست",
  history_changed: "تعداد نتایج تغییر کرد. تعداد جدید را بررسی و دوباره تأیید کنید.",
  retry_later: "تعداد تلاش‌ها زیاد است. کمی بعد دوباره امتحان کنید.",
  unavailable: "ارتباط با پنل برقرار نشد."
};

let feeds = [], latest = [], older = [], cursor = null;
let pollTimer, version = 0, editing = false, signedIn = false;

async function api(path, method = "GET", data) {
  let response;
  try {
    response = await fetch("/api/v1/" + path, {
      method,
      headers: {"X-Lab-Request": "1", "Content-Type": "application/json"},
      body: data === undefined ? undefined : JSON.stringify(data)
    });
  } catch {
    throw new Error("unavailable");
  }
  if (response.status === 405 && method === "DELETE" && /^subscriptions\/\d+$/.test(path)) {
    throw new Error("subscription_delete_unavailable");
  }
  if (response.status === 401 && path !== "login") showLogin();
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || "unavailable");
  return value;
}

function notice(message, isError = false, node = document.querySelector("#notice")) {
  if (node) {
    node.textContent = message;
    node.classList.toggle("error", isError);
  }
}

function errorText(code) {
  return String(code).split(",").map(value => errors[value] || value).join("، ");
}

function time(seconds) {
  if (!seconds) return "—";
  const value = new Date(seconds * 1000);
  return `<time datetime="${value.toISOString()}">${date.format(value)}</time>`;
}

function showLogin() {
  if (!signedIn && app.querySelector(".login")) return;
  signedIn = false;
  version++;
  clearTimeout(pollTimer);
  logout.hidden = true;
  app.replaceChildren(document.querySelector("#signin").content.cloneNode(true));
  app.removeAttribute("aria-busy");
  const form = app.querySelector("form");
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      await api("login", "POST", {password: form.elements.password.value});
      await openDashboard();
    } catch (error) {
      const message = ["invalid_credentials", "invalid_login", "login_failed"].includes(error.message)
        ? "رمز عبور صحیح نیست." : errorText(error.message);
      notice(message, true, form.querySelector(".notice"));
    } finally {
      button.disabled = false;
    }
  };
}

function paint(node, html) {
  if (node.rendered !== html) {
    node.innerHTML = html;
    node.rendered = html;
  }
}

function active(result) {
  return ["queued", "running", "cancelling"].includes(result.state);
}

function drawFeeds(data) {
  feeds = data.feeds;
  const form = document.querySelector("#feed-form");
  const select = form.elements.iface;
  const selected = select.value;
  const options = data.interfaces.filter(link => link.up).map(link =>
    `<option value="${escape(link.name)}">${link.kind === "Wi-Fi" ? "Wi-Fi" : "LAN"} · ${escape(link.name)}</option>`
  ).join("");
  paint(select, options || '<option value="">اتصال فعالی پیدا نشد</option>');
  if (selected && [...select.options].some(option => option.value === selected)) select.value = selected;
  form.querySelector("button").disabled = !data.ping_ready || !select.value;
  const policyNotice = document.querySelector("#policy-notice");
  policyNotice.hidden = data.ready;
  policyNotice.classList.toggle("error", !data.ping_ready);
  policyNotice.textContent = data.ping_ready ? "فقط تست پینگ فعال است." : errorText("subscription_ping_disabled");

  paint(document.querySelector("#feeds"), feeds.map(feed => {
    const batch = feed.progress;
    const scheduled = feed.enabled || feed.ping_hours > 0;
    const status = feed.busy ? "در حال تست" : scheduled ? "فعال" : "متوقف";
    const periods = [...new Set([12, 24, feed.hours])].sort((a, b) => a - b);
    const progress = feed.busy ? (batch?.total
      ? `<div class="progress">${number.format(batch.done)} از ${number.format(batch.total)} کانفیگ تست شد
           <progress max="${batch.total}" value="${batch.done}" aria-label="پیشرفت تست"></progress></div>`
      : '<p class="progress">در صف دریافت اشتراک</p>') : "";
    return `<article class="panel feed-card">
      <div class="feed-head"><h3>${escape(feed.name)}</h3><span class="badge ${feed.busy ? "running" : scheduled ? "success" : "muted"}">${status}</span></div>
      <dl><dt>اتصال</dt><dd><bdi>${escape(feed.iface)}</bdi></dd><dt>تست سرعت بعدی</dt><dd>${feed.enabled ? (feed.due ? time(feed.due) : "در انتظار اجرا") : "—"}</dd>
      ${feed.ping_hours > 0 ? `<dt>پینگ بعدی</dt><dd>${feed.ping_due ? time(feed.ping_due) : "در انتظار اجرا"}</dd>` : ""}</dl>
      ${progress}
      <div class="actions" data-feed="${feed.id}">
        <button class="primary" data-action="run" ${feed.busy || !data.ready ? "disabled" : ""}>تست فوری Cloudflare</button>
        <button data-action="ping" ${feed.busy || !data.ping_ready ? "disabled" : ""}>فقط پینگ</button>
        <button data-action="toggle" data-enabled="${feed.enabled ? 0 : 1}" ${!feed.enabled && !data.ready ? "disabled" : ""}>${feed.enabled ? "توقف تست سرعت" : "ادامهٔ تست سرعت"}</button>
        <button data-action="delete" class="danger">حذف اشتراک</button>
        <select data-action="period" aria-label="دورهٔ اجرای ${escape(feed.name)}">
          ${periods.map(hours => `<option value="${hours}" ${feed.hours === hours ? "selected" : ""}>هر ${number.format(hours)} ساعت</option>`).join("")}
        </select>
      </div>
    </article>`;
  }).join("") || '<p class="muted">هنوز اشتراکی ذخیره نشده است.</p>');
}

function resultRow(result, pingOnly) {
  const pending = active(result);
  const status = pending
    ? ({queued:"در صف", running:"در حال تست", cancelling:"در حال توقف"})[result.state]
    : result.ok ? "موفق"
      : errorText(result.error || (result.state === "done" ? "نتیجه در دسترس نیست" : result.state));
  const metric = (label, value, unit, error, samples, total, format = decimal) => `<div class="metric">
    <span class="muted">${label}</span>
    <span class="speed">${value == null ? "—" : `<bdi dir="ltr"><strong>${escape(format.format(value))}</strong> <small>${unit}</small></bdi>`}</span>
    ${total == null ? "" : `<span class="muted metric-samples">${number.format(samples)} از ${number.format(total)} موفق</span>`}
    ${error ? `<span class="error metric-error">${escape(errorText(error))}</span>` : ""}
  </div>`;
  const tunnelResponded = result.state === "done" &&
    (result.ping_ms != null || result.ok || result.upload_ok);
  return `<div class="result">
    <div class="result-name">
      <bdi>${escape(result.label)}</bdi>
      ${tunnelResponded ? '<span class="badge success">پراکسی پاسخ داد</span>' : ""}
    </div>
    ${time(result.at)}
    <div class="metrics ${pingOnly ? "ping-only" : ""}">
      ${metric(result.ping_ms != null && !result.ping_method ? "زمان پاسخ کامل" : "پینگ", result.ping_ms, "ms",
        result.ping_error, result.ping_sample_ok, result.ping_sample_total, wholeNumber)}
      ${pingOnly ? "" : `${metric("دانلود", result.mbps, "Mbps", result.state === "done" ? result.error : null,
        result.download_sample_ok, result.download_sample_total)}
      ${metric("آپلود", result.upload_mbps, "Mbps", result.upload_error,
        result.upload_sample_ok, result.upload_sample_total, uploadSpeed)}`}
    </div>
    ${pending || (!result.ok && result.state !== "done") ?
      `<span class="result-status ${pending ? "muted" : "error"}">${escape(status)}</span>` : ""}
  </div>`;
}

function drawRounds() {
  const node = document.querySelector("#rounds");
  const open = new Map([...node.querySelectorAll("details")].map(item => [Number(item.dataset.round), item.open]));
  const items = [...new Map([...older, ...latest].map(batch => [batch.id, batch])).values()]
    .sort((a, b) => b.id - a.id);
  if (node.newest !== items[0]?.id) open.delete(node.newest);
  node.newest = items[0]?.id;
  paint(node, items.map((batch, index) => {
    const messages = [];
    if (batch.state !== "ready") messages.push("در صف دریافت اشتراک");
    if (batch.rejected) messages.push(`${number.format(batch.rejected)} ورودی رد شد`);
    if (batch.skipped) messages.push(`${number.format(batch.skipped)} کانفیگ وارد صف نشد`);
    if (batch.error) messages.push(errorText(batch.error));
    const expanded = open.get(batch.id) ?? index === 0;
    const kind = batch.ping_only ? (batch.kind === "scheduled" ? "پینگ دوره‌ای" : "فقط پینگ")
      : ({manual:"اجرای فوری", scheduled:"دوره‌ای", legacy:"دور قبلی"})[batch.kind];
    return `<details data-round="${batch.id}" ${expanded ? "open" : ""}>
      <summary><bdi>${escape(batch.name)}</bdi> · ${kind} ${time(batch.created)}</summary>
      <div class="round-inner">
        ${messages.length ? `<p class="${batch.error ? "error" : "muted"}">${escape(messages.join(" · "))}</p>` : ""}
        ${batch.results.map(result => resultRow(result, batch.ping_only)).join("") || (!messages.length ? '<p class="muted">نتیجه‌ای ثبت نشده است.</p>' : "")}
      </div>
    </details>`;
  }).join("") || '<p class="muted">هنوز دور تستی ثبت نشده است.</p>');
  document.querySelector("#older").hidden = cursor == null;
  document.querySelector("#clear-history").disabled = feeds.length === 0 && items.length === 0;
}

async function refresh() {
  const token = ++version;
  const [data, history] = await Promise.all([api("subscriptions?summary=true"), api("subscriptions/rounds")]);
  if (!signedIn || token !== version) return;
  drawFeeds(data);
  // Keep rounds that move past the first page while the operator is viewing older pages.
  if (older.length && history.rounds.length) {
    const firstPageEnd = history.rounds.at(-1).id;
    older = [...new Map([...older, ...latest.filter(batch => batch.id < firstPageEnd)]
      .map(batch => [batch.id, batch])).values()];
  } else {
    cursor = history.before;
  }
  latest = history.rounds;
  drawRounds();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (!signedIn) return;
  pollTimer = setTimeout(async () => {
    try {
      if (!editing && !document.hidden) await refresh();
    } catch (error) {
      if (signedIn) notice(errorText(error.message), true);
    } finally {
      schedulePoll();
    }
  }, 3000);
}

async function act(work) {
  if (editing) return;
  editing = true;
  version++;
  notice("");
  try {
    await work();
    if (signedIn) await refresh();
  } catch (error) {
    if (signedIn) notice(errorText(error.message), true);
  } finally {
    editing = false;
  }
}

function bindDashboard() {
  const form = document.querySelector("#feed-form");
  form.onsubmit = event => {
    event.preventDefault();
    act(async () => {
      const data = new FormData(form);
      const saved = await api("subscriptions", "POST", {
        url: data.get("url").trim(), name: data.get("name"),
        iface: data.get("iface"), hours: Number(data.get("hours"))
      });
      form.elements.url.value = "";
      form.elements.name.value = "";
      notice("اشتراک ذخیره شد. اجرای بعدی " + date.format(saved.due * 1000));
    });
  };
  const cards = document.querySelector("#feeds");
  const deletion = document.querySelector("#delete-feed-dialog");
  const deleteForm = document.querySelector("#delete-feed-form");
  const deleteButton = deleteForm.querySelector("[type=submit]");
  const dismiss = deleteForm.querySelector("[data-dismiss]");
  const deleteError = deleteForm.querySelector("[role=alert]");
  dismiss.onclick = () => deletion.close();
  deletion.oncancel = event => {
    if (deleteButton.disabled) event.preventDefault();
  };
  deleteForm.onsubmit = event => {
    event.preventDefault();
    if (editing) return;
    act(async () => {
      deleteError.textContent = "";
      deleteButton.disabled = dismiss.disabled = true;
      try {
        await api("subscriptions/" + deleteForm.dataset.feed, "DELETE", {});
        deletion.close();
        notice("اشتراک حذف شد.");
      } catch (error) {
        deleteError.textContent = errorText(error.message);
      } finally {
        deleteButton.disabled = dismiss.disabled = false;
      }
    });
  };
  cards.onclick = event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.closest("[data-feed]").dataset.feed;
    const feed = feeds.find(item => item.id === Number(id));
    const path = "subscriptions/" + id;
    if (editing || !feed) return;
    if (button.dataset.action === "delete") {
      deleteForm.dataset.feed = id;
      deleteForm.querySelector("[data-description]").textContent =
        `اشتراک «${feed.name}» حذف شود؟ تاریخچهٔ تست‌ها باقی می‌ماند.`;
      deleteError.textContent = "";
      deletion.showModal();
      return;
    }
    act(async () => {
      if (["run", "ping"].includes(button.dataset.action)) {
        const ping = button.dataset.action === "ping";
        const round = await api(path + (ping ? "/ping" : "/run?cloudflare_upload=true"), "POST", {});
        if (round.queued) notice(ping ? "تست پینگ در صف قرار گرفت." : "یک دور تست در صف قرار گرفت.");
        else notice("یک دور تست در صف یا در حال اجراست.");
      } else {
        await api(path + "?enabled=" + (button.dataset.enabled === "1"), "PATCH", {});
      }
    });
  };
  cards.onchange = event => {
    const select = event.target;
    if (select.dataset.action !== "period") return;
    const id = select.closest("[data-feed]").dataset.feed;
    act(() => api("subscriptions/" + id + "/period", "PUT", {hours: Number(select.value)}));
  };
  document.querySelector("#older").onclick = () => act(async () => {
    const page = await api("subscriptions/rounds?before=" + cursor);
    older.push(...page.rounds);
    cursor = page.before;
    drawRounds();
  });
  bindHistory();
  bindTelegram();
}

function bindTelegram() {
  const panel = document.querySelector("#telegram-panel");
  const form = document.querySelector("#telegram-form");
  const status = document.querySelector("#telegram-status");
  const hostDialog = document.querySelector("#telegram-host-dialog");
  const button = form.querySelector("[type=submit]");
  const show = (message, error = false) => {
    status.textContent = message;
    status.classList.toggle("error", error);
  };
  function showStatus(data) {
    if (data.error) show(errorText(data.error), true);
    else if (data.last_ok) show("آخرین ارتباط موفق: " + date.format(data.last_ok * 1000));
    else show("ربات هنوز متصل نشده است.");
  }
  function confirmHost(host, server, port) {
    hostDialog.querySelector("[data-host-message]").textContent = host.changed
      ? `کلید ذخیره‌شده برای ${server} روی پورت ${port} تغییر کرده است.`
      : `اولین اتصال به ${server} روی پورت ${port}`;
    hostDialog.querySelector("[data-host-key]").textContent = `${host.algorithm} ${host.fingerprint}`;
    hostDialog.returnValue = "";
    return new Promise(resolve => {
      hostDialog.addEventListener("close", () => resolve(hostDialog.returnValue === "confirm"), {once: true});
      hostDialog.showModal();
    });
  }
  panel.ontoggle = async () => {
    if (!panel.open || button.disabled) return;
    try {
      showStatus(await api("telegram"));
    } catch (error) {
      show(errorText(error.message), true);
    }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    show("در حال بررسی اتصال SSH…");
    try {
      const data = new FormData(form);
      const file = form.elements.private_key.files[0];
      const password = data.get("password");
      if (Boolean(file) === Boolean(password) || (file && file.size > 32768)) {
        throw new Error("relay_ssh_credentials");
      }
      const target = {server: data.get("server").trim(), ssh_port: Number(data.get("ssh_port"))};
      const host = await api("telegram/host-key", "POST", target);
      if (!host.trusted && !await confirmHost(host, target.server, target.ssh_port)) {
        show("نصب لغو شد.");
        return;
      }
      show("در حال اتصال به سرور خارج و نصب ربات…");
      await api("telegram/setup", "POST", {
        ...target, username: data.get("username").trim(), api_port: Number(data.get("api_port")),
        fingerprint: host.fingerprint, password,
        private_key: file ? await file.text() : "", bot_token: data.get("bot_token").trim(),
        chat_id: Number(data.get("chat_id"))
      });
      show("ربات متصل است. در تلگرام دستور /test را بفرست.");
    } catch (error) {
      show(errorText(error.message), true);
    } finally {
      form.elements.password.value = "";
      form.elements.private_key.value = "";
      form.elements.bot_token.value = "";
      button.disabled = false;
    }
  };
}

function bindHistory() {
  const dialog = document.querySelector("#history-dialog");
  const form = document.querySelector("#history-form");
  const select = form.elements.feed;
  const preview = document.querySelector("#clear-preview");
  const confirm = form.querySelector("[type=submit]");
  let request = 0, expected;
  const scope = () => select.value ? "?fid=" + select.value : "";

  async function prepare(message = "") {
    const token = ++request;
    confirm.disabled = true;
    select.disabled = false;
    const label = select.selectedOptions[0].text;
    try {
      const {count} = await api("subscriptions/history" + scope());
      if (token !== request || !dialog.open) return;
      expected = count;
      preview.textContent = [message, `«${label}» · ${number.format(count)} نتیجه پاک می‌شود.`].filter(Boolean).join(" ");
      confirm.disabled = false;
    } catch (error) {
      if (token === request) preview.textContent = errorText(error.message);
    }
  }

  document.querySelector("#clear-history").onclick = () => {
    select.replaceChildren(new Option("همهٔ اشتراک‌ها", ""),
      ...feeds.map(feed => new Option(feed.name, feed.id)));
    preview.textContent = "در حال شمارش نتایج…";
    dialog.showModal();
    prepare();
  };
  select.onchange = () => prepare();
  form.querySelector("[data-dismiss]").onclick = () => dialog.close();
  dialog.onclose = () => { request++; };
  form.onsubmit = async event => {
    event.preventDefault();
    if (confirm.disabled) return;
    confirm.disabled = true;
    select.disabled = true;
    try {
      const target = scope();
      await api("subscriptions/history" + target + (target ? "&" : "?") + "count=" + expected, "DELETE", {});
      dialog.close();
      older = [];
      latest = [];
      cursor = null;
      await refresh();
      notice("تاریخچه پاک شد.");
    } catch (error) {
      if (dialog.open) await prepare(errorText(error.message));
    }
  };
}

async function openDashboard() {
  signedIn = true;
  older = [];
  latest = [];
  cursor = null;
  app.replaceChildren(document.querySelector("#dashboard").content.cloneNode(true));
  app.removeAttribute("aria-busy");
  logout.hidden = false;
  bindDashboard();
  try {
    await refresh();
  } catch (error) {
    if (signedIn) notice(errorText(error.message), true);
  }
  schedulePoll();
}

logout.onclick = () => act(async () => {
  await api("logout", "POST", {});
  showLogin();
});
openDashboard();
