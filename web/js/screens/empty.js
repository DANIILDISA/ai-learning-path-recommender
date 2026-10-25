// «Пока недостаточно данных для точной подборки» (FRAME 11 макета).
//
// Экран защитный. Замер на всех 323 профилях выгрузки: он не срабатывает ни
// разу -- каждый получает по десять рекомендаций, и профилей, у которых ни
// одна запись истории не связалась с каталогом, нет вообще. Мы всё равно его
// делаем: следующая выгрузка заказчика может выглядеть иначе, а экран,
// который честно говорит «данных мало», полезнее подборки, собранной из
// ничего и выданной за персональную.
//
// В инструкции администратора это сказано прямо, чтобы отсутствие экрана на
// демонстрации не сочли недоделкой.

import { api } from "../api.js";
import { el, icon, mount } from "../dom.js";
import { session } from "../session.js";

function known(label, value, missing = false) {
  return el("div", { class: "known-card" }, [
    el("div", { class: "known-label", text: label }),
    el("div", {
      class: missing ? "known-value is-missing" : "known-value",
      text: value,
    }),
  ]);
}

export async function renderEmpty(root, track) {
  const userId = session.userId();

  let profile = null;
  try {
    profile = await api.profile(userId);
  } catch {
    profile = null;
  }

  const resolved = profile
    ? profile.entries.filter((entry) => entry.in_catalogue).length
    : 0;

  mount(
    root,
    el("div", { class: "page" }, [
      el("h1", { class: "page-title", text: "Рекомендации для вас" }),
      el("section", { class: "empty-state" }, [
        el("span", { class: "empty-icon" }, [icon("tag")]),
        el("h2", {
          class: "empty-title",
          text: "Пока недостаточно данных для точной подборки",
        }),
        el("p", {
          class: "empty-text",
          text:
            "Мы пока не можем собрать полноценную персональную подборку. " +
            "Ответьте на несколько коротких вопросов — это поможет подобрать " +
            "программы с учётом ваших целей и интересов.",
        }),
        el("a", {
          class: "btn btn-primary btn-lg",
          href: "#/wizard/1",
          text: "Настроить рекомендации",
        }),
        el("span", { class: "banner-note" }, [icon("clock"), "Займёт около минуты"]),
      ]),
      el("h2", { class: "section-title section", text: "Что мы уже знаем" }),
      el("div", { class: "known-grid" }, [
        known("Должность", track.position),
        known("ИОГВ", track.iogv),
        known(
          "История обучения",
          resolved
            ? `${resolved} записей связано с каталогом`
            : "Недостаточно данных",
          !resolved
        ),
      ]),
      el("p", {
        class: "empty-footnote",
        text:
          "Чем больше информации появляется в КОРПУС, тем точнее становится " +
          "персональная подборка.",
      }),
    ])
  );
}
