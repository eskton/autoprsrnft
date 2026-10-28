"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const initData = (tg && tg.initData) || "";

const state = {
  me: null,
  tab: "pets",
  renderedTab: null,
  renderToken: 0,
  serverOffset: 0,
  shopFilter: "Все",
  adminSection: "orders",
  adminOrderStatus: "paid",
  tick: null,
  poll: null,
};

// ---------------------------------------------------------------------------
// Утилиты
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

/** Создаёт элемент. Текст всегда вставляется как текст (никакого innerHTML), поэтому XSS не пройдёт. */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf4 = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

/** Копейки -> «1 200 ₽» или «40,50 ₽» */
function rub(kop) {
  kop = Math.round(kop);
  return (kop % 100 === 0 ? nf0.format(kop / 100) : nf2.format(kop / 100)) + "\u00a0₽";
}
const rub4 = (kop) => nf4.format(kop / 100) + "\u00a0₽";
const kopToInput = (kop) => (kop % 100 === 0 ? String(kop / 100) : (kop / 100).toFixed(2));

function dt(ts) {
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function duration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), hrs = Math.floor((sec % 86400) / 3600), min = Math.floor((sec % 3600) / 60);
  if (d) return `${d} д ${hrs} ч`;
  if (hrs) return `${hrs} ч ${min} мин`;
  return `${Math.max(min, 1)} мин`;
}

const nowSec = () => Date.now() / 1000 + state.serverOffset;
const syncClock = (serverNow) => { state.serverOffset = serverNow - Date.now() / 1000; };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: { Authorization: "tma " + initData, "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* не JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || `Ошибка сервера (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(text, bad = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
  if (bad) haptic("error");
}

function haptic(kind) {
  try {
    if (kind === "tap") tg.HapticFeedback.impactOccurred("light");
    else tg.HapticFeedback.notificationOccurred(kind);
  } catch (e) { /* вне Telegram */ }
}

function ask(text) {
  return new Promise((resolve) => {
    try { tg.showConfirm(text, (ok) => resolve(!!ok)); }
    catch (e) { resolve(window.confirm(text)); }
  });
}

function openPay(url) {
  try {
    if (url.startsWith("https://t.me/")) { tg.openTelegramLink(url); return; }
  } catch (e) { /* вне Telegram */ }
  window.open(url, "_blank");
}

function setBalance(kop, animate = false) {
  const el = $("#balance");
  el.textContent = rub(kop);
  if (animate) { el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); }
}

async function refreshBalance() {
  const me = await api("/api/me");
  state.me = me;
  syncClock(me.now);
  setBalance(me.balance);
  return me;
}

// ---------------------------------------------------------------------------
// Нижняя шторка
// ---------------------------------------------------------------------------

function openSheet(...children) {
  stopPoll();
  const sheet = $("#sheet");
  sheet.replaceChildren(h("div", { class: "grip" }), ...children.flat().filter((c) => c != null && c !== false));
  sheet.hidden = false;
  $("#sheet-backdrop").hidden = false;
  try { tg.BackButton.show(); } catch (e) { /* старый клиент */ }
}

function closeSheet() {
  stopPoll();
  $("#sheet").hidden = true;
  $("#sheet-backdrop").hidden = true;
  try { tg.BackButton.hide(); } catch (e) { /* старый клиент */ }
}

function stopPoll() {
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
}

/** Шторка «оплати счёт в CryptoBot»: сама узнаёт, когда платёж прошёл. */
function waitPayment(invoice, { title, onPaid }) {
  const status = h("p", { class: "muted center small" },
    "Нажми «Оплатить» — откроется @CryptoBot. Как только платёж пройдёт, здесь всё обновится само.");
  openSheet(
    h("div", { class: "hero" }, "💳"),
    h("h3", { class: "center" }, title),
    h("div", { class: "spinner" }),
    status,
    h("button", { class: "btn block", onclick: () => openPay(invoice.pay_url) }, `Оплатить ${rub(invoice.amount)}`),
    h("div", { style: "height:8px" }),
    h("button", { class: "btn block ghost", onclick: closeSheet }, "Оплачу позже"),
    h("p", { class: "note center" }, "Неоплаченные счета лежат в Профиле."),
  );
  const started = Date.now();
  state.poll = setInterval(async () => {
    if (Date.now() - started > 30 * 60 * 1000) { stopPoll(); return; }
    try {
      const inv = await api(`/api/invoices/${invoice.id}`);
      if (inv.status === "paid") { stopPoll(); haptic("success"); onPaid(inv); }
      else if (inv.status === "expired" || inv.status === "failed") {
        stopPoll();
        status.textContent = "Счёт больше не действует — создай новый.";
      }
    } catch (e) { /* сеть моргнула — попробуем ещё */ }
  }, 3000);
}

