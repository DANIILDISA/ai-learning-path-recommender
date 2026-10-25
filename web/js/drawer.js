// Панель курса: выезжает справа поверх страницы.
//
// В макете главное действие панели -- «Перейти к программе». Оно ведёт в
// никуда: СДО у продукта нет, ссылок на программы в данных нет, записать
// человека на обучение он не может. На её месте три действия из буклета
// заказчика: принять, отложить, отклонить. Кнопка, которая честно работает,
// лучше кнопки, которая выглядит главной и не делает ничего.

import { api } from "./api.js";
import { deliveryHint, STUDY_FORM_HINT, toast, markedText } from "./components.js";
import { el, icon } from "./dom.js";
import { quotaBar, quotaStore, waitMessage } from "./quota.js";
import { session } from "./session.js";

const ACTIONS = [
  { action: "want", title: "Хочу пройти" },
  { action: "later", title: "Не сейчас" },
  { action: "irrelevant", title: "Неактуально" },
];

let node = null;
let backdrop = null;
// Кто имел фокус до открытия панели: туда он возвращается при закрытии.
let opener = null;

function ensureMounted() {
  if (node) return;
  backdrop = el("div", { class: "drawer-backdrop", onclick: close });
  node = el("aside", {
    class: "drawer",
    role: "dialog",
    "aria-modal": "true",
    "aria-label": "Описание программы",
    // Закрытая панель остаётся в DOM (ради анимации), но для фокуса и
    // экранных дикторов её нет: иначе в ней живут «призрачные» кнопки.
    hidden: "hidden",
  });
  document.body.append(backdrop, node);

  // Escape закрывает панель. Модальное окно без выхода с клавиатуры --
  // ловушка для того, кто не работает мышью.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("drawer-open")) {
      close();
    }
  });
}

export function close() {
  document.body.classList.remove("drawer-open");
  if (node) {
    node.replaceChildren();
    node.setAttribute("hidden", "hidden");
  }
  if (opener && opener.isConnected) opener.focus({ preventScroll: true });
  opener = null;
}

function section(title, children) {
  if (!children || (Array.isArray(children) && !children.length)) return null;
  return el("section", { class: "drawer-section" }, [
    el("h3", { class: "drawer-section-title", text: title }),
    ...[].concat(children),
  ]);
}

function meta(course) {
  // Только то, что есть. У курсов реестра нет ни часов, ни формы обучения.
  const parts = [];
  if (course.delivery) {
    parts.push(el("span", { class: "chip", title: deliveryHint(course.delivery), text: course.delivery }));
  }
  if (course.hours) {
    parts.push(el("span", { class: "meta-item" }, [icon("clock"), `${course.hours} ч`]));
  }
  if (course.study_form) {
    parts.push(el("span", { class: "meta-item", title: STUDY_FORM_HINT, text: course.study_form }));
  }
  if (course.competency_group) {
    parts.push(el("span", { class: "meta-item" }, [icon("tag"), course.competency_group]));
  }
  return parts.length ? el("div", { class: "drawer-meta" }, parts) : null;
}

function actionRow(card) {
  const userId = session.userId();
  const row = el(
    "div",
    { class: "drawer-actions" },
    ACTIONS.map(({ action, title }) => {
      const button = el("button", {
        // Главной кнопкой (синей) становится только выбранная: раньше «Хочу
        // пройти» была синей всегда, и по цвету нельзя было понять, отмечена
        // ли программа.
        class: [
          "btn",
          card.feedback === action ? "btn-primary" : "btn-secondary",
          "btn-wizard",
          card.feedback === action ? "is-chosen" : null,
        ].filter(Boolean).join(" "),
        type: "button",
        "data-action": action,
        "aria-pressed": card.feedback === action ? "true" : "false",
        text: card.feedback === action ? `✓ ${title}` : title,
        onclick: async () => {
          try {
            // Повторный щелчок по выбранной кнопке снимает отметку: иначе
            // передумать можно было только выбрав другую. Снятие запас
            // отметок не тратит.
            const unmark = card.feedback === action;
            if (unmark) {
              await api.clearFeedback(userId, card.course_key);
            } else {
              const result = await api.feedback({
                user_id: userId,
                course_key: card.course_key,
                action,
              });
              quotaStore.set(result.quota);
              toast(markedText(action));
            }
            card.feedback = unmark ? null : action;
            row.replaceWith(actionRow(card));
            // Экран под панелью показывает ту же отметку: карточка уходит из
            // подборки, списки «Моего обучения» обновляются. Экран сам решает,
            // что именно перерисовать.
            window.dispatchEvent(
              new CustomEvent("feedback-changed", {
                detail: { course_key: card.course_key, action: card.feedback },
              })
            );
          } catch (error) {
            if (error.quota) {
              quotaStore.set(error.quota);
            } else {
              row.append(el("span", { class: "action-error", text: error.message }));
            }
          }
        },
      });
      return button;
    })
  );

  // Когда недельный запас кончился, новую отметку поставить нельзя, а снять
  // уже стоящую -- можно.
  const sync = (quota) => {
    const locked = Boolean(quota && quota.exhausted);
    row.querySelectorAll("button").forEach((button) => {
      const chosen = button.dataset.action === card.feedback;
      button.disabled = locked && !chosen;
      button.title = button.disabled ? waitMessage(quota) : "";
    });
  };
  quotaStore.subscribe(row, sync);
  return row;
}

