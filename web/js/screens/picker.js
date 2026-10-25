// Экран выбора профиля -- нулевой, которого в макете нет.
//
// Макет нарисован для портала, где человек уже авторизован. У продукта
// авторизации нет и в этой поставке не будет, а профилей 323 и они
// обезличены. Экран собран из деталей макета: та же карточка с границей
// --line, тот же радиус, тот же поиск.

import { api } from "../api.js";
import { el, icon, mount, plural } from "../dom.js";
import { session, flash } from "../session.js";

const PAGE = 40;
let pendingNotice = null;
const PROFILE_FORMS = ["профиль", "профиля", "профилей"];

function matches(profile, needle) {
  if (!needle) return true;
  return (
    profile.user_id.toLowerCase().includes(needle) ||
    profile.position.toLowerCase().includes(needle) ||
    profile.iogv.toLowerCase().includes(needle)
  );
}

export async function renderPicker(root, onPicked) {
  const intro = el("div", {}, [
    el("h1", { class: "page-title", text: "Здравствуйте." }),
    el("p", {
      class: "page-lead",
      text:
        "Найдите себя по должности или органу власти — и мы соберём личную " +
        "подборку программ обучения. Это приглашение, а не поручение: каждую " +
        "программу можно принять, отложить или пропустить.",
    }),
  ]);

  const input = el("input", {
    class: "picker-input",
    id: "picker-input",
    type: "search",
    autocomplete: "off",
    placeholder: "Например: начальник отдела или Администрация Губернатора",
  });
  const search = el("div", { class: "picker-search" }, [icon("search"), input]);

  const count = el("p", { class: "picker-count", "aria-live": "polite" });
  const list = el("div", { class: "picker-list" });
  const more = el("div", { class: "picker-more" });
  const iogvFilter = el("select", { class: "filter picker-iogv", "aria-label": "Орган власти" });

  // Объяснение, почему человек снова видит этот экран (например, данные
  // заменили и прежнего профиля в них нет).
  // Экран может перерисоваться раньше, чем человек его прочитает (два
  // перехода подряд), поэтому сообщение живёт, пока он не выберет профиль.
  const taken = flash.take();
  if (taken) pendingNotice = taken;
  const notice = pendingNotice;

  mount(
    root,
    el("div", { class: "picker" }, [
      intro,
      notice ? el("p", { class: "data-notice", role: "status", text: notice }) : null,
      search,
      iogvFilter,
      count,
      list,
      more,
    ])
  );

  let profiles = [];
  try {
    profiles = await api.profiles();
  } catch (error) {
    count.textContent = error.message;
    return;
  }

  // Фильтр по органу власти: должность без органа ничего не говорит, а
  // поиск по строке требует знать, что вводить.
  const iogvs = [...new Set(profiles.map((profile) => profile.iogv))].sort((a, b) =>
    a.localeCompare(b, "ru")
  );
  iogvFilter.replaceChildren(
    el("option", { value: "", text: "Любой орган власти" }),
    ...iogvs.map((name) => el("option", { value: name, text: name }))
  );

  let shown = PAGE;

  function draw() {
    const needle = input.value.trim().toLowerCase();
    const iogv = iogvFilter.value;
    const found = profiles.filter(
      (profile) => matches(profile, needle) && (!iogv || profile.iogv === iogv)
    );

    // Показываем первые PAGE: 323 строки разом никто не читает, а поиск
    // сужает список быстрее, чем прокрутка.
    count.textContent = found.length
      ? `Найдено ${found.length} ${plural(found.length, PROFILE_FORMS)}` +
        (found.length > shown ? `, показаны первые ${shown}` : "")
      : "Ничего не нашлось. Попробуйте часть названия должности или органа.";

    list.replaceChildren(
      ...found.slice(0, shown).map((profile) =>
        el(
          "button",
          {
            class: "picker-item",
            type: "button",
            onclick: () => {
              pendingNotice = null;
              session.setUserId(profile.user_id);
              onPicked();
            },
          },
          [
            el("span", { class: "picker-item-id", text: profile.user_id }),
            el("span", { class: "picker-item-position", text: profile.position }),
            el("span", { class: "picker-item-iogv", text: profile.iogv }),
          ]
        )
      )
    );

    more.replaceChildren(
      ...(found.length > shown
        ? [
            el("button", {
              class: "btn btn-secondary",
              type: "button",
              text: `Показать ещё ${Math.min(PAGE, found.length - shown)}`,
              onclick: () => { shown += PAGE; draw(); },
            }),
          ]
        : [])
    );
  }

  more.replaceChildren();

  input.addEventListener("input", () => { shown = PAGE; draw(); });
  iogvFilter.addEventListener("change", () => { shown = PAGE; draw(); });
  draw();

  // preventScroll обязателен: обычный focus() прокручивает страницу к полю, и
  // человек попадает на экран с обрезанным приветствием -- заголовок уезжает
  // за верхнюю кромку ещё до того, как он что-нибудь сделал.
  input.focus({ preventScroll: true });
}