function errorBox(err) {
  return h("div", { class: "error-box" },
    h("div", { class: "empty" }, h("div", { class: "big" }, "😿")),
    h("p", {}, err.message || String(err)),
    h("button", { class: "btn", onclick: () => location.reload() }, "Обновить"));
}

const field = (label, input) => h("label", { class: "field" }, h("span", {}, label), input);
const textInput = (value, placeholder, mode) =>
  h("input", { class: "input", value: value ?? "", placeholder: placeholder || "", inputmode: mode || null, autocomplete: "off" });
function checkbox(checked) { const el = h("input", { type: "checkbox" }); el.checked = checked; return el; }
const sumRow = (label, value, cls = "") => h("div", { class: "row " + cls }, h("span", { class: "muted" }, label), h("span", { class: "num" }, value));

// ---------------------------------------------------------------------------
// Вкладки
// ---------------------------------------------------------------------------

async function render() {
  stopTick();
  const token = ++state.renderToken;
  const view = $("#view");
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === state.tab);
  if (state.renderedTab !== state.tab) {
    view.replaceChildren(h("div", { class: "loading" }, "Загрузка…"));
    window.scrollTo(0, 0);
  }
  const renderers = { pets: renderPets, shop: renderShop, profile: renderProfile, admin: renderAdmin };
  try {
    const node = await renderers[state.tab]();
    if (token !== state.renderToken) return; // пока грузились, пользователь ушёл на другую вкладку
    view.replaceChildren(node);
    state.renderedTab = state.tab;
  } catch (e) {
    if (token !== state.renderToken) return;
    view.replaceChildren(errorBox(e));
    state.renderedTab = null;
  }
}

function stopTick() {
  if (state.tick) { clearInterval(state.tick); state.tick = null; }
}

// --- Питомцы ---------------------------------------------------------------

async function renderPets() {
  const data = await api("/api/pets");
  syncClock(data.now);
  const wrap = h("div");

  wrap.append(h("h2", {}, "Мои питомцы"));
  if (data.mine.length) {
    const bankAmount = h("div", { class: "amount" });
    const claimBtn = h("button", { class: "btn", onclick: claim }, "Собрать");
    wrap.append(h("div", { class: "card bank" },
      h("div", { class: "grow" }, h("div", { class: "label" }, "Накопили питомцы"), bankAmount),
      claimBtn));
    const cards = data.mine.map(petCard);
    wrap.append(...cards.map((c) => c.node));
    const update = () => {
      const now = nowSec();
      let claimable = 0;
      for (const c of cards) claimable += c.update(now);
      bankAmount.textContent = rub4(claimable);
      claimBtn.disabled = Math.floor(claimable) < 1;
    };
    update();
    state.tick = setInterval(update, 250);
  } else {
    wrap.append(h("div", { class: "card empty" },
      h("div", { class: "big" }, "🥚"),
      h("div", {}, "У тебя пока нет питомцев"),
      h("div", { class: "muted small" }, "Купи первого — он будет каждый день приносить бонусы на донат в играх")));
  }

  wrap.append(h("h2", {}, "Купить питомца"));
  if (!data.catalog.length) wrap.append(h("div", { class: "card empty muted" }, "Скоро здесь появятся питомцы"));
  wrap.append(...data.catalog.map(offerCard));
  wrap.append(h("p", { class: "note" },
    "Питомцы покупаются за крипту через @CryptoBot. Бонусы, которые они приносят, тратятся на донат в магазине — вывести их нельзя."));
  return wrap;
}

