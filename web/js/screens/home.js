// Главная: «Рекомендовано для вас».
//
// Три программы, под ними приглашение настроить рекомендации, ниже -- ряд
// персональных электронных курсов и полоса «Как работает подбор». Любая
// отметка («хочу пройти», «не сейчас», «неактуально») уводит карточку из
// подборки, и на её место сразу встаёт следующая по рейтингу: очередь приходит
// с бэкенда целиком, экран только показывает по три первых и подставляет
// следующую. Никакого «Обновляем…»: ждать нечего, всё уже посчитано.

import { api } from "../api.js";
import {
  cardSkeleton,
  errorBox,
  fitTitles,
  loadingNote,
  onFeedbackChanged,
  recommendationCard,
} from "../components.js";
import { el, icon, mount } from "../dom.js";
import { openCourse } from "../drawer.js";
import { quotaBar, quotaStore } from "../quota.js";
import { session } from "../session.js";
import { presetCatalogue } from "./catalogue.js";

// Сколько карточек каждого ряда на виду. Остальные -- очередь за ними.
const VISIBLE = 3;

// Сколько занимает уход карточки, мс. Короткая: человек не должен ждать, но
// пропажа без перехода выглядит как сбой.
const LEAVE_MS = 150;

const HOW_STEPS = [
  ["circle", "Ваш профиль", "Учитываем должность и ИОГВ"],
  ["list", "История обучения", "Исключаем уже пройденные программы"],
  ["spark", "Персональные рекомендации", "Подбираем подходящие следующие курсы"],
];

// «Не сейчас» под баннером прячет его до перезагрузки, а не навсегда:
// человек, отложивший настройку, завтра может передумать, и отнимать у него
// эту возможность насовсем мы не вправе.
let bannerDismissed = false;

const reducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function howItWorks() {
  const steps = [];
  HOW_STEPS.forEach(([iconName, name, text], index) => {
    if (index > 0) {
      steps.push(el("span", { class: "how-arrow" }, [icon("arrow-right")]));
    }
    steps.push(
      el("div", { class: "how-step" }, [
        el("span", { class: "how-step-icon" }, [icon(iconName)]),
        el("div", {}, [
          el("div", { class: "how-step-name", text: name }),
          el("div", { class: "how-step-text", text }),
        ]),
      ])
    );
  });
  return el("section", { class: "how section", id: "how-it-works" }, [
    el("h2", { class: "how-title", text: "Как работает подбор" }),
    ...steps,
  ]);
}

function wizardBanner() {
  const banner = el("section", { class: "banner section" }, [
    el("span", { class: "banner-icon" }, [icon("spark")]),
    el("div", { class: "banner-body" }, [
      el("h2", { class: "banner-title", text: "Сделаем рекомендации точнее?" }),
      el("p", {
        class: "banner-text",
        // Пять, а не четыре. В макете написано «четыре вопроса»; шагов стало
        // пять, и человек, досчитавший до пятого, решил бы, что его обманули.
        text:
          "Ответьте на пять коротких вопросов — и мы добавим ваши карьерные цели, " +
          "интересы и программы, доступные лично вам. Займёт около минуты.",
      }),
    ]),
    el("div", { class: "banner-actions" }, [
      el("a", {
        class: "btn btn-primary btn-lg",
        href: "#/wizard/1",
        text: "Настроить рекомендации",
      }),
      el("button", {
        class: "btn btn-ghost",
        type: "button",
        text: "Не сейчас",
        onclick: () => {
          bannerDismissed = true;
          banner.remove();
        },
      }),
    ]),
  ]);
  return banner;
}

function head(track) {
  const explain = el("a", {
    class: "page-explain",
    href: "#how-it-works",
    text: "Как формируются рекомендации?",
    onclick: (event) => {
      // Отдельного экрана под это нет и не нужно: ответ уже нарисован
      // полосой внизу страницы, просто ниже сгиба. Переход по `#how-it-works`
      // нельзя пускать до браузера: в этом приложении хеш -- это маршрут,
      // и неизвестный маршрут отправлял на главную, то есть ссылка не делала
      // ничего.
      event.preventDefault();
      const how = document.getElementById("how-it-works");
      if (!how) return;
      how.scrollIntoView({ behavior: "smooth", block: "center" });
      how.classList.add("is-highlighted");
      setTimeout(() => how.classList.remove("is-highlighted"), 1600);
    },
  });

  // Подпись обязана говорить правду о том, на чём подборка построена. У
  // человека без истории и без ответов настройки нет ни того, ни другого.
  let lead = "Персональная подборка программ на основе вашей должности и истории обучения";
  if (track.cold_start) {
    lead = "Подборка по вашей должности: мы пока с вами не знакомы, расскажите о себе — станет точнее";
  } else if (track.preferences_applied) {
    lead = "Подборка с учётом вашей должности, истории обучения и ваших ответов";
  }

  return el("div", { class: "page-head" }, [
    el("div", {}, [
      el("h1", { class: "page-title", text: "Рекомендовано для вас" }),
      el("p", { class: "page-lead", text: lead }),
    ]),
    explain,
  ]);
}

/** Ряд карточек с очередью за ним.
 *
 *  `replace(key)` -- карточку отметили (здесь или в панели курса): она
 *  плавно уходит, а на её место встаёт следующая из очереди. Когда очередь
 *  кончилась, место просто освобождается; когда пуст весь ряд -- зовётся
 *  `onEmpty`. */