function availabilityNotice(course) {
  if (course.state === "restricted") {
    return el("div", { class: "drawer-alert" }, [
      el("strong", { text: "Эта программа недоступна именно вам" }),
      el("p", { text: course.state_note || "" }),
      el("p", {}, [
        "Если вы относитесь к этой категории, отметьте это на пятом шаге ",
        el("a", { href: "#/wizard/5", text: "настройки рекомендаций" }),
        ".",
      ]),
    ]);
  }
  if (course.state === "passed") {
    return el("p", { class: "drawer-note", text: course.state_note || "Вы уже прошли эту программу." });
  }
  return null;
}

// Дат набора, мест и ссылок на запись в выгрузках заказчика нет. Выдумывать
// их нельзя, поэтому панель говорит прямо, кому адресовать вопрос.
function enrolmentNote(course) {
  const self = course.delivery === "Электронный курс";
  return section(
    "Как записаться и когда",
    el("p", {
      class: "drawer-note",
      text: self
        ? "Электронный курс проходится самостоятельно. Ссылки на курс в данных нет — " +
          "уточните, где его открыть, в Корпоративном университете (КУ)."
        : "Даты набора, места и порядок записи в данных не указаны — уточните в " +
          "Корпоративном университете (КУ).",
    })
  );
}

/** Открыть панель по карточке рекомендации (или по ключу курса). */
export async function openCourse(card) {
  ensureMounted();
  if (!document.body.classList.contains("drawer-open")) {
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }
  node.removeAttribute("hidden");
  document.body.classList.add("drawer-open");

  node.replaceChildren(
    el("div", { class: "drawer-head" }, [
      el("button", {
        class: "drawer-close",
        type: "button",
        "aria-label": "Закрыть",
        onclick: close,
        text: "×",
      }),
    ]),
    el("p", { class: "drawer-loading", text: "Загружаем описание…" })
  );

  let course;
  try {
    course = await api.course(card.course_key, session.userId());
  } catch (error) {
    node.append(el("p", { class: "error-text", text: error.message }));
    return;
  }

  // Что человек уже отметил, панель узнаёт у сервера: открытая из каталога или
  // из «Моего обучения», она получает от вызывающего только ключ курса.
  if (course.feedback !== undefined) card.feedback = course.feedback;
  if (course.quota) quotaStore.set(course.quota);

  const stepNote =
    card.position && card.position > 0
      ? el("p", {
          class: "drawer-step",
          text: `В вашем маршруте — шаг ${card.position}`,
        })
      : null;

  node.replaceChildren(
    el("div", { class: "drawer-head" }, [
      el("button", {
        class: "drawer-close",
        type: "button",
        "aria-label": "Закрыть",
        onclick: close,
        text: "×",
      }),
    ]),
    el("div", { class: "drawer-body" }, [
      el("h2", { class: "drawer-title", text: course.title }),
      meta(course),
      stepNote,

      availabilityNotice(course),

      section("О программе", course.annotation
        ? el("p", { class: "drawer-text", text: course.annotation })
        : null),

      section("Цель программы", course.goal
        ? el("p", { class: "drawer-text", text: course.goal })
        : null),

      // Дефект D1 реестра: у «Русского языка» в поле «Результаты» лежит текст
      // другого курса. Показать его -- соврать о том, чему человека научат,
      // поэтому вместо списка стоит объяснение, почему его нет.
      course.outcomes_withheld
        ? section("Что вы изучите", el("p", {
            class: "drawer-note",
            text: "Описание результатов в реестре заказчика относится к другой " +
                  "программе, поэтому мы его не показываем. Уточняется.",
          }))
        : section("Что вы изучите", course.outcome_points.length
            ? el("ul", { class: "drawer-list" },
                course.outcome_points.map((point) => el("li", { text: point })))
            : null),

      section("Кому адресована", course.audience
        ? el("p", { class: "drawer-text", text: course.audience })
        : null),

      // Одно предложение, то же, что под карточкой. Из каталога и из «Моего
      // обучения» панель открывается без него -- тогда раздела нет вовсе.
      section("Почему рекомендуем именно вам", card.reason
        ? el("p", { class: "drawer-text", text: card.reason })
        : null),

      enrolmentNote(course),
    ]),
    el("div", { class: "drawer-foot" }, [quotaBar(), actionRow(card)])
  );

  node.querySelector(".drawer-close").focus({ preventScroll: true });
}
