// «Данные» -- служебный экран методиста: принять новый пакет выгрузок без
// правки кода и без перезапуска.
//
// Порядок: загрузить файлы -> посмотреть «что я распознал» и «что изменится»
// -> если что-то не так, выбрать колонку из списка -> применить. Прежние
// данные уходят в резерв, их можно вернуть одной кнопкой (с подтверждением).
//
// Экран открывается из пункта «Для методиста» в шапке: в общем меню слушателя
// ему не место.

import { api } from "../api.js";
import { errorBox } from "../components.js";
import { el, mount, plural } from "../dom.js";

const LEVEL_TEXT = {
  ok: "Замечаний нет",
  warn: "Есть предупреждения",
  error: "Данные не прочитаны",
};

const ROWS = ["строка", "строки", "строк"];
const COURSES = ["курс", "курса", "курсов"];
const PEOPLE = ["человек", "человека", "человек"];
const PROFILES = ["профиль", "профиля", "профилей"];

// Названия полей каталога по-русски: «tracks» методисту ничего не говорит.
const FIELD_RU = {
  hours: "часы",
  annotation: "аннотация",
  study_form: "форма обучения",
  audience: "категория слушателей",
  goal: "цель",
  outcomes: "результаты",
  navigator_tracks: "треки «Навигатора»",
  tracks: "треки «Навигатора»",
  competency_group: "группа компетенций",
  competency_subgroup: "подгруппа компетенций",
  title: "название",
};
const fieldName = (key) => FIELD_RU[key] || key;

const count = (n, forms) => `${n} ${plural(n, forms)}`;

