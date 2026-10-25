// «Моё обучение»: маршрут, прогресс и всё, что человек отметил.
//
// Это бывшие два экрана -- «Траектория обучения» и «Моё обучение». Наверху
// ближайшие четыре шага маршрута, левее прогресс по направлениям и профиль, ниже
// то, что человек решил про программы: «Хочу пройти», «Отложено», «Пройдено».
// Длинного списка «на год вперёд» здесь нет намеренно: шесть-семь программ
// подряд демотивируют, а ближайшие шаги честно показывают, с чего начать.
//
// «Неактуально» не смешивается с планами -- это отказ, а не отложенный план.
// Оно лежит отдельным блоком «Скрытые мной» с кнопкой «Вернуть»: иначе
// передумавший человек не нашёл бы программу, которую сам убрал.

import { api } from "../api.js";
import { errorBox, fitTitles, loadingNote, onFeedbackChanged, titleNode } from "../components.js";
import { el, icon, mount } from "../dom.js";
import { openCourse } from "../drawer.js";
import { session } from "../session.js";

// Сколько программ маршрута показывается крупными карточками со стрелками.
const STEPS_SHOWN = 4;

const STATUS_LABEL = {
  passed: "Пройден",
  not_passed: "Не завершён",
  in_progress: "В процессе",
};

function deliveryLabel(value) {
  if (!value || value === "не указан") return "формат не указан";
  return value;
}

const metaOf = (item) =>
  [item.delivery, item.hours ? `${item.hours} ч` : null].filter(Boolean).join(" · ");

// --- Ближайшие шаги --------------------------------------------------------

/** Крупная карточка шага маршрута. */
function stepCard(card) {
  const badge = (card.badges || []).find((b) => b.kind === "start_here" || b.kind === "high_match");
  return el(
    "article",
    {
      class: "step",
      // Требование ТЗ: при наведении курсора на пункт -- описание программы.
      title: card.annotation_short || card.title,
    },
    [
      // Место под бейдж занято всегда: без него названия в карточках с
      // бейджем и без него начинались бы на разной высоте.
      el("div", { class: "step-badge-slot" }, [
        badge
          ? el("span", {
              class: `badge ${badge.kind === "start_here" ? "badge-ok" : "badge-accent"}`,
              text: badge.text,
            })
          : null,
      ]),
      titleNode("h3", "step-title", card.title),
      el("p", { class: "step-meta", text: metaOf(card) }),
      el("button", {
        class: "btn btn-primary",
        type: "button",
        text: "Подробнее",
        onclick: () => openCourse(card),
      }),
    ]
  );
}

function stepsRow(cards) {
  if (!cards.length) {
    return el("p", {
      class: "learn-empty",
      text:
        "Ближайших шагов пока нет: подборка разобрана. Всё отмеченное — ниже, " +
        "остальные программы — в каталоге.",
    });
  }
  const nodes = [];
  cards.forEach((card, index) => {
    if (index > 0) nodes.push(el("span", { class: "step-arrow" }, [icon("arrow-right")]));
    nodes.push(stepCard(card));
  });
  return el("div", { class: "steps" }, nodes);
}

// --- Левая колонка: профиль и прогресс -------------------------------------

function progressRow(row) {
  const mine = row.passed + row.planned;
  const count = mine ? `${row.passed} из ${mine}` : `в линейке ${row.total}`;
  return el("div", { class: "progress-row" }, [
    el("div", { class: "progress-row-head" }, [
      el("span", { class: "progress-name", text: row.group }),
      el("span", {
        class: `progress-count${mine ? "" : " is-ref"}`,
        title: mine
          ? `Пройдено ${row.passed} из ${mine} программ этого направления в вашем плане`
          : `Программ в линейке по этому направлению: ${row.total}`,
        text: count,
      }),
    ]),
    mine
      ? el("div", { class: "progress progress-sm" }, [
          el("span", {
            class: "progress-fill",
            style: `width: ${(row.passed / mine) * 100}%`,
          }),
        ])
      : null,
  ]);
}

