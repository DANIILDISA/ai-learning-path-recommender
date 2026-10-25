// Повторяющиеся куски интерфейса.

import { api } from "./api.js";
import { el, icon } from "./dom.js";
import { quotaStore, waitMessage } from "./quota.js";

// Какие бейджи попадают на карточку.
//
// Бэкенд отдаёт бейдж на каждый сработавший фактор. На карточке -- только
// короткие ярлыки; «почему рекомендовано» -- отдельной строкой-предложением.
const CARD_BADGES = {
  start_here: "badge-ok",
  high_match: "badge-accent",
  interest: "badge-accent",
};

const ACTIONS = [
  { action: "want", title: "Хочу пройти" },
  { action: "later", title: "Не сейчас" },
  { action: "irrelevant", title: "Неактуально" },
];

// Что человек читает в подтверждении: куда ушла карточка, которую он отметил.
const MARKED_TEXT = {
  want: "Добавили в «Хочу пройти» — программа ждёт в разделе «Моё обучение»",
  later: "Отложили — программа ждёт в разделе «Моё обучение»",
  irrelevant: "Скрыли из подборки — вернуть можно в разделе «Моё обучение»",
};

export function markedText(action) {
  return MARKED_TEXT[action] || "";
}

// Расшифровки для тех, кто видит «ППК» и «ИОГВ» впервые. Подсказка по наведению,
// а не сноска: она не занимает места на карточке.
const DELIVERY_HINTS = {
  "ППК": "Программа повышения квалификации: на неё записывают, обучение проходит по расписанию",
  "Электронный курс": "Электронный курс: открывается и проходится самостоятельно, без записи",
};

export function deliveryHint(name) {
  return DELIVERY_HINTS[name] || null;
}

export const STUDY_FORM_HINT =
  "ДОТ — дистанционные образовательные технологии: часть занятий проходит онлайн";

const PREPOSITION = /^(?:и|в|во|на|по|с|со|к|о|об|от|для|при|про|за|из|у|а|но|или|не)$/i;

// Существительные, которые без продолжения («в сфере ...», «в рамках ...») --
// обрыв фразы.
const HANGING = /^(?:сфере|сферы|области|рамках|целях|части|помощью|использованием|вопросам|вопросов)$/i;

// Окончания прилагательных. Короткие («-ий», «-ие») совпадают с существительными
// («решений», «обеспечение»), поэтому берутся только однозначные формы. Во
// множественном «-ых/-их/-ые» -- лишь у длинных слов: «государственных»,
// «гражданских», но не «данных» и «служащих».
const ADJECTIVE = /^\p{L}{4,}(?:ый|ой|ая|яя|ое|ее|ую|юю|ого|его|ому|ему|ым)$|^\p{L}{7,}(?:ые|ых|их|ыми)$/iu;

const TAIL = /[\s,;:(–—-]+$/;

function trimDangling(text) {
  const words = text.replace(TAIL, "").split(" ");
  while (
    words.length > 2 &&
    (PREPOSITION.test(words[words.length - 1]) ||
      HANGING.test(words[words.length - 1]) ||
      ADJECTIVE.test(words[words.length - 1]))
  ) {
    words.pop();
    words[words.length - 1] = words[words.length - 1].replace(TAIL, "");
  }
  return words.join(" ").replace(TAIL, "");
}

/** Название для карточки: целиком, если помещается, иначе по границе слова.
 *
 *  Нативное обрезание по строкам (`line-clamp`) режет последнюю строку по
 *  символам и может оборвать слово посередине, а сколько знаков влезает в три
 *  строки, зависит от ширины карточки. Поэтому название режется после
 *  вставки на страницу (`fitTitles`) -- по слову, без висящего предлога на
 *  конце. Полное название остаётся в `title` и в панели курса. */
export function titleNode(tag, className, title) {
  return el(tag, { class: className, title, "data-full-title": title, text: title });
}

/** Подогнать названия внутри `root` под три строки, обрезая по границе слова.
 *
 *  Вызывается после того, как карточки вставлены в страницу: до этого
 *  измерять нечего. Узел помещается, пока его содержимое не выше видимой
 *  части (CSS ограничивает её тремя строками). */
export function fitTitles(root = document) {
  root.querySelectorAll("[data-full-title]").forEach((node) => {
    const full = node.dataset.fullTitle;
    node.textContent = full;
    const overflows = () => node.scrollHeight > node.clientHeight + 1;
    if (!overflows()) return;
    const words = full.split(" ");
    let low = 1;
    let high = words.length - 1;
    let best = 1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      node.textContent = `${words.slice(0, mid).join(" ")}…`;
      if (overflows()) high = mid - 1;
      else {
        best = mid;
        low = mid + 1;
      }
    }
    node.textContent = `${trimDangling(words.slice(0, best).join(" "))}…`;
  });
}

