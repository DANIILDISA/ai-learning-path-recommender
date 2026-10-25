// Мастер настройки рекомендаций.
//
// Пять шагов, а не четыре, как в макете. Пятый спрашивает признаки допуска:
// возраст и кадровый резерв, ответственность за профразвитие, контроль и
// надзор, финансовая грамотность. Этих сведений нет в кадровой выгрузке и
// взять их неоткуда, а без них четыре программы каталога недоступны никому.
// Спросить у человека честнее, чем угадать по должности.
//
// Варианты ответов приходят из /api/preferences/options и здесь не зашиты:
// интерфейс -- заменяемый слой, а списки должны пережить его замену.

import { api } from "../api.js";
import { errorBox } from "../components.js";
import { el, icon, mount } from "../dom.js";
import { session } from "../session.js";

export const STEPS = 5;
const MAX_INTERESTS = 3;

// Черновик ответов, пока человек идёт по шагам. Сохраняется на бэкенд один
// раз в конце: пять запросов вместо одного ничего не дают, а промежуточное
// состояние на сервере пришлось бы как-то отличать от законченного.
let draft = null;
let options = null;

function emptyDraft(userId) {
  return {
    user_id: userId,
    goal: null,
    urgency: null,
    interests: [],
    picked_courses: [],
    attributes: [],
  };
}

async function ensureLoaded(userId) {
  if (!options) options = await api.options();
  if (!draft || draft.user_id !== userId) {
    // Если человек уже проходил мастер, показываем его прежние ответы:
    // «Изменить предпочтения» должно открывать заполненную форму, а не
    // пустую, иначе поправить один ответ значит ввести все пять заново.
    const saved = await api.preferences(userId);
    draft = { ...emptyDraft(userId), ...saved };
  }
}

function head(step) {
  return el("div", {}, [
    el("div", { class: "wizard-head" }, [
      el("span", { class: "wizard-head-icon" }, [icon("target")]),
      el("span", { class: "wizard-head-title", text: "Настройка рекомендаций" }),
      el("span", { class: "wizard-step", text: `Шаг ${step} из ${STEPS}` }),
    ]),
    el("div", { class: "progress", role: "progressbar",
      "aria-valuenow": String(step), "aria-valuemin": "1",
      "aria-valuemax": String(STEPS) }, [
      el("span", {
        class: "progress-fill",
        style: `width: ${(step / STEPS) * 100}%`,
      }),
    ]),
  ]);
}

function question(text, hint) {
  return el("div", { class: "wizard-question" }, [
    el("h2", { class: "wizard-title", text }),
    hint ? el("p", { class: "wizard-hint", text: hint }) : null,
  ]);
}

/** Плитка выбора. `checked` рисует галочку, `disabled` гасит плитку. */
function tile({ title, description, meta, checked, disabled, onclick }) {
  return el(
    "button",
    {
      class: [
        "tile",
        checked ? "is-checked" : null,
        disabled ? "is-disabled" : null,
      ]
        .filter(Boolean)
        .join(" "),
      type: "button",
      "aria-pressed": checked ? "true" : "false",
      disabled: disabled || null,
      onclick,
    },
    [
      checked ? el("span", { class: "tile-check" }, [icon("check")]) : null,
      el("span", { class: "tile-title", text: title }),
      description ? el("span", { class: "tile-desc", text: description }) : null,
      meta ? el("span", { class: "tile-meta", text: meta }) : null,
    ]
  );
}

function foot(step, { onBack, onNext, nextTitle, nextEnabled, onSkip }) {
  return el("div", { class: "wizard-foot" }, [
    step > 1
      ? el("button", {
          class: "btn btn-secondary btn-wizard",
          type: "button",
          text: "← Назад",
          onclick: onBack,
        })
      : el("a", {
          class: "btn btn-secondary btn-wizard",
          href: "#/home",
          text: "← На главную",
        }),
    onSkip
      ? el("button", {
          class: "btn btn-ghost",
          type: "button",
          text: "Пропустить",
          onclick: onSkip,
        })
      : null,
    el("button", {
      class: "btn btn-primary btn-wizard wizard-next",
      type: "button",
      text: nextTitle,
      disabled: nextEnabled ? null : true,
      title: nextEnabled ? null : "Выберите вариант, чтобы продолжить",
      onclick: onNext,
    }),
  ]);
}