function confidenceText(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function fmtDate(iso) {
  if (!iso) return "дата неизвестна";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString("ru-RU", { dateStyle: "long", timeStyle: "short" });
}

function summaryBlock(passport) {
  const history = passport.tables.find((t) => t.used && t.kind === "history");
  const registry = passport.tables.find((t) => t.used && t.kind === "registry");
  const where = (t) => `«${t.file}»${t.sheet ? `, лист «${t.sheet}»` : ""}`;
  const s = passport.summary || {};
  const rows = [];
  rows.push(
    el("li", {
      text: history
        ? `История обучения: ${where(history)} — ${count(history.data_rows, ROWS)}, ${count(
            s.people ?? 0,
            PEOPLE
          )}.`
        : "История обучения: не найдена.",
    })
  );
  rows.push(
    el("li", {
      text: registry
        ? `Реестр электронных курсов: ${where(registry)} — ${count(registry.data_rows, COURSES)}.`
        : "Реестр электронных курсов: не найден (приложение работает без него).",
    })
  );
  if (s.catalogue_courses !== undefined) {
    // Два числа в разных местах («Каталог: 161» и «Каталог программ: 99»)
    // сбивали: это части одного каталога, и здесь они разложены.
    const parts = [];
    if (s.registry_courses !== undefined) {
      parts.push(`электронных курсов из реестра — ${s.registry_courses}`);
      parts.push(`программ из буклета — ${s.booklet_programmes}`);
    }
    rows.push(
      el("li", {
        text:
          `Каталог: ${count(s.catalogue_courses, COURSES)}` +
          (parts.length
            ? ` (${parts.join(", ")}; часть курсов есть и в реестре, и в буклете, поэтому сумма больше)`
            : "") +
          ".",
      })
    );
    const missing = s.history_keys - s.linked_keys;
    rows.push(
      el("li", {
        text:
          `Названия курсов из истории: ${s.linked_keys} из ${s.history_keys} найдены в каталоге` +
          (missing > 0
            ? `; ${missing} в каталоге нет (это курсы, которых нет в каталоге программ, — список ниже).`
            : "."),
      })
    );
  }
  return el("ul", { class: "data-summary" }, rows);
}

/** Раскрывающийся «паспорт»: то, на что ссылаются предупреждения. */
function passportDetails(passport) {
  const blocks = [];
  const links = passport.fuzzy_links || [];
  if (links.length) {
    blocks.push(
      el("details", { class: "data-details", open: "open" }, [
        el("summary", { text: `Курсы, связанные с каталогом по сходству (${links.length})` }),
        el(
          "ul",
          {},
          links.map((l) =>
            el("li", { text: `«${l.from}» → «${l.to}» (сходство ${confidenceText(l.score)})` })
          )
        ),
      ])
    );
  }
  const unlinked = (passport.summary || {}).unlinked_titles || [];
  if (unlinked.length) {
    blocks.push(
      el("details", { class: "data-details" }, [
        el("summary", { text: `Названия из истории, которых нет в каталоге (${unlinked.length})` }),
        el("ul", {}, unlinked.map((t) => el("li", { text: t }))),
      ])
    );
  }
  return blocks.length ? el("div", { class: "data-passport" }, blocks) : null;
}

function messages(passport) {
  const nodes = [];
  for (const text of passport.errors || []) nodes.push(el("li", { class: "data-msg is-error", text }));
  for (const text of passport.warnings || []) nodes.push(el("li", { class: "data-msg is-warn", text }));
  const unknown = passport.unknown_values || {};
  for (const [group, values] of Object.entries(unknown)) {
    const top = Object.entries(values)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([raw, n]) => `«${raw}» ×${n}`)
      .join(", ");
    if (top) {
      nodes.push(
        el("li", {
          class: "data-msg is-warn",
          text: `Не распознаны значения (${group === "status" ? "статус" : "формат"}): ${top}.`,
        })
      );
    }
  }
  const homonyms = passport.homonym_cases || [];
  if (homonyms.length) {
    const items = homonyms.slice(0, 10).map((c) =>
      el("li", { text: `${c.name}: ${(c.iogvs || []).join(" / ")} (строк: ${c.rows})` })
    );
    nodes.push(
      el("li", { class: "data-msg is-warn" }, [
        el("div", { text: `Возможные однофамильцы / переводы — проверьте (${homonyms.length}). Без табельного номера одинаковое ФИО в разных ИОГВ считается двумя людьми.` }),
        el("ul", {}, items),
      ])
    );
  }
  const resolved = passport.position_resolution || [];
  if (resolved.length) {
    const items = resolved.slice(0, 15).map((r) =>
      el("li", {
        text: `«${r.position}» → ${r.grade} (${r.how === "keyword" ? `по слову «${r.matched}»` : "по умолчанию"}; чел.: ${r.people})`,
      })
    );
    nodes.push(
      el("li", { class: "data-msg is-warn" }, [
        el("div", { text: `Должности вне справочника, грейд определён автоматически (${resolved.length}):` }),
        el("ul", {}, items),
      ])
    );
  }
  return nodes.length ? el("ul", { class: "data-msgs" }, nodes) : null;
}

/** Таблица «роль → колонка» для одной используемой таблицы. */
function tableEditor(table, passport, onChange) {
  const roles = (passport.role_options[table.kind] || []).slice();
  const byRole = Object.fromEntries(table.columns.map((c) => [c.role, c]));
  const selects = {};

  const collect = () => {
    const cols = {};
    for (const [role, select] of Object.entries(selects)) {
      cols[role] = select.value === "" ? null : Number(select.value);
    }
    return cols;
  };

  const rows = roles.map((opt) => {
    const current = byRole[opt.role];
    const select = el(
      "select",
      {
        class: "data-select",
        "aria-label": opt.label,
        onchange: () => onChange(table, collect()),
      },
      [
        el("option", { value: "", text: opt.required ? "— не выбрано —" : "— нет такой колонки —" }),
        ...table.all_headers.map((h, i) => {
          const o = el("option", { value: String(i), text: `${i + 1}. ${h || "(без названия)"}` });
          if (current && current.index === i) o.selected = true;
          return o;
        }),
      ]
    );
    selects[opt.role] = select;
    const preview = current ? current.samples.join(" · ") : "";
    return el("tr", {}, [
      el("td", { class: "data-role", text: opt.label + (opt.required ? " *" : "") }),
      el("td", {}, [select]),
      el("td", {
        class: current && current.confidence < 0.6 ? "data-conf is-low" : "data-conf",
        text: current ? `${confidenceText(current.confidence)} · ${current.method_label}` : "—",
      }),
      el("td", { class: "data-sample", title: preview, text: preview }),
    ]);
  });

  return el("div", { class: "data-table-wrap" }, [
    el("table", { class: "data-table" }, [
      el("thead", {}, [
        el(
          "tr",
          {},
          ["Что это", "Колонка в файле", "Уверенность", "Примеры значений"].map((t) =>
            el("th", { text: t })
          )
        ),
      ]),
      el("tbody", {}, rows),
    ]),
  ]);
}