function petCard(p) {
  const ring = h("div", { class: "ring" }, h("span", {}, p.emoji));
  const earned = h("span", { class: "num" });
  const fill = h("i");
  const left = h("span", { class: "muted nowrap" });
  const pct = p.price ? Math.round((p.total_return / p.price - 1) * 100) : 0;
  const node = h("div", { class: "card pet" },
    ring,
    h("div", { class: "grow" },
      h("div", { class: "row" }, h("div", { class: "name grow" }, p.name), h("span", { class: "chip good" }, `${pct >= 0 ? "+" : ""}${pct}%`)),
      h("div", { class: "bar" }, fill),
      h("div", { class: "row small" }, h("span", { class: "grow" }, "Принёс ", earned, ` из ${rub(p.total_return)}`), left)));
  const update = (now) => {
    const frac = Math.min(1, Math.max(0, (now - p.started_at) / (p.ends_at - p.started_at)));
    const acc = p.total_return * frac;
    ring.style.setProperty("--p", (frac * 100).toFixed(2));
    ring.classList.toggle("done", frac >= 1);
    fill.style.width = (frac * 100).toFixed(2) + "%";
    earned.textContent = rub(Math.floor(acc));
    left.textContent = frac >= 1 ? "✅ всё принёс" : "ещё " + duration(p.ends_at - now);
    return Math.max(0, acc - p.claimed);
  };
  return { node, update };
}

function offerCard(t) {
  const pct = t.price ? Math.round((t.total_return / t.price - 1) * 100) : 0;
  return h("div", { class: "card offer" },
    h("div", { class: "row" },
      h("div", { class: "emoji" }, t.emoji),
      h("div", { class: "grow" },
        h("div", { class: "name" }, t.name),
        t.description ? h("div", { class: "muted small" }, t.description) : null)),
    h("div", { class: "chips" },
      h("span", { class: "chip good" }, `${pct >= 0 ? "+" : ""}${pct}%`),
      h("span", { class: "chip" }, `${t.duration_days} дн.`),
      h("span", { class: "chip" }, `≈ ${rub(Math.floor(t.total_return / t.duration_days))} в день`)),
    h("div", { class: "small muted", style: "margin-top:8px" }, `Принесёт ${rub(t.total_return)} бонусами`),
    h("button", { class: "btn block", onclick: () => buyPet(t) }, `Купить за ${rub(t.price)}`));
}

async function buyPet(t) {
  haptic("tap");
  const ok = await ask(`Купить «${t.name}» за ${rub(t.price)}?\nОн принесёт ${rub(t.total_return)} бонусами за ${t.duration_days} дн.`);
  if (!ok) return;
  try {
    const { invoice } = await api("/api/pets/buy", { body: { type_id: t.id } });
    waitPayment(invoice, {
      title: `${t.emoji} ${t.name}`,
      onPaid: () => {
        closeSheet();
        toast(`🎉 ${t.name} теперь твой!`);
        if (state.tab === "pets") render();
      },
    });
  } catch (e) {
    toast(e.message, true);
  }
}