/** Шаги 1 и 2: один вариант из четырёх. */
function singleChoice(step, { field, list, title, hint }, rerender) {
  return [
    question(title, hint),
    el(
      "div",
      { class: "tiles tiles-2" },
      list.map((option) =>
        tile({
          title: option.title,
          description: option.description,
          checked: draft[field] === option.key,
          onclick: () => {
            draft[field] = option.key;
            rerender();
          },
        })
      )
    ),
  ];
}

/** Шаг 3: до трёх направлений. */
function interestsStep(rerender) {
  const chosen = draft.interests;
  const full = chosen.length >= MAX_INTERESTS;
  return [
    question(
      "Какие профессиональные направления вам наиболее интересны?",
      "Выберите до трёх вариантов. Это поможет учитывать ваши интересы при подборе обучения."
    ),
    el(
      "div",
      { class: "tiles tiles-3" },
      // Порядок -- как отдал бэкенд. Он повторяет макет; сортировка на
      // клиенте однажды уже превратила список в перемешанный.
      options.interests.map((option) => {
        const checked = chosen.includes(option.key);
        return tile({
          title: option.title,
          checked,
          // Гасим, а не игнорируем клик: молчащая плитка выглядит сломанной,
          // и человек нажимает её ещё несколько раз, прежде чем поверить.
          disabled: full && !checked,
          onclick: () => {
            draft.interests = checked
              ? chosen.filter((key) => key !== option.key)
              : [...chosen, option.key];
            rerender();
          },
        });
      })
    ),
    el("p", {
      class: "wizard-count",
      text: full
        ? `Выбрано ${chosen.length} из ${MAX_INTERESTS}. Больше трёх выбрать нельзя — чтобы взять другое направление, снимите одно из выбранных.`
        : `Выбрано ${chosen.length} из ${MAX_INTERESTS}`,
    }),
  ];
}

/** Шаг 4: программы из пула, который собрал движок. */
function programmesStep(pool, rerender) {
  return [
    question(
      "Какие из этих программ вам было бы интересно изучить?",
      // Не «по вашим предыдущим ответам»: при первом прохождении мастера
      // ответы шагов 1--3 ещё не сохранены, и пул собран без них.
      "Программы отобраны по вашей должности, истории обучения и опыту коллег. Отметьте те, что вызывают наибольший интерес. Этот шаг необязательный: можно продолжить, ничего не отмечая."
    ),
    el(
      "div",
      { class: "tiles tiles-3" },
      pool.map((card) => {
        const checked = draft.picked_courses.includes(card.course_key);
        // «ППК · 24 часа» -- но только из того, что есть. У 64 курсов часов
        // нет вообще, и «· null часов» было бы неправдой о программе.
        const meta = [card.delivery, card.hours ? `${card.hours} ч` : null]
          .filter(Boolean)
          .join(" · ");
        return tile({
          title: card.title,
          meta,
          checked,
          onclick: () => {
            draft.picked_courses = checked
              ? draft.picked_courses.filter((key) => key !== card.course_key)
              : [...draft.picked_courses, card.course_key];
            rerender();
          },
        });
      })
    ),
  ];
}

/** Шаг 5: признаки допуска. */
function attributesStep(rerender) {
  return [
    question(
      "Отметьте, что относится к вам",
      "Эти признаки открывают доступ к отдельным программам обучения: в кадровой выгрузке таких сведений нет, и угадывать их мы не вправе. Ответы хранятся в базе приложения и нужны только для подбора; их видит администратор продукта. Если ничего не подходит — нажмите «Пропустить»."
    ),
    el(
      "div",
      { class: "attr-list" },
      options.attributes.map((option) => {
        const checked = draft.attributes.includes(option.key);
        return el(
          "button",
          {
            class: `attr-item ${checked ? "is-checked" : ""}`,
            type: "button",
            "aria-pressed": checked ? "true" : "false",
            onclick: () => {
              draft.attributes = checked
                ? draft.attributes.filter((key) => key !== option.key)
                : [...draft.attributes, option.key];
              rerender();
            },
          },
          [
            el("span", { class: "attr-box" }, [checked ? icon("check") : null]),
            el("span", { class: "attr-title", text: option.title }),
          ]
        );
      })
    ),
    el("p", {
      class: "wizard-note",
      text:
        "Отмеченное открывает дополнительные программы и ни на что больше " +
        "не влияет. Ответ можно изменить в любой момент.",
    }),
  ];
}

