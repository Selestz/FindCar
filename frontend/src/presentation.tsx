import { Icon } from "./components/Icon";
import { Fragment, useState } from "react";
export const sourceNames: Record<string, string> = {
  auto_ru: "Auto.ru",
  drom: "Drom",
};
export const listingCount = (n: number) =>
  `${n} ${n % 100 >= 11 && n % 100 <= 14 ? "объявлений" : n % 10 === 1 ? "объявление" : n % 10 >= 2 && n % 10 <= 4 ? "объявления" : "объявлений"}`;
export const labels: Record<string, string> = {
  porsche: "Porsche",
  bmw: "BMW",
  panamera: "Panamera",
  "3-series": "3 series",
  make: "марка",
  model: "модель",
  generation: "поколение",
  body_type: "кузов",
  engine_type: "тип двигателя",
  year: "год",
  body_types: "кузов",
  region: "регион",
  drive_type: "привод",
  steering_wheel: "руль",
  transmission: "коробка",
  ACTIVE: "Активно",
  REMOVED: "Снято",
  UNKNOWN: "Статус неизвестен",
  RELISTED: "Вероятно перевыставлено",
  all: "Полный",
  front: "Передний",
  rear: "Задний",
  left: "Левый",
  right: "Правый",
  petrol: "Бензин",
  diesel: "Дизель",
  hybrid: "Гибрид",
  electric: "Электро",
  gas: "Газ",
  other: "Другой",
  automatic: "Автомат",
  manual: "Механика",
  robot: "Робот",
  cvt: "Вариатор",
  sedan: "Седан",
  hatchback: "Хэтчбек",
  liftback: "Лифтбек",
  wagon: "Универсал",
  suv: "Внедорожник",
  coupe: "Купе",
  convertible: "Кабриолет",
  pickup: "Пикап",
  minivan: "Минивэн",
  van: "Фургон",
  black: "Чёрный",
  silver: "Серебристый",
  white: "Белый",
  blue: "Синий",
  red: "Красный",
  OK: "Доступен",
  NOT_CHECKED: "Ещё не проверен",
  ACCESS_NOT_CONFIGURED: "Не подключён",
  SOURCE_UNAVAILABLE: "Источник недоступен",
  RATE_LIMITED: "Временное ограничение запросов",
  AUTH_REQUIRED: "Нужен вход в источник",
  PARSER_ERROR: "Не удалось прочитать ответ",
  SEARCH_SCOPE_REQUIRED: "Укажите марку автомобиля",
  UNSUPPORTED_REGION:
    "Источник не поддерживает этот регион. Выберите Москву или оставьте регион пустым",
  UNSUPPORTED_FILTER: "Источник не поддерживает выбранное значение фильтра",
  RESPONSE_TOO_LARGE: "Ответ источника превышает допустимый размер",
  RETRY_EXHAUSTED: "Обновление не завершено",
  NEW_LISTING: "Новое объявление",
  PRICE_DROP: "Цена снизилась",
  PRICE_INCREASE: "Цена выросла",
  LISTING_REMOVED: "Объявление снято",
  LISTING_RETURNED: "Снова в продаже",
  PROBABLE_RELIST: "Вероятно перевыставлено",
  POSSIBLE_DUPLICATE: "Возможный дубль",
  owners_max: "число владельцев",
  price_to: "цена",
  price_from: "цена",
  mileage_to: "пробег",
  mileage_from: "пробег",
  year_from: "год",
  year_to: "год",
  engine_volume_from: "объём двигателя",
  engine_volume_to: "объём двигателя",
  power_from: "мощность",
  power_to: "мощность",
  radius_km: "радиус поиска",
};
export const display = (value: string | number | null | undefined) =>
  value == null ? "Не указано" : labels[String(value)] || String(value);
export const date = (value?: string | null) =>
  value
    ? new Date(value).toLocaleString("ru-RU", {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Ещё не проверено";
const flags =
  /(?:расход масла|есть окрасы|не на ходу|после дтп|требует ремонта|проблемы с коробкой)/giu;
export function Description({
  text,
  compact = false,
}: {
  text: string | null;
  compact?: boolean;
}) {
  const value = text || "Описание не указано";
  const matches = [...value.matchAll(flags)];
  if (compact)
    return matches.length ? (
      <p className="text-flags">
        В описании:{" "}
        {matches.map((m, i) => (
          <Fragment key={i}>
            {i > 0 && " · "}
            <mark>«{m[0]}»</mark>
          </Fragment>
        ))}
      </p>
    ) : null;
  const pieces = [];
  let cursor = 0;
  for (const m of matches) {
    pieces.push(
      value.slice(cursor, m.index),
      <mark key={m.index}>{m[0]}</mark>,
    );
    cursor = m.index! + m[0].length;
  }
  pieces.push(value.slice(cursor));
  return <p className="seller-description">{pieces}</p>;
}
export function CarPreview({
  title,
  image,
}: {
  title: string;
  source?: string;
  image?: string;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  if (image && failed !== image)
    return (
      <div className="car-preview photograph">
        <img
          src={image}
          alt={title}
          loading="lazy"
          onError={() => setFailed(image)}
        />
      </div>
    );
  return (
    <div className="car-preview no-photo" aria-label="Фотография недоступна">
      <Icon name="image" size={32} />
      <span>Нет фотографии</span>
    </div>
  );
}