async function claim(ev) {
  const btn = ev.currentTarget;
  btn.disabled = true;
  try {
    const r = await api("/api/pets/claim", { method: "POST" });
    if (r.claimed > 0) {
      haptic("success");
      toast(`+${rub(r.claimed)} в бонусы 💎`);
      setBalance(r.balance, true);
    } else {
      toast("Пока нечего собирать");
    }
    await render();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

// --- Магазин ---------------------------------------------------------------

async function renderShop() {
  const [{ products }] = await Promise.all([api("/api/shop"), refreshBalance()]);
  const games = ["Все", ...new Set(products.map((p) => p.game))];
  if (!games.includes(state.shopFilter)) state.shopFilter = "Все";

  const list = h("div");
  const fill = () => {
    const shown = products.filter((p) => state.shopFilter === "Все" || p.game === state.shopFilter);
    const nodes = [];
    let lastGame = null;
    for (const p of shown) {
      if (p.game !== lastGame) { nodes.push(h("h2", {}, p.game)); lastGame = p.game; }
      nodes.push(h("div", { class: "card product" },
        h("div", { class: "grow" },
          h("div", { class: "title" }, p.title),
          p.description ? h("div", { class: "muted small" }, p.description) : null),
        h("div", { class: "price" }, rub(p.price)),
        h("button", { class: "btn sm", onclick: () => checkout(p) }, "Купить")));
    }
    if (!shown.length) nodes.push(h("div", { class: "card empty muted", style: "margin-top:10px" }, "Товаров пока нет"));
    list.replaceChildren(...nodes);
  };
  const filters = h("div", { class: "filters" }, games.map((g) =>
    h("button", {
      class: g === state.shopFilter ? "active" : null,
      onclick: (ev) => {
        state.shopFilter = g;
        for (const b of filters.children) b.classList.toggle("active", b === ev.currentTarget);
        fill();
      },
    }, g)));
  fill();
  return h("div", {}, filters, list,
    h("p", { class: "note" }, "Плати бонусами от питомцев, криптой или частично тем и другим. Донат выдаём вручную — когда будет готово, бот пришлёт сообщение."));
}

function checkout(p) {
  haptic("tap");
  const balance = state.me ? state.me.balance : 0;
  const details = h("input", { class: "input", maxlength: "300", placeholder: p.input_label, autocomplete: "off" });
  const useBonus = checkbox(balance > 0);
  useBonus.disabled = balance <= 0;
  const bonusValue = h("span", { class: "num" });
  const cryptoValue = h("span", { class: "num" });
  const payBtn = h("button", { class: "btn block" });

  const recalc = () => {
    const bonus = useBonus.checked ? Math.min(balance, p.price) : 0;
    const crypto = p.price - bonus;
    bonusValue.textContent = bonus ? "−" + rub(bonus) : "0 ₽";
    cryptoValue.textContent = rub(crypto);
    payBtn.textContent = crypto ? `Оплатить ${rub(crypto)} криптой` : "Оплатить бонусами";
  };
  useBonus.addEventListener("change", recalc);
  recalc();

  payBtn.addEventListener("click", async () => {
    const text = details.value.trim();
    if (!text) { details.focus(); toast(`Укажи: ${p.input_label}`, true); return; }
    payBtn.disabled = true;
    try {
      const res = await api("/api/orders", { body: { product_id: p.id, details: text, use_bonus: useBonus.checked } });
      refreshBalance().catch(() => {});
      if (res.invoice) {
        waitPayment(res.invoice, { title: `Заказ #${res.order.id}`, onPaid: () => orderPaidSheet(res.order) });
      } else {
        orderPaidSheet(res.order);
      }
    } catch (e) {
      toast(e.message, true);
      payBtn.disabled = false;
    }
  });

  openSheet(
    h("h3", {}, `${p.game} · ${p.title}`),
    p.description ? h("p", { class: "muted small" }, p.description) : null,
    field(p.input_label, details),
    h("label", { class: "check" }, useBonus,
      h("span", {}, balance > 0 ? `Списать бонусы (есть ${rub(balance)})` : "Бонусов пока нет — их приносят питомцы")),
    h("div", { class: "summary" },
      sumRow("Цена", rub(p.price)),
      h("div", { class: "row" }, h("span", { class: "muted" }, "Бонусами"), bonusValue),
      h("div", { class: "row total" }, h("span", {}, "Криптой"), cryptoValue)),
    payBtn);
}

function orderPaidSheet(order) {
  haptic("success");
  refreshBalance().catch(() => {});
  openSheet(
    h("div", { class: "hero" }, "🎉"),
    h("h3", { class: "center" }, `Заказ #${order.id} оплачен`),
    h("p", { class: "muted center" }, "Выполним вручную — когда донат придёт, бот напишет тебе."),
    h("button", { class: "btn block", onclick: () => { closeSheet(); state.tab = "profile"; render(); } }, "Мои заказы"),
    h("div", { style: "height:8px" }),
    h("button", { class: "btn block ghost", onclick: closeSheet }, "Ок"));
}

// --- Профиль ---------------------------------------------------------------

const ORDER_STATUS = {
  awaiting_payment: ["Ждёт оплаты", "warn"],
  paid: ["Выполняем", "warn"],
  done: ["Выполнен", "ok"],
  rejected: ["Отклонён", "bad"],
};

async function renderProfile() {
  const data = await api("/api/profile");
  setBalance(data.balance);
  if (state.me) state.me.balance = data.balance;
  const user = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
  const wrap = h("div");

  wrap.append(
    h("div", { class: "card row", style: "margin-top:8px" },
      h("div", { class: "ring done", style: "width:52px;height:52px" }, h("span", { style: "font-size:26px" }, "😎")),
      h("div", { class: "grow" },
        h("div", { style: "font-weight:700" }, (user && user.first_name) || "Профиль"),
        h("div", { class: "muted small" }, "Бонусный баланс")),
      h("div", { style: "font-size:22px;font-weight:800;color:var(--gold)" }, rub(data.balance))),
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("span", { class: "muted small" }, "Питомцев"), h("b", {}, data.stats.pets)),
      h("div", { class: "stat" }, h("span", { class: "muted small" }, "Принесли бонусов"), h("b", {}, rub(data.stats.income)))),
  );

  if (data.invoices.length) {
    wrap.append(h("h2", {}, "Ждут оплаты"), h("div", { class: "card list" }, data.invoices.map((inv) =>
      h("div", { class: "item" },
        h("div", { class: "grow" }, h("div", {}, inv.title), h("div", { class: "muted small" }, dt(inv.created_at))),
        h("button", { class: "btn sm", onclick: () => openPay(inv.pay_url) }, `Оплатить ${rub(inv.amount)}`)))));
  }

  wrap.append(h("h2", {}, "Мои заказы"));
  if (!data.orders.length) wrap.append(h("div", { class: "card empty muted" }, "Заказов пока нет"));
  else wrap.append(h("div", { class: "card list" }, data.orders.map(orderItem)));

  wrap.append(h("h2", {}, "История бонусов"));
  if (!data.ledger.length) wrap.append(h("div", { class: "card empty muted" }, "Здесь появятся бонусы от питомцев"));
  else wrap.append(h("div", { class: "card list" }, data.ledger.map((l) =>
    h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", {}, l.note || "—"), h("div", { class: "muted small" }, dt(l.created_at))),
      h("b", { class: "num " + (l.amount > 0 ? "plus" : "minus") }, (l.amount > 0 ? "+" : "−") + rub(Math.abs(l.amount)))))));
  return wrap;
}