function progressPanel(progress) {
  // Показываем направления, где что-то пройдено или запланировано. Остальные --
  // в раскрывающемся списке справкой: «1 из 55» читалось бы как «осталось 54
  // курса», хотя человеку предлагают три.
  const active = progress.filter((row) => row.passed || row.planned);
  const rest = progress.filter((row) => !row.passed && !row.planned);

  return el("section", { class: "progress-panel" }, [
    el("h3", { class: "side-title", text: "Прогресс по направлениям" }),
    active.length
      ? el("div", { class: "progress-list" }, active.map(progressRow))
      : el("p", { class: "side-note", text: "Пока нет пройденных и запланированных программ из линейки." }),
    rest.length
      ? el("details", { class: "progress-rest" }, [
          el("summary", { text: `Остальные направления (${rest.length})` }),
          el("div", { class: "progress-list" }, rest.map(progressRow)),
        ])
      : null,
    // Одна ясная фраза вместо прежней «доли пройденного среди пройденного».
    // Панель считает по программам буклета: у 64 курсов реестра группы
    // компетенций нет, и молчаливое «97 из 161» выглядело бы потерянной
    // частью каталога.
    el("p", {
      class: "side-note",
      text:
        "Сколько программ по направлению вы уже прошли из тех, что есть в вашем " +
        "плане. Электронные курсы не учитываются.",
    }),
  ]);
}

function profileCard(summary) {
  const chips = (values) =>
    el("div", { class: "badges" },
      values.map((value) => el("span", { class: "badge badge-accent", text: value })));

  return el("aside", { class: "side-card" }, [
    el("h2", { class: "side-title", text: "Ваш профиль" }),

    el("div", { class: "side-field" }, [
      el("span", { class: "side-label", text: "Должность" }),
      el("span", { class: "side-value", text: summary.position }),
    ]),
    el("div", { class: "side-field" }, [
      el("span", {
        class: "side-label",
        title: "Исполнительный орган государственной власти",
        text: "ИОГВ",
      }),
      el("span", { class: "side-value", text: summary.iogv }),
    ]),

    summary.goal
      ? el("div", { class: "side-field" }, [
          el("span", { class: "side-label", text: "Цель" }),
          chips([summary.goal]),
        ])
      : null,
    summary.interests.length
      ? el("div", { class: "side-field" }, [
          el("span", { class: "side-label", text: "Интересы" }),
          chips(summary.interests),
        ])
      : null,

    el("div", { class: "side-counters" }, [
      el("div", {}, [
        el("span", { class: "side-label", text: "Пройдено" }),
        el("span", { class: "side-counter", text: String(summary.passed) }),
      ]),
      el("div", {}, [
        // «Не завершено», а не «В процессе» из макета: этого статуса
        // в выгрузке нет вообще, и счётчик всегда показывал бы ноль.
        el("span", { class: "side-label", text: "Не завершено" }),
        el("span", { class: "side-counter", text: String(summary.unfinished) }),
      ]),
    ]),

    el("a", {
      class: "btn btn-secondary",
      href: "#/wizard/1",
      text: "Изменить предпочтения",
    }),
  ]);
}

/** Причины порядка маршрута -- только те, что действительно сработали.
 *
 *  Каждая строка проверяется по данным: утверждение «учитываем цели и
 *  интересы» у человека, не проходившего настройку, подрывало доверие ко всему
 *  экрану. */
