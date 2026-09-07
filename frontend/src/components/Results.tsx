import { useState } from "react";
import { api, money, type Listing, type Results as ResultData } from "../api";
import {
  CarPreview,
  date,
  Description,
  display,
  sourceNames,
  listingCount,
} from "../presentation";
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
      <div className="empty" role="status">
        {loading
          ? "Загружаем автомобили…"
          : "Выберите сохранённый поиск или задайте свои условия."}
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
      <div className="list-summary">
        <span>
          Найдено автомобилей: <strong>{data.total}</strong>
        </span>
        <span className="small muted">Один автомобиль — одна строка</span>
      </div>
      {chosen.length >= 2 && (
        <div className="merge-selection">
          <button
            disabled={busy || chosen.length > 10}
            onClick={() => void merge()}
          >
            Объединить выбранные ({chosen.length})
          </button>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={override}
              onChange={(e) => setOverride(e.target.checked)}
            />
            Заменить предыдущие решения «разные автомобили» для выбранных
          </label>
          {chosen.length > 10 && <p>Выберите не более 10 автомобилей.</p>}
        </div>
      )}
      <div className="result-list" aria-busy={loading}>
        {data.items.map((car) => (
          <article className="car" key={car.cluster_id}>
            <CarPreview
              title={car.title}
              source={car.source}
              image={
                car.source !== "mock" && car.images?.length
                  ? `/api/listings/${car.id}/image/0`
                  : undefined
              }
            />
            <div className="car-description">
              <div className="badges">
                {car.new_at && <span className="badge positive">Новое</span>}
                {car.changed_at && (
                  <span className="badge amber">Изменилось</span>
                )}
                {car.status !== "ACTIVE" && (
                  <span className="badge">{display(car.status)}</span>
                )}
              </div>
              <h3>
                <button
                  className="title-button"
                  onClick={() => onOpen(car.cluster_id)}
                >
                  {car.title}
                </button>
              </h3>
              <p>
                {car.year ?? "Год не указан"} ·{" "}
                {car.engine_volume
                  ? Number(car.engine_volume) + " л"
                  : "Объём не указан"}{" "}
                · {display(car.transmission)} · {display(car.drive_type)}
              </p>
              <p>
                {car.mileage_km?.toLocaleString("ru-RU") ??
                  "Неизвестный пробег"}{" "}
                км ·{" "}
                {car.city === "москва"
                  ? "Москва"
                  : car.city || "Город не указан"}
              </p>
              <div className="badges">
                {car.sources.map((s) => (
                  <span className="source-badge" key={s}>
                    {sourceNames[s] || s}
                  </span>
                ))}
                <span className="small muted">
                  {listingCount(car.listing_count)}
                </span>
              </div>
              <p className="small muted">
                Впервые замечен {date(car.cluster_first_seen_at)}
              </p>
              <Description text={car.description} compact />
              {car.match_state === "unverified" && (
                <p className="warning">
                  Не проверено: {car.unknown_filters.map(display).join(", ")}
                </p>
              )}
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
                Сравнить и объединить
              </label>
            </div>
            <div className="car-price">
              <strong className="price">
                {car.price_min != null
                  ? money(car.price_min)
                  : money(car.price, car.currency)}
              </strong>
              {car.price_max !== car.price_min && car.price_min != null && (
                <p className="small muted">до {money(car.price_max)}</p>
              )}
              {car.price_drop_amount && (
                <p className="price-drop">
                  ↓ {money(car.price_drop_amount)}{" "}
                  <span className="small">· {date(car.price_drop_at)}</span>
                </p>
              )}
              <div className="car-actions">
                <button
                  className="outline"
                  aria-label={
                    car.favourite
                      ? `Убрать ${car.title} из избранного`
                      : `Добавить ${car.title} в избранное`
                  }
                  aria-pressed={car.favourite}
                  disabled={busy}
                  onClick={() => void state(car, { favourite: !car.favourite })}
                >
                  {car.favourite ? "♥" : "♡"}
                </button>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() => void state(car, { hidden: !car.hidden })}
                >
                  {car.hidden ? "Вернуть" : "Скрыть"}
                </button>
                <button
                  className="text-button"
                  onClick={() => onOpen(car.cluster_id)}
                >
                  Подробнее →
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
      {data.items.length === 0 && (
        <div className="empty-state">
          <h3>Здесь пока нет автомобилей</h3>
          <p>
            Измените условия или выберите другой раздел. Неизвестные
            характеристики можно включить отдельно.
          </p>
        </div>
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