function orderItem(o) {
  const [label, cls] = ORDER_STATUS[o.status] || [o.status, ""];
  return h("div", { class: "item" },
    h("div", { class: "grow" },
      h("div", {}, `#${o.id} ${o.title}`),
      h("div", { class: "muted small" }, `${dt(o.created_at)} · ${o.details}`),
      o.status === "rejected" && o.admin_note ? h("div", { class: "small minus" }, o.admin_note) : null,
      o.pay_url ? h("button", { class: "btn sm", style: "margin-top:6px", onclick: () => openPay(o.pay_url) }, "Оплатить") : null),
    h("div", { class: "right" }, h("div", { class: "nowrap" }, rub(o.price)), h("span", { class: "badge " + cls }, label)));
}

// --- Админка ---------------------------------------------------------------

const ADMIN_SECTIONS = [["orders", "Заказы"], ["stats", "Статистика"], ["products", "Товары"], ["pets", "Питомцы"], ["bonus", "Бонусы"]];

async function renderAdmin() {
  const seg = h("div", { class: "seg", style: "margin-top:8px" }, ADMIN_SECTIONS.map(([key, label]) =>
    h("button", { class: key === state.adminSection ? "active" : null, onclick: () => { state.adminSection = key; render(); } }, label)));
  const sections = { orders: adminOrders, stats: adminStats, products: adminProducts, pets: adminPets, bonus: adminBonus };
  return h("div", {}, seg, await sections[state.adminSection]());
}