function whyLines(track) {
  const summary = track.profile_summary;
  const lines = [
    { ok: true, text: "Учитываем вашу должность и ИОГВ" },
    { ok: true, text: "Исключаем уже пройденные программы" },
  ];

  const personal = [];
  if (summary.goal) personal.push(`цель «${summary.goal}»`);
  if (summary.interests.length) personal.push(`интересы: ${summary.interests.join(", ")}`);
  if (personal.length) {
    lines.push({ ok: true, text: `Учитываем ваши ответы в настройке — ${personal.join("; ")}` });
  } else {
    lines.push({
      ok: false,
      text: "Ваши цели и интересы пока не заданы, поэтому не учитываются.",
      link: { href: "#/wizard/1", text: "Пройти короткий тест" },
    });
  }

  if (track.used_signals && track.used_signals.peers) {
    lines.push({ ok: true, text: "Используем образовательный опыт коллег на такой же должности" });
  } else {
    lines.push({
      ok: false,
      text: "Коллег на вашей должности для сравнения недостаточно — их опыт не использован",
    });
  }

  if (track.used_signals && track.used_signals.marks) {
    lines.push({ ok: true, text: "Учитываем ваши отметки: отмеченное уходит из подборки в разделы ниже" });
  }
  return lines;
}

function whyPanel(track) {
  return el("section", { class: "why-panel" }, [
    el("h2", { class: "section-title", text: "Почему именно такая последовательность?" }),
    el("ul", { class: "why-panel-list" },
      whyLines(track).map((line) =>
        el("li", { class: line.ok ? "" : "is-off" }, [
          icon(line.ok ? "check" : "clock"),
          el("span", { text: line.text }),
          line.link ? el("a", { href: line.link.href, text: line.link.text }) : null,
        ]))),
  ]);
}

// --- Что человек решил про программы ---------------------------------------

function historyRow(item) {
  const openable = item.in_catalogue;
  return el(
    openable ? "button" : "div",
    {
      class: `learn-row ${openable ? "" : "is-static"}`,
      type: openable ? "button" : null,
      onclick: openable ? () => openCourse({ course_key: item.course_key, position: 0 }) : null,
    },
    [
      el("span", { class: "learn-title" }, [
        item.title,
        // Одна программа, записанная в выгрузке несколько раз, показывается
        // одной строкой; «×3» говорит, сколько записей за ней стоит.
        item.count > 1
          ? el("span", {
              class: "learn-count",
              title: `В выгрузке записей по этой программе: ${item.count}`,
              text: `×${item.count}`,
            })
          : null,
      ]),
      el("span", { class: "learn-meta", text: deliveryLabel(item.delivery) }),
      el("span", {
        class: `learn-status is-${item.status}`,
        text: STATUS_LABEL[item.status] || item.status,
      }),
      // 25 названий из истории каталогу 2025 года неизвестны. Говорим об этом
      // прямо и не ведём в карточку, которой нет: браузер записал бы 404
      // в консоль заказчика, как бы аккуратно скрипт ни обработал ответ.
      openable
        ? null
        : el("span", { class: "learn-note", text: "Нет в каталоге программ" }),
    ]
  );
}

function markedRow(item) {
  return el(
    "button",
    {
      class: "learn-row",
      type: "button",
      onclick: () => openCourse({ course_key: item.course_key, position: 0 }),
    },
    [
      el("span", { class: "learn-title", text: item.title }),
      el("span", { class: "learn-meta", text: metaOf(item) }),
    ]
  );
}

function hiddenRow(item, userId, onRestore) {
  const button = el("button", {
    class: "btn btn-secondary",
    type: "button",
    text: "Вернуть",
    onclick: async () => {
      button.disabled = true;
      try {
        await api.clearFeedback(userId, item.course_key);
        onRestore();
      } catch (error) {
        button.disabled = false;
        row.append(el("span", { class: "action-error", text: error.message }));
      }
    },
  });
  const row = el("div", { class: "learn-row is-static" }, [
    el("span", { class: "learn-title", text: item.title }),
    el("span", { class: "learn-meta", text: metaOf(item) }),
    button,
  ]);
  return row;
}

function block(title, empty, rows, note = null) {
  return el("section", { class: "section" }, [
    el("h2", { class: "section-title", text: `${title}${rows.length ? ` · ${rows.length}` : ""}` }),
    note && rows.length ? el("p", { class: "section-lead", text: note }) : null,
    rows.length
      ? el("div", { class: "learn-list" }, rows)
      : el("p", { class: "learn-empty", text: empty }),
  ]);
}

