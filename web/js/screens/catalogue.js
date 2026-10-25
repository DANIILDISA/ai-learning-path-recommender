// Каталог программ: все 161, с поиском и фильтрами.
//
// Здесь видны те 64 электронных курса, которые не попадают в маршрут никому.
// Недоступная программа не прячется, а объясняется: спрятанная выглядит для
// человека как несуществующая, и он никогда не узнает ни что она есть, ни
// что её можно открыть, заявив о себе на пятом шаге мастера.

import { api } from "../api.js";
import { deliveryHint, errorBox, fitTitles, titleNode } from "../components.js";
import { el, icon, mount, plural } from "../dom.js";
import { openCourse } from "../drawer.js";
import { session } from "../session.js";

const PAGE = 24;

const HOURS_FILTER = [
  { value: "", title: "Любая трудоёмкость" },
  { value: "short", title: "До 40 часов" },
  { value: "long", title: "Больше 40 часов" },
  // Обязательная позиция: у 64 курсов трудоёмкости нет вообще, и без неё
  // фильтр прятал бы 40% каталога молча.
  { value: "unspecified", title: "Часы не указаны (электронные курсы)" },
];

const DELIVERY_FILTER = [
  { value: "", title: "Любой формат" },
  { value: "ППК", title: "ППК" },
  { value: "Электронный курс", title: "Электронный курс" },
];

const STATE_LABEL = {
  passed: "Пройдено",
  restricted: "Недоступно",
};

let filters = { query: "", delivery: "", interest: "", hours: "" };
let offset = 0;

/** Открыть каталог сразу с фильтром (ссылка «Все электронные курсы»). */
export function presetCatalogue(values) {
  filters = { query: "", delivery: "", interest: "", hours: "", ...values };
  offset = 0;
}

function select(name, options, value, onChange) {
  return el(
    "select",
    { class: "filter", name, onchange: (event) => onChange(event.target.value) },
    options.map((option) =>
      el("option", {
        value: option.value,
        selected: option.value === value ? "selected" : null,
        text: option.title,
      })
    )
  );
}

function catalogueCard(item, onOpen) {
  const meta = [
    item.hours ? `${item.hours} ч` : null,
    item.study_form,
    item.competency_group,
  ].filter(Boolean);

  return el("article", { class: `cat-card is-${item.state}` }, [
    el("div", { class: "cat-card-head" }, [
      item.delivery
        ? el("span", { class: "chip", title: deliveryHint(item.delivery), text: item.delivery })
        : null,
      item.state !== "available"
        ? el("span", {
            class: `badge ${item.state === "passed" ? "badge-ok" : "badge-muted"}`,
            text: STATE_LABEL[item.state],
          })
        : null,
    ]),
    titleNode("h3", "cat-title", item.title),
    item.annotation_short
      ? el("p", { class: "cat-annotation", text: item.annotation_short })
      : null,
    meta.length ? el("p", { class: "cat-meta", text: meta.join(" · ") }) : null,
    // Причина недоступности -- полностью, без сокращения. Человек имеет право
    // знать, по какому признаку программа для него закрыта.
    item.state_note
      ? el("p", { class: "cat-note", text: item.state_note })
      : null,
    el("button", {
      class: "btn btn-secondary",
      type: "button",
      text: "Подробнее",
      onclick: () => onOpen({ course_key: item.course_key, position: 0 }),
    }),
  ]);
}

export async function renderCatalogue(root) {
  const userId = session.userId();

  const head = el("div", { class: "page-head" }, [
    el("div", {}, [
      el("h1", { class: "page-title", text: "Каталог программ" }),
      el("p", {
        class: "page-lead",
        text: "Все программы обучения из каталога. Пройденные и недоступные " +
              "показаны с пояснением, а не скрыты. Порядок — по алфавиту.",
      }),
    ]),
  ]);

  const searchInput = el("input", {
    class: "picker-input",
    type: "search",
    autocomplete: "off",
    value: filters.query,
    placeholder: "Поиск по названию и описанию программы",
  });

  const results = el("div", { class: "cat-grid" });
  const summary = el("p", { class: "picker-count", "aria-live": "polite" });
  const pager = el("div", { class: "pager" });

  let interests = [];
  try {
    interests = (await api.options()).interests;
  } catch {
    interests = [];
  }

  async function load() {
    results.replaceChildren(el("p", { class: "picker-count", text: "Загружаем…" }));
    pager.replaceChildren();

    const params = { limit: PAGE, offset };
    if (userId) params.user_id = userId;
    for (const [key, value] of Object.entries(filters)) {
      if (value) params[key] = value;
    }

    let data;
    try {
      data = await api.catalogue(params);
    } catch (error) {
      results.replaceChildren(errorBox(error.message, load));
      return;
    }

    const word = plural(data.total, ["программа", "программы", "программ"]);
    summary.textContent = data.total
      ? `Найдено ${data.total} ${word}` +
        (data.total > PAGE
          ? `, показаны ${offset + 1}–${Math.min(offset + PAGE, data.total)}`
          : "")
      : "Ничего не нашлось. Попробуйте изменить условия поиска или сбросьте фильтры.";

    results.replaceChildren(
      ...(data.items.length
        ? data.items.map((item) => catalogueCard(item, openCourse))
        : [
            el("button", {
              class: "btn btn-secondary",
              type: "button",
              text: "Сбросить фильтры",
              onclick: () => {
                filters = { query: "", delivery: "", interest: "", hours: "" };
                offset = 0;
                renderCatalogue(root);
              },
            }),
          ])
    );

    // Названия режутся по границе слова после вставки: нативное обрезание по
    // строкам оставляло «Санкт-…» посреди слова.
    fitTitles(results);

    if (data.total > PAGE) {
      pager.replaceChildren(
        el("button", {
          class: "btn btn-secondary",
          type: "button",
          text: "← Назад",
          disabled: offset === 0 ? true : null,
          onclick: () => { offset = Math.max(0, offset - PAGE); load(); },
        }),
        el("button", {
          class: "btn btn-secondary",
          type: "button",
          text: "Вперёд →",
          disabled: offset + PAGE >= data.total ? true : null,
          onclick: () => { offset += PAGE; load(); },
        })
      );
    }
  }

  const reload = () => { offset = 0; load(); };

  searchInput.addEventListener("input", () => {
    filters.query = searchInput.value.trim();
    reload();
  });

  mount(
    root,
    el("div", { class: "page" }, [
      head,
      el("div", { class: "cat-controls" }, [
        el("div", { class: "picker-search cat-search" }, [icon("search"), searchInput]),
        select("delivery", DELIVERY_FILTER, filters.delivery, (value) => {
          filters.delivery = value;
          reload();
        }),
        select(
          "interest",
          [{ value: "", title: "Любое направление" },
           ...interests.map((i) => ({ value: i.key, title: i.title }))],
          filters.interest,
          (value) => { filters.interest = value; reload(); }
        ),
        select("hours", HOURS_FILTER, filters.hours, (value) => {
          filters.hours = value;
          reload();
        }),
      ]),
      summary,
      results,
      pager,
    ])
  );

  load();
}