async function adminOrders() {
  const { orders } = await api(`/api/admin/orders?status=${state.adminOrderStatus}`);
  const statuses = [["paid", "Ждут выполнения"], ["done", "Выполненные"], ["rejected", "Отклонённые"]];
  return h("div", {},
    h("div", { class: "filters wrap" }, statuses.map(([key, label]) =>
      h("button", { class: key === state.adminOrderStatus ? "active" : null, onclick: () => { state.adminOrderStatus = key; render(); } }, label))),
    orders.length ? orders.map(adminOrderCard) : h("div", { class: "card empty muted" }, "Заказов нет"));
}

function adminOrderCard(o) {
  const who = o.username ? "@" + o.username : (o.first_name || "без имени");
  const pay = [o.bonus_used ? `${rub(o.bonus_used)} бонусами` : null, o.crypto_amount ? `${rub(o.crypto_amount)} криптой` : null]
    .filter(Boolean).join(" + ");
  const card = h("div", { class: "card" },
    h("div", { class: "row" }, h("b", { class: "grow" }, `#${o.id} ${o.title}`), h("span", { class: "nowrap" }, rub(o.price))),
    h("div", { class: "muted small" }, `${who} · id ${o.user_id} · ${dt(o.created_at)}`),
    h("div", { class: "details" }, o.details),
    h("div", { class: "muted small" }, pay),
    o.admin_note ? h("div", { class: "small minus" }, "Причина: " + o.admin_note) : null);
  if (o.status === "paid") {
    const actions = h("div", { class: "actions" },
      h("button", {
        class: "btn ok sm",
        onclick: async () => {
          if (!(await ask(`Заказ #${o.id} выполнен?`))) return;
          try {
            await api(`/api/admin/orders/${o.id}/done`, { method: "POST" });
            haptic("success");
            toast("Готово ✅ Покупатель получил уведомление");
            render();
          } catch (e) { toast(e.message, true); }
        },
      }, "✅ Выполнено"),
      h("button", { class: "btn danger sm", onclick: () => rejectForm(o, card, actions) }, "❌ Отказ"));
    card.append(actions);
  }
  return card;
}

function rejectForm(o, card, actions) {
  const note = textInput("", "Причина (увидит покупатель)");
  note.maxLength = 300;
  const form = h("div", { style: "margin-top:10px" }, note,
    h("div", { class: "actions" },
      h("button", { class: "btn ghost sm", onclick: () => { form.remove(); actions.hidden = false; } }, "Назад"),
      h("button", {
        class: "btn danger sm",
        onclick: async () => {
          try {
            await api(`/api/admin/orders/${o.id}/reject`, { body: { note: note.value } });
            toast("Отклонён, сумма вернулась покупателю бонусами");
            render();
          } catch (e) { toast(e.message, true); }
        },
      }, "Отклонить")));
  actions.hidden = true;
  card.append(form);
  note.focus();
}

async function adminStats() {
  const s = await api("/api/admin/stats");
  const stat = (label, value) => h("div", { class: "stat" }, h("span", { class: "muted small" }, label), h("b", {}, value));
  return h("div", { class: "stats", style: "margin-top:10px" },
    stat("Пользователей", s.users),
    stat("Ждут выполнения", s.orders_waiting),
    stat("Питомцев продано", s.pets_sold),
    stat("Активных питомцев", s.pets_active),
    stat("Крипта за питомцев", rub(s.crypto_pets)),
    stat("Крипта за донат", rub(s.crypto_orders)),
    stat("Выполнено заказов", s.orders_done),
    stat("Бонусы на балансах", rub(s.owed_balances)),
    h("div", { class: "stat wide" },
      h("span", { class: "muted small" }, "Ещё должен выдать донатом"),
      h("b", {}, rub(s.owed_pets + s.owed_balances)),
      h("div", { class: "muted small" },
        `${rub(s.owed_pets)} ещё принесут питомцы + ${rub(s.owed_balances)} уже лежит на балансах. Держи эти деньги в резерве.`)));
}