function listsBlocks(data, userId, onRestore) {
  return [
    block(
      "Хочу пройти",
      "Пока пусто. Отметьте программу «Хочу пройти» в подборке — она появится здесь.",
      data.want.map(markedRow)
    ),
    block(
      "Отложено",
      "Пока пусто. Программы, отмеченные «Не сейчас», собираются здесь.",
      data.later.map(markedRow)
    ),
    block(
      "Пройдено",
      "В выгрузке нет записей о пройденном обучении.",
      data.passed.map(historyRow),
      // Дат и результатов в выгрузке заказчика нет -- не придумываем.
      "Даты прохождения и результаты в выгрузке не указаны — их можно уточнить в Корпоративном университете (КУ)."
    ),
    block(
      "Не завершено",
      "Незавершённых программ нет.",
      data.unfinished.map(historyRow)
    ),
    (data.hidden || []).length
      ? block(
          "Скрытые мной",
          "",
          data.hidden.map((item) => hiddenRow(item, userId, onRestore)),
          "Программы, которые вы отметили как неактуальные. «Вернуть» снимает отметку и возвращает программу в подборку."
        )
      : null,
  ];
}

export async function renderLearning(root) {
  const userId = session.userId();
  mount(root, el("div", { class: "page" }, [
    el("h1", { class: "page-title", text: "Моё обучение" }),
    loadingNote("Собираем ваш маршрут…"),
  ]));

  let track;
  let data;
  try {
    [track, data] = await Promise.all([api.track(userId), api.myLearning(userId)]);
  } catch (error) {
    mount(root, el("div", { class: "page" }, [
      errorBox(error.message, () => renderLearning(root)),
    ]));
    return;
  }

  // Очередь маршрута: ближайшие шаги берутся из её начала. Отметка в панели
  // курса убирает программу из очереди, и на её место встаёт следующая --
  // без нового запроса к модели.
  const queue = [...track.near_term, ...track.later];
  const stepsHost = el("div", { class: "steps-host" });
  const listsHost = el("div", { class: "lists-host" });

  const drawSteps = () => {
    stepsHost.replaceChildren(stepsRow(queue.slice(0, STEPS_SHOWN)));
    fitTitles(stepsHost);
  };
  const drawLists = () =>
    listsHost.replaceChildren(...listsBlocks(data, userId, () => renderLearning(root)).filter(Boolean));
  drawSteps();
  drawLists();

  const page = el("div", { class: "page" }, [
    el("div", { class: "page-head" }, [
      el("div", {}, [
        el("h1", { class: "page-title", text: "Моё обучение" }),
        el("p", {
          class: "page-lead",
          text: "Ближайшие шаги, ваш прогресс и всё, что вы отметили или уже прошли.",
        }),
      ]),
    ]),

    track.ai_note ? el("p", { class: "ai-note", text: track.ai_note }) : null,

    el("section", { class: "steps-section", "aria-label": "Ближайшие шаги" }, [
      el("h2", { class: "section-title", text: "Ближайшие шаги" }),
      stepsHost,
    ]),

    el("div", { class: "track-layout section" }, [
      el("div", { class: "track-side" }, [
        profileCard(track.profile_summary),
        progressPanel(track.progress),
      ]),
      el("div", { class: "track-main" }, [whyPanel(track), listsHost]),
    ]),
  ]);
  mount(root, page);
  fitTitles(stepsHost);

  // Отметка в панели курса. Поставлена -- убираем программу из ближайших шагов
  // и обновляем списки (это дешёвый запрос). Снята -- программа возвращается в
  // маршрут, и его проще загрузить заново.
  onFeedbackChanged(page, async ({ course_key: key, action }) => {
    if (!action) {
      renderLearning(root);
      return;
    }
    const index = queue.findIndex((card) => card.course_key === key);
    if (index >= 0) queue.splice(index, 1);
    drawSteps();
    try {
      data = await api.myLearning(userId);
      drawLists();
    } catch {
      /* списки обновятся при следующем открытии экрана */
    }
  });
}