function tableCard(table, passport, state) {
  const title = `${table.kind_label}: «${table.file}»${table.sheet ? ` / «${table.sheet}»` : ""}`;
  const head = el("div", { class: "data-card-head" }, [
    el("h3", { class: "data-card-title", text: title }),
    el("span", {
      class: "data-card-meta",
      text: `${count(table.data_rows, ROWS)} · заголовок в строке ${table.header_row ?? "— (по содержимому)"}`,
    }),
  ]);
  return el("div", { class: "data-card" }, [
    head,
    tableEditor(table, passport, (t, columns) => state.edit(t, t.kind, columns)),
  ]);
}

function otherTables(passport, state) {
  const others = passport.tables.filter((t) => !t.used);
  if (!others.length) return null;
  return el("details", { class: "data-others", open: passport.errors?.length ? "open" : null }, [
    el("summary", { text: `Остальные листы и файлы (${others.length})` }),
    el(
      "ul",
      { class: "data-other-list" },
      others.map((t) => {
        const select = el(
          "select",
          {
            class: "data-select",
            "aria-label": "Как использовать лист",
            onchange: () => state.edit(t, select.value || "ignored", null),
          },
          [
            el("option", { value: "", text: "Не использовать" }),
            el("option", { value: "history", text: "Это история обучения" }),
            el("option", { value: "registry", text: "Это реестр электронных курсов" }),
          ]
        );
        return el("li", {}, [
          el("div", {
            class: "data-other-name",
            text: `«${t.file}»${t.sheet ? ` / «${t.sheet}»` : ""} — ${count(t.data_rows, ROWS)}`,
          }),
          el("div", { class: "data-other-reason", text: t.reason }),
          select,
        ]);
      })
    ),
  ]);
}

/** Выбор файлов в общем стиле: вместо серой системной кнопки -- наша. */
function dropZone({ title, hint, accept, multiple, onFiles }) {
  const input = el("input", {
    type: "file",
    ...(multiple ? { multiple: "multiple" } : {}),
    accept,
    class: "visually-hidden",
    onchange: () => {
      onFiles([...input.files]);
      input.value = "";
    },
  });
  const zone = el("label", { class: "data-drop" }, [
    el("span", { class: "data-drop-title", text: title }),
    el("span", { class: "data-drop-hint", text: hint }),
    el("span", { class: "btn btn-primary data-drop-btn", text: multiple ? "Выбрать файлы" : "Выбрать файл" }),
    input,
  ]);
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("is-over");
    onFiles([...e.dataTransfer.files]);
  });
  return zone;
}