async function adminProducts() {
  const { products } = await api("/api/admin/products");
  return h("div", {},
    h("button", { class: "btn block", style: "margin:10px 0", onclick: () => productForm(null) }, "+ Добавить товар"),
    products.map((p) => h("div", { class: "card product" + (p.active ? "" : " inactive") },
      h("div", { class: "grow" },
        h("div", { class: "title" }, `${p.game} · ${p.title}`),
        h("div", { class: "muted small" }, p.active ? `Спрашиваем: ${p.input_label}` : "Скрыт")),
      h("div", { class: "price" }, rub(p.price)),
      h("button", { class: "btn ghost sm", onclick: () => productForm(p), "aria-label": "Изменить" }, "✏️"))));
}

function productForm(p) {
  const f = {
    game: textInput(p && p.game, "Roblox"),
    title: textInput(p && p.title, "400 Robux"),
    description: textInput(p && p.description, "Необязательно"),
    price: textInput(p ? kopToInput(p.price) : "", "450", "decimal"),
    input_label: textInput(p ? p.input_label : "Ник или ID в игре", "Ник в Roblox"),
    sort: textInput(p ? String(p.sort) : "0", "0", "numeric"),
  };
  const active = checkbox(p ? !!p.active : true);
  const save = h("button", { class: "btn block" }, p ? "Сохранить" : "Добавить");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await api(p ? `/api/admin/products/${p.id}` : "/api/admin/products", {
        body: {
          game: f.game.value, title: f.title.value, description: f.description.value,
          price: f.price.value, input_label: f.input_label.value,
          sort: Number(f.sort.value || 0), active: active.checked,
        },
      });
      closeSheet();
      toast("Сохранено");
      render();
    } catch (e) {
      toast(e.message, true);
      save.disabled = false;
    }
  });
  openSheet(
    h("h3", {}, p ? "Товар" : "Новый товар"),
    field("Игра", f.game),
    field("Название", f.title),
    field("Описание", f.description),
    h("div", { class: "two" }, field("Цена, ₽", f.price), field("Порядок", f.sort)),
    field("Что спросить у покупателя", f.input_label),
    h("label", { class: "check" }, active, h("span", {}, "Показывать в магазине")),
    save);
}

async function adminPets() {
  const { pets } = await api("/api/admin/pets");
  return h("div", {},
    h("button", { class: "btn block", style: "margin:10px 0", onclick: () => petForm(null) }, "+ Добавить питомца"),
    pets.map((t) => {
      const pct = Math.round((t.total_return / t.price - 1) * 100);
      return h("div", { class: "card product" + (t.active ? "" : " inactive") },
        h("div", { style: "font-size:30px" }, t.emoji),
        h("div", { class: "grow" },
          h("div", { class: "title" }, t.name),
          h("div", { class: "muted small" }, `${rub(t.price)} → ${rub(t.total_return)} за ${t.duration_days} дн. (${pct >= 0 ? "+" : ""}${pct}%)${t.active ? "" : " · скрыт"}`)),
        h("button", { class: "btn ghost sm", onclick: () => petForm(t), "aria-label": "Изменить" }, "✏️"));
    }),
    h("p", { class: "note" }, "Изменения действуют только на новые покупки — уже купленные питомцы работают на старых условиях."));
}

