import { useState } from "react";
import { api, money, type Listing, type Results as ResultData } from "../api";
import {
  CarPreview,
  date,
  Description,
  display,
  sourceNames,
} from "../presentation";
import { Icon } from "./Icon";
export function Results({
  data,
  loading,
  onMore,
  onChanged,
  onOpen,
}: {
  data: ResultData | null;
  loading: boolean;
  onMore: () => void;
  onChanged: () => void;
  onOpen: (id: string) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [override, setOverride] = useState(false);
  const chosen =
    data?.items.filter((row) => selected.includes(row.cluster_id)) ?? [];
  async function state(car: Listing, values: object) {
    setBusy(true);
    setError("");
    try {
      await api(`/vehicles/${car.cluster_id}/state`, {
        method: "PATCH",
        body: JSON.stringify({ version: car.cluster_version, ...values }),
      });
      onChanged();
    } catch (e) {
      setError((e as Error).message);
      onChanged();
    } finally {
      setBusy(false);
    }
  }
  async function merge() {
    setBusy(true);
    setError("");
    try {
      await api("/vehicles/merge", {
        method: "POST",
        body: JSON.stringify({
          versions: Object.fromEntries(
            chosen.map((r) => [r.cluster_id, r.cluster_version]),
          ),
          supersede_rejections: override,
        }),
      });
      setSelected([]);
      setOverride(false);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
      onChanged();
    } finally {
      setBusy(false);
    }
  }
  if (!data)
    return (
      <div className="empty-state" role="status">
        <Icon name="filter" size={28} />
        <h2>
          {loading
            ? "Ищем подходящие автомобили…"
            : "Ваш следующий автомобиль — здесь"}
        </h2>
        <p>
          {loading
            ? "Объявления появятся по мере проверки площадок."
            : "Выберите марку, модель и бюджет. Мы соберём объявления с разных площадок и объединим повторяющиеся."}
        </p>
      </div>
    );
  return (
    <>
      {loading && (
        <p className="small muted" role="status">
          Обновляем список…
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {selected.length > 0 && (
        <div className="merge-selection">
          <span>Выбрано: {chosen.length}</span>
          <button
            disabled={busy || chosen.length < 2 || chosen.length > 10}
            onClick={() => void merge()}
          >
            Объединить выбранные
          </button>
          <button
            className="text-button"
            onClick={() => {
              setSelected([]);
              setOverride(false);
            }}
          >
            Отменить выбор
          </button>
          <label className="checkbox small">
            <input
              type="checkbox"
              checked={override}
              onChange={(e) => setOverride(e.target.checked)}
            />
            Заменить предыдущие решения «разные автомобили»
          </label>
        </div>
      )}
      <div className="car-list">
        {data.items.map((car) => (
          <article
            className={
              "car-card " + (selected.includes(car.cluster_id) ? "chosen" : "")
            }
            key={car.cluster_id}
          >
            <button
              className="photo-link"
              onClick={() => onOpen(car.cluster_id)}
              aria-label={"Открыть " + car.title}
            >
              <CarPreview
                title={car.title}
                image={
                  car.images?.length
                    ? `/api/listings/${car.id}/image/0`
                    : undefined
                }
              />
            </button>
            <button
              className={
                "favourite-button " + (car.favourite ? "is-favourite" : "")
              }
              aria-label={
                (car.favourite ? "Убрать " : "Добавить ") +
                car.title +
                (car.favourite ? " из избранного" : " в избранное")
              }
              aria-pressed={car.favourite}
              disabled={busy}
              onClick={() => void state(car, { favourite: !car.favourite })}
            >
              <Icon name="heart" size={24} filled={car.favourite} />
            </button>
            {car.price_drop_amount && (
              <span
                className="price-drop-badge"
                title={"Цена снижена " + date(car.price_drop_at)}
              >
                ↓ {money(car.price_drop_amount)}
              </span>
            )}
            <div className="car-info">
              <div className="card-price-line">
                <strong className="price">
                  {car.price_min != null
                    ? money(car.price_min)
                    : money(car.price, car.currency)}
                </strong>
                {car.price_max !== car.price_min && car.price_min != null && (
                  <span className="small muted">– {money(car.price_max)}</span>
                )}
                <details className="card-menu">
                  <summary aria-label={"Действия с " + car.title}>
                    <Icon name="more" />
                  </summary>
                  <div className="popover">
                    <button
                      className="text-button"
                      onClick={() => void state(car, { hidden: !car.hidden })}
                      disabled={busy}
                    >
                      {car.hidden
                        ? "Вернуть в результаты"
                        : "Скрыть автомобиль"}
                    </button>
                    <label className="checkbox small">
                      <input
                        type="checkbox"
                        aria-label={`Выбрать ${car.source_listing_id} для объединения`}
                        checked={selected.includes(car.cluster_id)}
                        onChange={(e) =>
                          setSelected((ids) =>
                            e.target.checked
                              ? [...ids, car.cluster_id]
                              : ids.filter((id) => id !== car.cluster_id),
                          )
                        }
                      />
                      Выбрать для объединения
                    </label>
                  </div>
                </details>
              </div>
              <h2>
                <button
                  className="car-title"
                  onClick={() => onOpen(car.cluster_id)}
                >
                  {car.title}
                  {car.year && !car.title.includes(String(car.year))
                    ? " " + car.year
                    : ""}
                </button>
              </h2>
              <div className="car-specs">
                {[
                  car.mileage_km != null
                    ? car.mileage_km.toLocaleString("ru-RU") + " км"
                    : "Пробег не указан",
                  car.engine_volume ? Number(car.engine_volume) + " л" : null,
                  car.transmission ? display(car.transmission) : null,
                  car.city
                    ? car.city[0].toUpperCase() + car.city.slice(1)
                    : null,
                ]
                  .filter(Boolean)
                  .map((text, i) => (
                    <span key={i}>{text}</span>
                  ))}
                <span className="card-sources">
                  {car.sources.map((s) => sourceNames[s] || s).join(" · ")}
                  {car.listing_count > 1
                    ? " · " + car.listing_count + " объявления"
                    : ""}
                </span>
              </div>
              <Description text={car.description} compact />
              {car.match_state === "unverified" && (
                <p className="unverified-note">
                  Не проверено: {car.unknown_filters.map(display).join(", ")}
                </p>
              )}
            </div>
          </article>
        ))}
      </div>
      {data.items.length === 0 ? (
        <div className="empty-state">
          <Icon name="filter" size={28} />
          <h2>
            {loading
              ? "Проверяем объявления…"
              : "Подходящих автомобилей пока нет"}
          </h2>
          <p>
            Попробуйте расширить годы выпуска или бюджет. Снятые с продажи
            объявления здесь не показываются.
          </p>
        </div>
      ) : (
        <p className="result-count small muted">
          В продаже: {data.total} · Показано: {data.items.length}
        </p>
      )}
      {data.next_cursor && (
        <button
          className="outline load-more"
          disabled={loading}
          onClick={onMore}
        >
          Показать ещё
        </button>
      )}
    </>
  );
}
