// «Рекомендации обновлены» -- экран подтверждения после мастера.
//
// Показывает не «спасибо», а то, что человек про себя рассказал: увидев свои
// ответы списком, он может заметить ошибку и вернуться. «Спасибо» без сводки
// оставляет его гадать, что именно записалось.

import { api } from "../api.js";
import { errorBox } from "../components.js";
import { el, icon, mount } from "../dom.js";
import { session } from "../session.js";

function chips(title, values) {
  if (!values.length) return null;
  return el("div", { class: "done-row" }, [
    el("span", { class: "done-label", text: title }),
    el(
      "div",
      { class: "badges" },
      values.map((value) => el("span", { class: "badge badge-accent", text: value }))
    ),
  ]);
}

export async function renderDone(root) {
  const userId = session.userId();

  let preferences;
  let options;
  try {
    [preferences, options] = await Promise.all([
      api.preferences(userId),
      api.options(),
    ]);
  } catch (error) {
    mount(root, el("div", { class: "page" }, [
      errorBox(error.message, () => renderDone(root)),
    ]));
    return;
  }

  const titleOf = (list, key) => {
    const found = list.find((option) => option.key === key);
    return found ? found.title : key;
  };

  const nothingChosen =
    !preferences.goal &&
    !preferences.urgency &&
    !preferences.interests.length &&
    !preferences.picked_courses.length &&
    !preferences.attributes.length;

  mount(
    root,
    el("div", { class: "page" }, [
      el("section", { class: "done" }, [
        el("span", { class: "done-icon" }, [icon("check")]),
        el("h1", {
          class: "done-title",
          text: nothingChosen ? "Ответы не записаны" : "Рекомендации обновлены",
        }),
        el("p", {
          class: "done-text",
          text: nothingChosen
            ? "Вы прошли мастер, ничего не выбрав. Подборка осталась прежней — она по-прежнему строится по вашей должности и истории обучения."
            : "Мы учли ваши ответы и пересобрали подборку. Она открыта на главной, а ближайшие шаги и всё отмеченное — в «Моём обучении».",
        }),

        nothingChosen
          ? null
          : el("div", { class: "done-summary" }, [
              preferences.goal
                ? chips("Цель", [titleOf(options.goals, preferences.goal)])
                : null,
              preferences.urgency
                ? chips("Сроки", [titleOf(options.urgency, preferences.urgency)])
                : null,
              chips(
                "Интересы",
                preferences.interests.map((key) => titleOf(options.interests, key))
              ),
              preferences.picked_courses.length
                ? chips("Отмечено программ", [
                    String(preferences.picked_courses.length),
                  ])
                : null,
              chips(
                "О себе",
                preferences.attributes.map((key) =>
                  titleOf(options.attributes, key)
                )
              ),
            ]),

        el("div", { class: "done-actions" }, [
          el("a", {
            class: "btn btn-primary btn-lg",
            href: "#/home",
            text: "Посмотреть рекомендации",
          }),
          el("a", {
            class: "btn btn-secondary btn-lg",
            href: "#/my-learning",
            text: "Моё обучение",
          }),
          el("a", {
            class: "btn btn-secondary btn-lg",
            href: "#/wizard/1",
            text: "Изменить предпочтения",
          }),
        ]),
      ]),
    ])
  );
}
