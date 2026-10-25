// Недельный запас отметок: пять «хочу пройти / не сейчас / неактуально» в неделю.
//
// Считает и хранит запас сервер (по профилю, таймер стартует с первой отметки
// недели). Здесь -- один общий слепок его последнего ответа, на который
// подписаны счётчик на экране, кнопки на карточках и панель курса: отметка в
// панели обязана погасить кнопки на карточках позади неё, и наоборот.
//
// Тон намеренно спокойный. Лимит -- не наказание, а просьба выбирать то, что
// действительно хочется пройти; когда запас кончился, человек читает не отказ,
// а «новые отметки откроются через ...».

import { el, icon } from "./dom.js";

let current = null;
const listeners = new Set();

export const quotaStore = {
  get: () => current,

  set(next) {
    if (!next) return;
    current = next;
    for (const listener of [...listeners]) listener(next);
  },

  /** Подписаться на изменения; узел-хозяин, ушедший со страницы, отписывается сам.
   *
   *  «Ушёл» значит «был на странице и пропал»: узел, который ещё не успели
   *  вставить, отписывать нельзя -- он просто опоздает на первую перерисовку. */
  subscribe(owner, listener) {
    let seen = false;
    const wrapped = (quota) => {
      if (owner) {
        if (owner.isConnected) seen = true;
        else if (seen) {
          listeners.delete(wrapped);
          return;
        }
      }
      listener(quota);
    };
    listeners.add(wrapped);
    if (current) listener(current);
    return () => listeners.delete(wrapped);
  },
};

/** «Новые отметки откроются через 5 дн. 3 ч». */
export function waitMessage(quota) {
  return quota && quota.resets_in_text
    ? `Новые отметки откроются через ${quota.resets_in_text}`
    : "Новые отметки скоро откроются";
}

/** Полоса «Осталось отметок на неделе: 3 из 5» -- или её замена, когда запас кончился. */
export function quotaBar() {
  const bar = el("div", { class: "quota", role: "status" });

  const draw = (quota) => {
    bar.classList.toggle("is-empty", quota.exhausted);
    if (quota.exhausted) {
      bar.replaceChildren(
        icon("clock"),
        el("span", {}, [
          el("strong", { text: "Отметки на этой неделе закончились. " }),
          waitMessage(quota) + " — а пока можно подробнее изучить подборку.",
        ])
      );
      bar.removeAttribute("aria-label");
      return;
    }
    const dots = [];
    for (let i = 0; i < quota.limit; i += 1) {
      dots.push(el("span", { class: `quota-dot${i < quota.left ? " is-left" : ""}` }));
    }
    bar.replaceChildren(
      el("span", { class: "quota-dots", "aria-hidden": "true" }, dots),
      el("span", { text: `Осталось отметок на неделе: ${quota.left} из ${quota.limit}` })
    );
  };

  quotaStore.subscribe(bar, draw);
  return bar;
}