export async function renderWizard(root, step) {
  const userId = session.userId();
  const go = (next) => {
    location.hash = `#/wizard/${next}`;
  };

  mount(root, el("div", { class: "page" }, [el("div", { class: "wizard-card is-loading" })]));

  try {
    await ensureLoaded(userId);
  } catch (error) {
    mount(root, el("div", { class: "page" }, [
      errorBox(error.message, () => renderWizard(root, step)),
    ]));
    return;
  }

  let pool = [];
  if (step === 4) {
    try {
      pool = (await api.wizardPool(userId)).items;
    } catch (error) {
      mount(root, el("div", { class: "page" }, [
        errorBox(error.message, () => renderWizard(root, step)),
      ]));
      return;
    }
  }

  const rerender = () => renderWizard(root, step);

  let body;
  let nextEnabled = true;
  let nextTitle = "Продолжить →";
  let onNext = () => go(step + 1);
  let onSkip = null;

  if (step === 1) {
    body = singleChoice(step, {
      field: "goal",
      list: options.goals,
      title: "Как вы видите своё профессиональное развитие в ближайшие 2–3 года?",
      hint: "Выберите вариант, который лучше всего соответствует вашим планам. Ответ можно изменить позже.",
    }, rerender);
    nextEnabled = Boolean(draft.goal);
  } else if (step === 2) {
    // Заголовок по смыслу вариантов. В макете здесь стоял заголовок первого
    // шага при ответах про сроки -- дефект макета.
    body = singleChoice(step, {
      field: "urgency",
      list: options.urgency,
      title: "Выберите период для прохождения планового курса повышения квалификации.",
      hint: "Это влияет на то, какие программы окажутся в начале подборки.",
    }, rerender);
    nextEnabled = Boolean(draft.urgency);
  } else if (step === 3) {
    body = interestsStep(rerender);
  } else if (step === 4) {
    body = programmesStep(pool, rerender);
  } else {
    body = attributesStep(rerender);
    nextTitle = "Сохранить предпочтения";
    onSkip = () => save(root, userId, true);
    onNext = () => save(root, userId, false);
  }

  mount(
    root,
    el("div", { class: "page" }, [
      // Заголовок уже есть в шапке карточки («Настройка рекомендаций»);
      // второй такой же подряд оставлен только для программ чтения с экрана.
      el("h1", { class: "visually-hidden", text: "Настройка рекомендаций" }),
      el("div", { class: "wizard-card" }, [
        head(step),
        ...body,
        el("div", { class: "wizard-divider" }),
        foot(step, {
          onBack: () => go(step - 1),
          onNext,
          nextTitle,
          nextEnabled,
          onSkip,
        }),
        nextEnabled
          ? null
          : el("p", {
              class: "wizard-note",
              text: "Чтобы продолжить, выберите один из вариантов выше.",
            }),
      ]),
    ])
  );
}

async function save(root, userId, skipping) {
  const button = root.querySelector(".wizard-next");
  if (button) {
    button.disabled = true;
    button.textContent = "Сохраняем…";
  }
  try {
    // «Пропустить» сохраняет то, что уже выбрано на других шагах: человек
    // отказался от последнего вопроса, а не от всего мастера.
    await api.savePreferences(userId, draft);
    draft = null;
    location.hash = "#/done";
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = skipping ? "Пропустить" : "Сохранить предпочтения";
    }
    root.querySelector(".wizard-card").append(errorBox(error.message, null));
  }
}