/** Блок «Что изменится» -- как у буклета, но для истории обучения. */
function changeBlock(r, ack) {
  const v = r.preview;
  if (!v) return null;
  const nodes = [el("h3", { class: "data-subtitle", text: "Что изменится" })];

  nodes.push(
    el("p", {
      class: "data-files",
      text:
        `Было: ${count(v.old_people, PEOPLE)}, ${count(v.old_rows, ROWS)} истории. ` +
        `Станет: ${count(v.new_people, PEOPLE)}, ${count(v.new_rows, ROWS)}.`,
    })
  );

  if (v.gone_total > 0) {
    const names = v.gone_examples.map((g) => `${g.user_id} (${g.position})`).join("; ");
    nodes.push(
      el("div", { class: "data-msg is-warn" }, [
        el("div", {
          text:
            `Пропадут ${count(v.gone_total, PROFILES)}: людей, которые есть сейчас, нет в новой выгрузке. ` +
            `Сотрудник, у которого такой профиль выбран, увидит предложение выбрать себя заново.`,
        }),
        el("div", { class: "data-sample", title: names, text: `Например: ${names}` }),
        v.gone_with_marks
          ? el("div", {
              text:
                `У ${count(v.gone_with_marks, PEOPLE)} из них есть отметки на карточках или ответы настройки. ` +
                "Они останутся в базе, но станут недоступны, пока этих людей нет в данных (вернутся при откате).",
            })
          : null,
      ])
    );
  }
  if (v.kept_with_marks > 0) {
    nodes.push(
      el("p", {
        class: "data-files",
        text:
          `У ${count(v.kept_with_marks, PEOPLE)} есть отметки и ответы настройки: они привязаны к имени профиля ` +
          "(«Пользователь N» или ФИО) и сохранятся. Если в новой выгрузке то же имя означает другого человека, " +
          "отметки достанутся ему.",
      })
    );
  }
  if (v.delivery_from_catalog > 0) {
    nodes.push(
      el("p", {
        class: "data-files",
        text: `Формат (ППК / ЭК) у ${count(v.delivery_from_catalog, ROWS)} определён по каталогу программ.`,
      })
    );
  }
  if (v.unknown_delivery > 0) {
    nodes.push(
      el("div", { class: "data-msg is-warn" }, [
        el("strong", { text: `Формат обучения не указан у ${count(v.unknown_delivery, ROWS)}.` }),
        el("div", {
          text:
            "Эти записи не засчитываются как электронные курсы: в «Моём обучении» у них будет «формат не указан». " +
            "На то, что человек прошёл, и на подборку это не влияет. Добавьте в выгрузку колонку «Формат» (ППК / ЭК), " +
            "чтобы подписи были полными.",
        }),
      ])
    );
  }

  if (v.sample.length) {
    nodes.push(
      el("div", { class: "data-table-wrap" }, [
        el("table", { class: "data-table" }, [
          el("thead", {}, [
            el(
              "tr",
              {},
              ["Сотрудник", "Должность", "Курс", "Статус", "Формат"].map((t) => el("th", { text: t }))
            ),
          ]),
          el(
            "tbody",
            {},
            v.sample.map((row) =>
              el("tr", {}, [
                el("td", { text: row.user }),
                el("td", { class: "data-sample", title: row.position, text: row.position }),
                el("td", { class: "data-sample", title: row.title, text: row.title }),
                el("td", { text: row.status }),
                el("td", { text: row.delivery }),
              ])
            )
          ),
        ]),
      ])
    );
    nodes.push(
      el("p", { class: "data-hint", text: "Так будут выглядеть первые строки разных людей после разбора." })
    );
  }
  if (v.unused_columns.length) {
    nodes.push(
      el("p", {
        class: "data-hint",
        text:
          `Колонки, которые не используются: ${v.unused_columns.join(", ")}. ` +
          "Даты начала и окончания обучения в расчёте не участвуют.",
      })
    );
  }

  const needAck = v.gone_total > 0 || v.unknown_delivery > 0;
  if (needAck) {
    nodes.push(
      el("label", { class: "data-ai" }, [
        el("input", {
          type: "checkbox",
          ...(ack.value ? { checked: "checked" } : {}),
          onchange: (e) => {
            ack.value = e.target.checked;
            ack.redraw();
          },
        }),
        el("span", { text: " Я прочитал(а) это и понимаю последствия" }),
      ])
    );
  }
  return el("div", { class: "data-change" }, nodes);
}