// Размеры букв меняются, когда догружается шрифт, и когда окно меняет ширину:
// подгонка названий повторяется.
let fitTimer = null;
const refit = () => {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => fitTitles(), 120);
};
window.addEventListener("resize", refit);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(refit);

// --- Подтверждение действия ---------------------------------------------

let toastNode = null;
let toastTimer = null;

/** Короткое сообщение внизу окна: подтверждает действие и сразу исчезает. */
export function toast(text) {
  if (!text) return;
  if (!toastNode) {
    toastNode = el("div", { class: "toast", role: "status", "aria-live": "polite" });
    document.body.append(toastNode);
  }
  toastNode.textContent = text;
  toastNode.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastNode.classList.remove("is-visible"), 3800);
}

/** Отметка поставлена в панели курса: экран позади панели узнаёт об этом здесь.
 *
 *  Подписка живёт, пока жив `owner`. Раньше главная подписывалась на событие
 *  один раз и не отписывалась, -- и отметка, поставленная уже на другом экране,
 *  подменяла его главной. */
export function onFeedbackChanged(owner, handler) {
  const listener = (event) => {
    if (!owner.isConnected) {
      window.removeEventListener("feedback-changed", listener);
      return;
    }
    handler(event.detail || {});
  };
  window.addEventListener("feedback-changed", listener);
}

// --- Карточка рекомендации -------------------------------------------------

function badges(card) {
  const shown = (card.badges || []).filter((badge) => badge.kind in CARD_BADGES);
  if (!shown.length) return null;
  return el(
    "div",
    { class: "badges" },
    shown.map((badge) =>
      el("span", { class: `badge ${CARD_BADGES[badge.kind]}`, text: badge.text })
    )
  );
}

function meta(card) {
  // Отсутствующие поля не рисуются вовсе. У 64 курсов из 161 нет ни часов, ни
  // формы обучения, ни направления -- прочерк или «0 часов» были бы неправдой
  // о программе заказчика, а пустой разделитель выглядел бы поломкой.
  const parts = [];
  if (card.hours) {
    parts.push(el("span", { class: "meta-item" }, [icon("clock"), `${card.hours} ч`]));
  }
  if (card.competency_group) {
    parts.push(
      el("span", { class: "meta-item" }, [icon("tag"), card.competency_group])
    );
  }
  return parts.length ? el("div", { class: "meta" }, parts) : null;
}

/** Метка формата в верхней строке карточки.
 *
 *  Электронный курс подписан тем, что для человека важнее всего: его можно
 *  начать сразу, без записи. */
function formatLabel(card) {
  if (card.self_paced) {
    return el(
      "span",
      { class: "chip chip-ek", title: deliveryHint("Электронный курс") },
      [icon("spark"), "Электронный курс"]
    );
  }
  return card.delivery
    ? el("span", { class: "chip", title: deliveryHint(card.delivery), text: card.delivery })
    : null;
}

/** «Почему рекомендовано» -- одно предложение, всегда на виду.
 *
 *  Раньше это был раскрывающийся список из трёх строк движка, которые читались
 *  как служебный журнал. Теперь -- одна фраза про этого человека и эту
 *  программу (`engine.reasons` на бэкенде). */