function petForm(t) {
  const f = {
    name: textInput(t && t.name, "Пёс"),
    emoji: textInput(t ? t.emoji : "🐾", "🐶"),
    description: textInput(t && t.description, "Необязательно"),
    price: textInput(t ? kopToInput(t.price) : "", "1000", "decimal"),
    total_return: textInput(t ? kopToInput(t.total_return) : "", "1200", "decimal"),
    duration_days: textInput(t ? String(t.duration_days) : "30", "30", "numeric"),
    sort: textInput(t ? String(t.sort) : "0", "0", "numeric"),
  };
  const active = checkbox(t ? !!t.active : true);
  const hint = h("p", { class: "note" });
  const recalc = () => {
    const price = parseFloat(f.price.value.replace(",", "."));
    const ret = parseFloat(f.total_return.value.replace(",", "."));
    const days = parseInt(f.duration_days.value, 10);
    if (price > 0 && ret >= 0 && days > 0) {
      const pct = Math.round((ret / price - 1) * 100);
      hint.textContent = `Бонус покупателю: ${pct >= 0 ? "+" : ""}${pct}% · ≈ ${rub(Math.floor((ret * 100) / days))} в день. Убедись, что наценка на донат покрывает этот бонус.`;
    } else {
      hint.textContent = "";
    }
  };
  for (const input of [f.price, f.total_return, f.duration_days]) input.addEventListener("input", recalc);
  recalc();
  const save = h("button", { class: "btn block" }, t ? "Сохранить" : "Добавить");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await api(t ? `/api/admin/pets/${t.id}` : "/api/admin/pets", {
        body: {
          name: f.name.value, emoji: f.emoji.value, description: f.description.value,
          price: f.price.value, total_return: f.total_return.value,
          duration_days: Number(f.duration_days.value), sort: Number(f.sort.value || 0), active: active.checked,
        },
      });
      closeSheet();
      toast("Сохранено");
      render();
    } catch (e) {
      toast(e.message, true);
      save.disabled = false;
    }
  });
  openSheet(
    h("h3", {}, t ? "Питомец" : "Новый питомец"),
    h("div", { class: "two" }, field("Имя", f.name), field("Эмодзи", f.emoji)),
    field("Описание", f.description),
    h("div", { class: "two" }, field("Цена, ₽", f.price), field("Принесёт всего, ₽", f.total_return)),
    h("div", { class: "two" }, field("Срок, дней", f.duration_days), field("Порядок", f.sort)),
    hint,
    h("label", { class: "check" }, active, h("span", {}, "Продавать")),
    save);
}

async function adminBonus() {
  const userId = textInput("", "Например 123456789", "numeric");
  const amount = textInput("", "+100 или -50", "decimal");
  const note = textInput("", "За что (увидит пользователь)");
  const btn = h("button", { class: "btn block" }, "Применить");
  btn.addEventListener("click", async () => {
    const id = parseInt(userId.value, 10);
    if (!id) { toast("Укажи Telegram id", true); return; }
    btn.disabled = true;
    try {
      const r = await api("/api/admin/balance", { body: { user_id: id, amount: amount.value, note: note.value } });
      toast(`Готово. Баланс пользователя: ${rub(r.balance)}`);
      amount.value = "";
      note.value = "";
    } catch (e) {
      toast(e.message, true);
    }
    btn.disabled = false;
  });
  return h("div", { class: "card", style: "margin-top:10px" },
    field("Telegram id пользователя", userId),
    field("Сумма, ₽ (минус — списать)", amount),
    field("Комментарий", note),
    btn,
    h("p", { class: "note" }, "id покупателя видно в заказах и в сообщениях бота."));
}

// ---------------------------------------------------------------------------
// Запуск
// ---------------------------------------------------------------------------

async function init() {
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor("secondary_bg_color");
      tg.setBackgroundColor("secondary_bg_color");
      tg.BackButton.onClick(closeSheet);
    } catch (e) { /* старый клиент */ }
  }
  $("#sheet-backdrop").addEventListener("click", closeSheet);
  for (const b of document.querySelectorAll("#tabs button")) {
    b.addEventListener("click", () => {
      if (state.tab === b.dataset.tab) return;
      haptic("tap");
      closeSheet();
      state.tab = b.dataset.tab;
      render();
    });
  }
  $("#balance-btn").addEventListener("click", () => {
    if (state.tab === "profile") return;
    closeSheet();
    state.tab = "profile";
    render();
  });

  if (!initData) {
    $("#tabs").hidden = true;
    $("#view").replaceChildren(errorBox(new Error("Открой магазин через кнопку в Telegram-боте")));
    return;
  }
  try {
    await refreshBalance();
  } catch (e) {
    $("#view").replaceChildren(errorBox(e));
    return;
  }
  $('#tabs [data-tab="admin"]').hidden = !state.me.is_admin;
  render();

  // Вернулись из CryptoBot или из другого чата — обновляем данные
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    refreshBalance().catch(() => {});
    if (state.tab === "pets" && $("#sheet").hidden) render();
  });
}

init();