function cardRow(items, { userId, onEmpty }) {
  const queue = items.slice(VISIBLE);
  const grid = el("div", { class: "card-grid" });
  const slots = new Map(); // course_key -> место в сетке

  const build = (item) =>
    recommendationCard(item, {
      userId,
      onOpen: openCourse,
      onMarked: (card) => replace(card.course_key),
    });

  function put(slot, item) {
    slots.set(item.course_key, slot);
    slot.replaceChildren(build(item));
  }

  for (const item of items.slice(0, VISIBLE)) {
    const slot = el("div", { class: "slot" });
    put(slot, item);
    grid.append(slot);
  }

  function replace(key) {
    const slot = slots.get(key);
    if (!slot) {
      // Карточка ещё не показана: просто убираем её из очереди.
      const index = queue.findIndex((item) => item.course_key === key);
      if (index >= 0) queue.splice(index, 1);
      return;
    }
    slots.delete(key);
    const next = queue.shift();
    const swap = () => {
      if (next) {
        put(slot, next);
        slot.firstElementChild.classList.add("is-entering");
        fitTitles(slot);
      } else {
        slot.remove();
        if (!grid.children.length) onEmpty();
      }
    };
    const old = slot.firstElementChild;
    if (!old || reducedMotion()) {
      swap();
      return;
    }
    old.classList.add("is-leaving");
    setTimeout(swap, LEAVE_MS);
  }

  return { node: grid, replace, size: () => items.length };
}

function allSorted(marked) {
  return el("section", { class: "empty-state is-compact" }, [
    el("span", { class: "empty-icon" }, [icon("check")]),
    el("h2", {
      class: "empty-title",
      text: marked ? "Вы разобрали всю подборку" : "В подборке пока нет программ",
    }),
    el("p", {
      class: "empty-text",
      text: marked
        ? "Всё, что вы отметили, лежит в разделе «Моё обучение». Новые программы " +
          "появятся в подборке, когда обновятся данные, а пока можно поискать самому."
        : "Загляните в каталог — там все программы Корпоративного университета.",
    }),
    el("div", { class: "empty-actions" }, [
      marked
        ? el("a", { class: "btn btn-primary", href: "#/my-learning", text: "Моё обучение" })
        : null,
      el("a", { class: "btn btn-secondary", href: "#/catalogue", text: "Каталог программ" }),
    ]),
  ]);
}

function electronicSection(items, userId) {
  // Отдельный ряд, а не часть подборки выше. Программа повышения
  // квалификации и электронный курс -- разные вещи: на первую записывают,
  // второй человек открывает и проходит сам. Общий список сравнивал бы
  // несравнимое, а без ряда 40% каталога не увидел бы никто: в маршрут эти
  // курсы не попадают ни одному из 323 профилей. Сюда попадают только курсы с
  // настоящим доводом (коллеги, близость к пройденному, интерес).
  if (!items.length) return null;

  const section = el("section", { class: "section", "aria-label": "Электронные курсы" });
  const row = cardRow(items, { userId, onEmpty: () => section.remove() });
  section.append(
    el("div", { class: "section-head section-head-row" }, [
      el("div", {}, [
        el("h2", { class: "section-title", text: "Электронные курсы" }),
        el("p", {
          class: "section-lead",
          text: "Подобраны лично вам. Открываются и проходятся самостоятельно, без записи.",
        }),
      ]),
      el("a", {
        class: "page-explain",
        href: "#/catalogue",
        text: "Все электронные курсы",
        onclick: () => presetCatalogue({ delivery: "Электронный курс" }),
      }),
    ]),
    row.node
  );
  return { node: section, row };
}

export async function renderHome(root) {
  const userId = session.userId();

  mount(
    root,
    el("div", { class: "page" }, [
      loadingNote("Подбираем программы… Если подключена языковая модель, это может занять до минуты."),
      el("div", { class: "card-grid" }, [cardSkeleton(), cardSkeleton(), cardSkeleton()]),
    ])
  );

  let track;
  try {
    track = await api.track(userId);
  } catch (error) {
    mount(
      root,
      el("div", { class: "page" }, [errorBox(error.message, () => renderHome(root))])
    );
    return;
  }

  if (!track.enough_data) {
    const { renderEmpty } = await import("./empty.js");
    return renderEmpty(root, track);
  }

  quotaStore.set(track.quota);

  const route = [...track.near_term, ...track.later];
  const programmes = el("section", { class: "recommended", "aria-label": "Программы" });
  const showAllSorted = () => programmes.replaceChildren(allSorted(track.marked_total > 0));
  let programmeRow = null;
  if (route.length) {
    programmeRow = cardRow(route, { userId, onEmpty: showAllSorted });
    programmes.append(programmeRow.node);
  } else {
    showAllSorted();
  }

  const electronic = electronicSection(track.electronic || [], userId);

  const page = el("div", { class: "page" }, [
    head(track),
    quotaBar(),
    programmes,
    track.preferences_applied || bannerDismissed ? null : wizardBanner(),
    electronic ? electronic.node : null,
    howItWorks(),
  ]);
  mount(root, page);
  fitTitles(page);

  // Отметка из панели курса: убираем карточку и здесь. Перерисовывать экран
  // целиком незачем -- это был бы лишний запрос к модели.
  onFeedbackChanged(page, ({ course_key: key, action }) => {
    if (!action) return;
    if (programmeRow) programmeRow.replace(key);
    if (electronic) electronic.row.replace(key);
  });
}