function reasonBlock(card) {
  if (!card.reason) return null;
  return el("div", { class: "card-reason" }, [
    el("span", { class: "card-reason-label", text: "Почему рекомендовано" }),
    el("p", { class: "card-reason-text", text: card.reason }),
  ]);
}

function actionRow(card, userId, onMarked) {
  // Три действия из буклета заказчика. Они и есть то, ради чего продукт
  // существует: программу можно принять, отложить или отклонить, и ничего
  // не происходит без решения самого человека. Отмеченная карточка уходит из
  // подборки сразу -- на её место встаёт следующая.
  let busy = false;
  const note = el("span", { class: "action-error", hidden: "hidden" });

  const buttons = ACTIONS.map(({ action, title }) =>
    el("button", {
      class: "action",
      type: "button",
      "data-action": action,
      text: title,
      onclick: async () => {
        if (busy) return;
        busy = true;
        sync();
        note.hidden = true;
        try {
          const result = await api.feedback({
            user_id: userId,
            course_key: card.course_key,
            action,
          });
          quotaStore.set(result.quota);
          toast(markedText(action));
          onMarked(card, action);
        } catch (error) {
          if (error.quota) {
            // Запас кончился, пока экран был открыт: гасим кнопки и пишем
            // про срок, а не про ошибку.
            quotaStore.set(error.quota);
          } else {
            note.textContent = error.message;
            note.hidden = false;
          }
        } finally {
          busy = false;
          sync();
        }
      },
    })
  );

  const row = el("div", { class: "actions" }, [...buttons, note]);

  function sync(quota = quotaStore.get()) {
    const locked = Boolean(quota && quota.exhausted);
    for (const button of buttons) {
      button.disabled = locked || busy;
      button.title = locked ? waitMessage(quota) : "";
    }
  }

  quotaStore.subscribe(row, sync);
  return row;
}

/** Карточка рекомендации: формат, название, описание, метаданные, бейджи,
 *  «почему рекомендовано», «Подробнее» и три действия.
 *
 *  `onMarked(card, action)` вызывается, когда отметка принята сервером: экран
 *  решает, что поставить на место карточки. */
export function recommendationCard(card, { userId, onOpen, onMarked }) {
  return el("article", { class: "card", "data-course": card.course_key }, [
    formatLabel(card),
    // title -- чтобы длинное название реестра можно было прочитать целиком,
    // не открывая карточку: на экране оно сокращено по границе слова.
    titleNode("h3", "card-title", card.title),
    card.annotation_short
      ? el("p", {
          class: "card-annotation",
          title: card.annotation_short,
          text: card.annotation_short,
        })
      : null,
    meta(card),
    badges(card),
    reasonBlock(card),
    el("div", { class: "card-foot" }, [
      onOpen
        ? el("button", {
            class: "btn btn-primary",
            type: "button",
            text: "Подробнее",
            onclick: () => onOpen(card),
          })
        : null,
    ]),
    actionRow(card, userId, onMarked),
  ]);
}

/** Заметка «Загружаем…» на время, пока экран ждёт ответа сервера. */
export function loadingNote(text = "Загружаем…") {
  return el("p", { class: "loading-note", role: "status", text });
}

/** Заглушка на время загрузки. Скелетон, а не спиннер: он говорит «здесь
 *  будет карточка», а не «что-то происходит», и переход к содержимому не
 *  дёргает вёрстку. */
export function cardSkeleton() {
  return el("div", { class: "card card-skeleton" }, [
    el("span", { class: "skeleton skeleton-chip" }),
    el("span", { class: "skeleton skeleton-title" }),
    el("span", { class: "skeleton skeleton-line" }),
    el("span", { class: "skeleton skeleton-line short" }),
  ]);
}

/** Сообщение об ошибке с кнопкой «Повторить».
 *  Пустой экран без объяснения человек читает как поломку продукта. */
export function errorBox(message, onRetry) {
  return el("div", { class: "error-box" }, [
    el("p", { class: "error-text", text: message }),
    onRetry
      ? el("button", {
          class: "btn btn-secondary",
          type: "button",
          text: "Повторить",
          onclick: onRetry,
        })
      : null,
  ]);
}