export async function renderData(root) {
  const state = {
    uploadId: null,
    overrides: { tables: {}, use: {} },
    ai: false,
    result: null, // ответ анализа загрузки
    current: null, // паспорт рабочих данных
    busy: "",
    error: "",
    notice: "",
    skipped: [],
    ack: { value: false, redraw: () => draw() },
    confirm: null, // {kind: "data"|"catalog", ...} -- окно подтверждения отката
    bk: { current: null, report: null, ai: false, busy: "", error: "", notice: "" }, // каталог программ (буклет)
  };

  async function loadCurrent() {
    try {
      state.current = await api.dataPassport();
      state.error = "";
    } catch (e) {
      state.error = e.message;
    }
  }

  state.edit = async (table, kind, columns) => {
    const ov = { kind };
    if (columns) ov.columns = columns;
    state.overrides.tables[table.key] = ov;
    if (kind === "history" || kind === "registry") state.overrides.use[kind] = table.key;
    await remap("Пересчитываю…");
  };

  async function remap(busyText) {
    state.busy = busyText;
    draw();
    try {
      state.result = await api.dataMapping(state.uploadId, { ...state.overrides, ai: state.ai });
      state.error = "";
    } catch (e) {
      state.error = e.message;
    }
    state.ack.value = false;
    state.busy = "";
    draw();
  }

  async function handleFiles(files) {
    if (!files.length) return;
    state.busy = "Загружаю файлы…";
    state.notice = "";
    state.overrides = { tables: {}, use: {} };
    state.ack.value = false;
    draw();
    try {
      for (const file of files) {
        const res = await api.dataUploadFile(file, state.uploadId);
        state.uploadId = res.upload_id;
        for (const name of res.skipped || []) {
          if (!state.skipped.includes(name)) state.skipped.push(name);
        }
      }
      state.busy = "Разбираю таблицы…";
      draw();
      state.result = await api.dataUpload(state.uploadId);
      state.error = "";
    } catch (e) {
      state.error = e.message;
    }
    state.busy = "";
    draw();
  }

  async function apply() {
    state.busy = "Применяю данные…";
    draw();
    try {
      const res = await api.dataApply(state.uploadId);
      state.current = res;
      state.result = null;
      state.uploadId = null;
      state.skipped = [];
      state.ack.value = false;
      const a = res.aftermath || {};
      state.notice =
        `Новые данные применены: было ${count(a.people_before ?? 0, PEOPLE)}, стало ${count(a.people_after ?? 0, PEOPLE)}. ` +
        "Прежние данные сохранены — их можно вернуть." +
        (a.orphans_with_marks
          ? ` Отметки и ответы настройки ${count(a.orphans_with_marks, PEOPLE)} остались в базе, но в новых данных этих людей нет.`
          : "");
      state.error = "";
    } catch (e) {
      state.error = e.message;
    }
    state.busy = "";
    draw();
  }

  async function askRevert() {
    state.busy = "Смотрю, какая версия сохранена…";
    draw();
    try {
      const info = await api.dataRevertPreview();
      state.confirm = { kind: "data", info };
      state.error = "";
    } catch (e) {
      state.error = e.message;
    }
    state.busy = "";
    draw();
  }

  async function revert() {
    state.confirm = null;
    state.busy = "Возвращаю прежние данные…";
    draw();
    try {
      state.current = await api.dataRevert();
      const a = state.current.aftermath || {};
      state.notice =
        `Возвращена предыдущая версия данных: ${count(a.people_after ?? 0, PEOPLE)}.` +
        (a.orphans_with_marks
          ? ` Отметки и ответы настройки ${count(a.orphans_with_marks, PEOPLE)} относятся к людям, которых в этих данных нет.`
          : "");
      state.error = "";
    } catch (e) {
      state.error = e.message;
    }
    state.busy = "";
    draw();
  }

  async function discard() {
    try {
      await api.dataDiscard(state.uploadId);
    } catch {
      /* карантин и так чистится при применении */
    }
    state.uploadId = null;
    state.result = null;
    state.skipped = [];
    draw();
  }

  function confirmBox(title, lines, onYes, yesText) {
    return el("div", { class: "data-confirm", role: "alertdialog", "aria-label": title }, [
      el("strong", { text: title }),
      ...lines.map((text) => el("p", { text })),
      el("div", { class: "data-actions" }, [
        el("button", { class: "btn btn-primary", type: "button", text: yesText, onclick: onYes }),
        el("button", {
          class: "btn btn-ghost",
          type: "button",
          text: "Отмена",
          onclick: () => {
            state.confirm = null;
            draw();
          },
        }),
      ]),
    ]);
  }

  function currentBlock() {
    const p = state.current;
    if (!p) return el("p", { class: "learn-empty", text: "Загружаю сведения о данных…" });
    const backups = p.backups || [];
    const parts = [
      el("h2", { class: "section-title", text: "Данные, с которыми работает приложение" }),
      el("div", { class: `data-level is-${p.level}`, text: LEVEL_TEXT[p.level] || "" }),
      summaryBlock(p),
      messages(p),
      passportDetails(p),
    ];
    if (backups.length) {
      const last = backups[0];
      parts.push(
        el("p", {
          class: "data-hint",
          text: `Предыдущая версия: ${last.files.join(", ") || "файлы"}, сохранена ${fmtDate(last.created)}.`,
        })
      );
      if (state.confirm && state.confirm.kind === "data") {
        const i = state.confirm.info;
        parts.push(
          confirmBox(
            "Вернуть предыдущую версию данных?",
            [
              `Вернётся версия от ${fmtDate(i.created)}: ${i.files.join(", ") || "файлы"} — ${count(i.people, PEOPLE)}, ${count(i.rows, ROWS)}.`,
              `Сейчас в приложении ${count(i.current_people, PEOPLE)}, ${count(i.current_rows, ROWS)}. Текущая версия тоже сохранится, и откат можно будет отменить.`,
            ],
            revert,
            "Да, вернуть"
          )
        );
      } else {
        parts.push(
          el("button", {
            class: "btn btn-secondary",
            type: "button",
            text: "Вернуть предыдущую версию данных",
            disabled: state.busy ? "disabled" : null,
            onclick: askRevert,
          })
        );
      }
    }
    return el("section", { class: "section" }, parts);
  }

  function uploadBlock() {
    const r = state.result;
    const parts = [
      el("h2", { class: "section-title", text: "Загрузить новый пакет" }),
      dropZone({
        title: "Выберите файлы пакета или перетащите их сюда",
        hint: "История обучения и (по возможности) реестр электронных курсов. Excel .xlsx / .xls или CSV; названия файлов не важны.",
        accept: ".xlsx,.xlsm,.xls,.csv,.txt,.tsv",
        multiple: true,
        onFiles: handleFiles,
      }),
    ];
    if (state.skipped.length) {
      parts.push(
        el("p", {
          class: "data-hint",
          text:
            `Пропущены служебные файлы: ${state.skipped.join(", ")}. Это пометки Excel «файл открыт» — в данные они не входят.`,
        })
      );
    }
    if (r) {
      parts.push(
        el("p", {
          class: "data-files",
          text: `Загружено: ${r.files.map((f) => f.name).join(", ")}`,
        })
      );
      parts.push(el("div", { class: `data-level is-${r.level}`, text: `Что я распознал — ${LEVEL_TEXT[r.level] || ""}` }));
      parts.push(summaryBlock(r));
      for (const t of r.tables.filter((x) => x.used)) parts.push(tableCard(t, r, state));
      parts.push(otherTables(r, state));
      parts.push(messages(r));
      parts.push(passportDetails(r));
      parts.push(
        el("label", { class: "data-ai" }, [
          el("input", {
            type: "checkbox",
            ...(state.ai ? { checked: "checked" } : {}),
            onchange: (e) => {
              state.ai = e.target.checked;
              remap(state.ai ? "Спрашиваю ИИ, какие это колонки…" : "Пересчитываю без ИИ…");
            },
          }),
          el("span", { text: " Подсказать колонки с помощью ИИ (если ключ GigaChat настроен)" }),
        ])
      );
      if (r.ai) {
        parts.push(el("p", { class: "data-notice", role: "status", text: r.ai.message }));
      }
      parts.push(changeBlock(r, state.ack));
      const v = r.preview || {};
      const needAck = v.gone_total > 0 || v.unknown_delivery > 0;
      const canApply = r.can_apply && !state.busy && (!needAck || state.ack.value);
      parts.push(
        el("div", { class: "data-actions" }, [
          el("button", {
            class: "btn btn-primary",
            type: "button",
            text: "Применить эти данные",
            disabled: canApply ? null : "disabled",
            title: needAck && !state.ack.value ? "Сначала отметьте, что вы прочитали раздел «Что изменится»" : null,
            onclick: apply,
          }),
          el("button", { class: "btn btn-ghost", type: "button", text: "Отменить загрузку", onclick: discard }),
        ])
      );
    }
    return el("section", { class: "section" }, parts);
  }

  async function loadBooklet() {
    try {
      state.bk.current = await api.bookletCurrent();
    } catch (e) {
      state.bk.error = e.message;
    }
  }

  async function bkFile(files) {
    const file = files[0];
    if (!file) return;
    state.bk.busy = "Читаю файл… (PDF-буклет разбирается несколько секунд)";
    state.bk.error = "";
    state.bk.notice = "";
    draw();
    try {
      state.bk.report = await api.bookletUpload(file, state.bk.ai);
    } catch (e) {
      state.bk.report = null;
      state.bk.error = e.message;
    }
    state.bk.busy = "";
    draw();
  }

  async function bkApply() {
    state.bk.busy = "Применяю каталог…";
    draw();
    try {
      const res = await api.bookletApply(state.bk.report.upload_id, state.bk.ai);
      state.bk.current = res;
      state.bk.report = null;
      state.bk.notice = `Каталог применён: ${res.programmes} программ. Прежний сохранён — его можно вернуть.`;
      state.bk.error = "";
      loadCurrent().then(draw);
    } catch (e) {
      state.bk.error = e.message;
    }
    state.bk.busy = "";
    draw();
  }

  async function bkDiscard() {
    try {
      await api.bookletDiscard(state.bk.report.upload_id);
    } catch {
      /* карантин чистится сам */
    }
    state.bk.report = null;
    draw();
  }

  async function bkRevert() {
    state.confirm = null;
    state.bk.busy = "Возвращаю прежний каталог…";
    draw();
    try {
      state.bk.current = await api.bookletRevert();
      state.bk.notice = `Каталог возвращён. Сейчас используется: ${state.bk.current.using}.`;
      state.bk.error = "";
      loadCurrent().then(draw);
    } catch (e) {
      state.bk.error = e.message;
    }
    state.bk.busy = "";
    draw();
  }

  function bookletBlock() {
    const bk = state.bk;
    const cur = bk.current;
    const parts = [
      el("h2", { class: "section-title", text: "Каталог программ (буклет)" }),
      el("p", {
        class: "data-files",
        text: cur
          ? `Сейчас: ${cur.is_default ? "поставляемый каталог" : "загруженный каталог"}, программ: ${cur.programmes}` +
            (cur.source ? ` (источник: ${cur.source})` : "")
          : "Загружаю сведения о каталоге…",
      }),
    ];
    parts.push(
      dropZone({
        title: "Выберите новый буклет или каталог программ",
        hint: "PDF-буклет «Линейка образовательных программ» или таблица (Excel, CSV, Word): название, часы, форма, категория слушателей, описание. Сначала покажу, что изменится; применится только по кнопке.",
        accept: ".pdf,.xlsx,.xlsm,.xls,.csv,.tsv,.txt,.docx",
        multiple: false,
        onFiles: bkFile,
      })
    );
    parts.push(
      el("label", { class: "data-ai" }, [
        el("input", {
          type: "checkbox",
          ...(bk.ai ? { checked: "checked" } : {}),
          onchange: (e) => {
            bk.ai = e.target.checked;
          },
        }),
        el("span", { text: " Дозаполнять неясные поля с помощью ИИ (если ключ GigaChat настроен)" }),
      ])
    );
    if (bk.busy) parts.push(el("p", { class: "data-busy", text: bk.busy }));
    if (bk.notice) parts.push(el("p", { class: "data-notice", text: bk.notice }));
    if (bk.error) parts.push(errorBox(bk.error, null));
    const r = bk.report;
    if (r) {
      const d = r.diff;
      parts.push(el("h3", { class: "data-subtitle", text: `Что изменится — файл «${r.name || r.source}»` }));
      parts.push(
        el("p", {
          class: "data-files",
          text: `Было программ: ${d.old_count}, станет: ${d.new_count}. Без изменений: ${d.unchanged}, изменено: ${d.changed.length}, добавится: ${d.added.length}, пропадёт: ${d.removed.length}.`,
        })
      );
      const list = (title, items) =>
        items.length
          ? el("details", { class: "data-details" }, [
              el("summary", { text: `${title} (${items.length})` }),
              el("ul", {}, items.slice(0, 40).map((t) => el("li", { text: t }))),
            ])
          : null;
      parts.push(list("Добавятся", d.added));
      parts.push(list("Пропадут", d.removed));
      parts.push(list("Изменятся", d.changed.map((c) => `${c.title} — ${Object.keys(c.fields).map(fieldName).join(", ")}`)));
      if (r.warnings.length)
        parts.push(el("ul", { class: "data-messages" }, r.warnings.map((w) => el("li", { class: "is-warn", text: w }))));
      if (r.llm_calls) parts.push(el("p", { class: "data-files", text: `Обращений к ИИ: ${r.llm_calls}.` }));
      parts.push(
        el("div", { class: "data-actions" }, [
          el("button", {
            class: "btn btn-primary",
            type: "button",
            text: "Применить этот каталог",
            disabled: r.can_apply && !bk.busy ? null : "disabled",
            onclick: bkApply,
          }),
          el("button", { class: "btn btn-ghost", type: "button", text: "Отменить", onclick: bkDiscard }),
        ])
      );
    }
    const backups = (cur && cur.backups) || [];
    if (backups.length) {
      const last = backups[0];
      const what =
        (last.is_default ? "поставляемый каталог, " : "") +
        `версия от ${fmtDate(last.created)}` +
        (last.programmes != null ? `, программ: ${last.programmes}` : "") +
        (last.note ? ` (${last.note})` : "");
      if (state.confirm && state.confirm.kind === "catalog") {
        parts.push(
          confirmBox(
            "Вернуть предыдущий каталог программ?",
            [
              `Вернётся ${what}.`,
              `Сейчас используется каталог из ${cur.programmes} программ. Текущий каталог тоже сохранится.`,
            ],
            bkRevert,
            "Да, вернуть"
          )
        );
      } else {
        parts.push(el("p", { class: "data-hint", text: `Предыдущий каталог: ${what}.` }));
        parts.push(
          el("button", {
            class: "btn btn-secondary",
            type: "button",
            text: "Вернуть предыдущий каталог программ",
            disabled: bk.busy ? "disabled" : null,
            onclick: () => {
              state.confirm = { kind: "catalog" };
              draw();
            },
          })
        );
      }
    }
    return el("section", { class: "section" }, parts);
  }

  // Самый низ служебного экрана, мелко и бледно: кнопка для тестов, а не для
  // методиста. Обнуляет недельный лимит отметок у всех профилей разом, чтобы
  // показ и проверка не ждали семи дней. Сами отметки остаются.
  function testingFooter() {
    const status = el("span", { class: "test-reset-status", role: "status" });
    const button = el("button", {
      class: "test-reset",
      type: "button",
      text: "Сбросить недельный лимит (для тестов)",
      onclick: async () => {
        button.disabled = true;
        try {
          await api.resetMarks();
          status.textContent = "Готово: недельный лимит отметок сброшен у всех профилей.";
        } catch (e) {
          status.textContent = e.message;
        }
        button.disabled = false;
      },
    });
    return el("div", { class: "test-footer" }, [button, status]);
  }

  function draw() {
    mount(
      root,
      el("div", { class: "page page-service" }, [
        el("div", { class: "page-head" }, [
          el("div", {}, [
            el("span", { class: "service-tag", text: "Служебный раздел · для методиста" }),
            el("h1", { class: "page-title", text: "Данные" }),
            el("p", {
              class: "page-lead",
              text: "Загрузите новую выгрузку — приложение само поймёт, где что лежит, и покажет это для проверки.",
            }),
          ]),
        ]),
        state.busy ? el("p", { class: "data-busy", role: "status", text: state.busy }) : null,
        state.notice ? el("p", { class: "data-notice", role: "status", text: state.notice }) : null,
        state.error ? errorBox(state.error, null) : null,
        currentBlock(),
        uploadBlock(),
        bookletBlock(),
        testingFooter(),
      ])
    );
  }

  draw();
  await loadCurrent();
  await loadBooklet();
  draw();
}
