// Три помощника вместо фреймворка.
//
// Больше не нужно: экранов тринадцать, состояние держит бэкенд, а всякая
// абстракция сверх этого -- код, который придётся объяснять тому, кто будет
// сопровождать продукт у заказчика после нас.

/**
 * Создать элемент. `attrs.text` ставит текст, `attrs.class` -- класс,
 * ключ на `on...` вешает обработчик, остальное уходит в атрибуты.
 *
 * Значения `null`, `undefined` и `false` пропускаются: так условный атрибут
 * пишется выражением, а не веткой на три строки.
 *
 * Способа вставить разметку строкой здесь намеренно нет. Всё, что попадает
 * на экран, приходит из API -- названия программ, аннотации, обоснования, --
 * и должно оставаться текстом. Как только появляется `innerHTML`, содержимое
 * данных получает возможность стать разметкой, а данные эти мы получаем из
 * выгрузки заказчика и из ответа языковой модели.
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

/** Иконка из спрайта. Цвет и толщина наследуются от текста. */
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `icons.svg#${name}`);
  svg.append(use);
  return svg;
}

/**
 * Показать один экран целиком.
 *
 * `replaceChildren` снимает обработчики вместе с узлами; очистка строкой
 * оставила бы их висеть на удалённых элементах.
 */
export function mount(root, ...nodes) {
  root.replaceChildren(...nodes.filter(Boolean));
  return root;
}

/**
 * Русское числительное: plural(5, ["программа", "программы", "программ"]).
 *
 * Отдельная функция, потому что «Найдено 64 программ» на экране заказчика --
 * это то, за что продукт выглядит недоделанным, даже если всё остальное
 * безупречно.
 */
export function plural(count, [one, few, many]) {
  const mod100 = count % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = count % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}
