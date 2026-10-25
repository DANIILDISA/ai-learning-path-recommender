// Шапка: логотип, разделы, кто вошёл.
//
// Уведомлений из макета здесь нет. Источника данных под них не существует, а
// колокольчик с красной цифрой «3», которая никогда не изменится, обещает
// человеку события, которых не будет.

import { api } from "./api.js";
import { el, icon } from "./dom.js";
import { session } from "./session.js";

// «Моё обучение» -- бывшие «Траектория обучения» и «Моё обучение» в одном
// экране; старый адрес #/track перенаправляется на него (см. main.js).
const SECTIONS = [
  { hash: "#/home", title: "Главная" },
  { hash: "#/my-learning", title: "Моё обучение" },
  { hash: "#/catalogue", title: "Каталог программ" },
];

// «Данные» -- служебный экран методиста (загрузка выгрузок, откат версий).
// В общем меню слушателя ему не место: он открывается из отдельного пункта
// «Для методиста». Пароля нет и в этой поставке не будет, но и на виду он не
// лежит.
const SERVICE_HASH = "#/data";

const READY = new Set(["#/home", "#/my-learning", "#/catalogue"]);

// Оригинальный знак КОРПУС (синий круг и подпись «Корпоративный университет
// Санкт-Петербурга») лежит в web/ рядом с индексом: продукт ставится
// распаковкой и не ссылается на файлы соседних продуктов. Размеры заданы
// атрибутами (1248 x 378 пикселей исходника, ровно 3,3 : 1), чтобы шапка не
// прыгала, пока картинка грузится.
function brand() {
  return el("a", { class: "brand", href: "#/home", "aria-label": "КОРПУС — на главную" }, [
    el("img", {
      class: "brand-logo",
      src: "korpus-logo.png",
      alt: "КОРПУС — Корпоративный университет Санкт-Петербурга",
      width: "172",
      height: "52",
      decoding: "async",
    }),
  ]);
}

function nav() {
  // Активен ровно один раздел. В макете на экранах «Моя траектория» и
  // «Подробнее о курсе» подсвечены сразу два -- дефект макета.
  const current = location.hash || "#/home";
  return el(
    "nav",
    { class: "nav", "aria-label": "Разделы" },
    SECTIONS.map((section) => {
      const ready = READY.has(section.hash);
      return el("a", {
        onclick: (event) => {
          // Адрес не меняется -> hashchange не придёт, и экран остался бы
          // тем, что есть. Повторный клик по активному пункту -- перерисовка.
          if (ready && section.hash === (location.hash || "#/home")) {
            event.preventDefault();
            window.dispatchEvent(new Event("app-reroute"));
          }
        },
        class: [
          "nav-item",
          section.hash === current ? "is-active" : null,
          ready ? null : "is-disabled",
        ]
          .filter(Boolean)
          .join(" "),
        href: ready ? section.hash : null,
        title: ready ? null : "Раздел появится в следующей версии",
        "aria-disabled": ready ? null : "true",
        text: section.title,
      });
    })
  );
}

function serviceMenu() {
  const active = (location.hash || "") === SERVICE_HASH;
  const menu = el("details", { class: `service-menu${active ? " is-active" : ""}` }, [
    el("summary", { class: "service-summary", text: "Для методиста" }),
    el("div", { class: "service-pop" }, [
      el("a", {
        class: "service-link",
        href: SERVICE_HASH,
        onclick: () => menu.removeAttribute("open"),
      }, [
        el("span", { class: "service-link-title", text: "Данные" }),
        el("span", {
          class: "service-link-text",
          text: "Загрузка выгрузок и каталога, возврат прежней версии",
        }),
      ]),
    ]),
  ]);
  return menu;
}

// Закрывается щелчком мимо, как обычное меню. Один слушатель на страницу, а не
// по слушателю на каждую перерисовку шапки.
document.addEventListener("click", (event) => {
  document.querySelectorAll(".service-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});

// Шапка рисуется без ожидания экрана, поэтому две отрисовки подряд могут
// закончиться не в том порядке, в каком начались.
let headerEpoch = 0;

export async function renderHeader(root, { minimal, service = false }) {
  const mine = ++headerEpoch;
  if (minimal) {
    const back = el("a", { class: "nav-item", href: "#/home", text: "К подборке" });
    root.replaceChildren(
      el("div", { class: "header-inner" }, [brand(), service ? back : null, serviceMenu()])
    );
    return;
  }

  const userId = session.userId();
  let position = "";
  try {
    position = (await api.profile(userId)).position;
  } catch {
    // Шапка не должна ронять страницу из-за подписи под именем. Если профиль
    // не читается, экран ниже покажет настоящую ошибку сам.
    position = "";
  }
  if (mine !== headerEpoch) return;

  const who = el("div", { class: "who" }, [
    el("div", { class: "who-avatar" }, [icon("person")]),
    el("div", {}, [
      el("div", { class: "who-name", title: userId, text: userId }),
      // title -- длинную должность в шапке режет многоточие, полное название
      // читается по наведению.
      position
        ? el("div", { class: "who-position", title: position, text: position })
        : null,
    ]),
  ]);

  const change = el("button", {
    class: "btn btn-ghost",
    type: "button",
    text: "Сменить профиль",
    onclick: () => {
      session.clear();
      location.hash = "#/home";
      location.reload();
    },
  });

  root.replaceChildren(
    el("div", { class: "header-inner" }, [brand(), nav(), who, change, serviceMenu()])
  );
}
