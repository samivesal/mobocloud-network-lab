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
  invalid_length: "بدنهٔ درخواست معتبر نیست.",
  invalid_input: "اطلاعات واردشده را بررسی کنید.",
  invalid_url_or_inactive_interface: "لینک HTTPS و اتصال شبکه را بررسی کنید.",
  interface_unavailable: "رابط شبکه متصل نیست.",
  subscription_speed_policy_disabled: "سرویس تست اشتراک فعال نیست.",
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
  form.querySelector("button").disabled = !data.ready || !select.value;
  document.querySelector("#policy-notice").hidden = data.ready;

  paint(document.querySelector("#feeds"), feeds.map(feed => {
    const batch = feed.progress;
    const status = feed.busy ? "در حال تست" : feed.enabled ? "فعال" : "متوقف";
    const progress = feed.busy ? (batch?.total
      ? `<div class="progress">${number.format(batch.done)} از ${number.format(batch.total)} کانفیگ تست شد
           <progress max="${batch.total}" value="${batch.done}" aria-label="پیشرفت تست"></progress></div>`
      : '<p class="progress">در صف دریافت اشتراک</p>') : "";
    return `<article class="panel feed-card">
      <div class="feed-head"><h3>${escape(feed.name)}</h3><span class="badge ${feed.busy ? "running" : feed.enabled ? "success" : "muted"}">${status}</span></div>
      <dl><dt>اتصال</dt><dd><bdi>${escape(feed.iface)}</bdi></dd><dt>اجرای بعدی</dt><dd>${feed.enabled ? (feed.due ? time(feed.due) : "در انتظار اجرا") : "—"}</dd></dl>
      ${progress}
      <div class="actions" data-feed="${feed.id}">
        <button class="primary" data-action="run" ${feed.busy || !data.ready ? "disabled" : ""}>تست فوری Cloudflare</button>
        <button data-action="toggle" data-enabled="${feed.enabled ? 0 : 1}" ${!feed.enabled && !data.ready ? "disabled" : ""}>${feed.enabled ? "توقف" : "ادامه"}</button>
        <button data-action="delete" class="danger">حذف اشتراک</button>
        <select data-action="period" aria-label="دورهٔ اجرای ${escape(feed.name)}">
          <option value="12" ${feed.hours === 12 ? "selected" : ""}>هر ۱۲ ساعت</option>
          <option value="24" ${feed.hours === 24 ? "selected" : ""}>هر ۲۴ ساعت</option>
        </select>
      </div>
    </article>`;
  }).join("") || '<p class="muted">هنوز اشتراکی ذخیره نشده است.</p>');
}

function resultRow(result) {
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
    <div class="metrics">
      ${metric(result.ping_ms != null && !result.ping_method ? "زمان پاسخ کامل" : "پینگ", result.ping_ms, "ms",
        result.ping_error, result.ping_sample_ok, result.ping_sample_total, wholeNumber)}
      ${metric("دانلود", result.mbps, "Mbps", result.state === "done" ? result.error : null,
        result.download_sample_ok, result.download_sample_total)}
      ${metric("آپلود", result.upload_mbps, "Mbps", result.upload_error,
        result.upload_sample_ok, result.upload_sample_total, uploadSpeed)}
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
    const kind = ({manual:"اجرای فوری", scheduled:"دوره‌ای", legacy:"دور قبلی"})[batch.kind];
    return `<details data-round="${batch.id}" ${expanded ? "open" : ""}>
      <summary><bdi>${escape(batch.name)}</bdi> · ${kind} ${time(batch.created)}</summary>
      <div class="round-inner">
        ${messages.length ? `<p class="${batch.error ? "error" : "muted"}">${escape(messages.join(" · "))}</p>` : ""}
        ${batch.results.map(resultRow).join("") || (!messages.length ? '<p class="muted">نتیجه‌ای ثبت نشده است.</p>' : "")}
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
      if (button.dataset.action === "run") {
        const round = await api(path + "/run?cloudflare_upload=true", "POST", {});
        notice(round.queued ? "یک دور تست در صف قرار گرفت." : "یک دور تست در صف یا در حال اجراست.");
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
