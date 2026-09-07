import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  money,
  type Note,
  type VehicleDetail as DetailData,
} from "../api";
import {
  CarPreview,
  CheckTime,
  date,
  Description,
  display,
  sourceNames,
  listingCount,
} from "../presentation";

function PriceChart({ detail }: { detail: DetailData }) {
  const points = detail.snapshots
    .filter((s) => s.price != null && s.currency === "RUB")
    .sort((a, b) => a.observed_at.localeCompare(b.observed_at));
  if (points.length < 2)
    return (
      <p className="muted">Для графика нужны хотя бы два наблюдения цены.</p>
    );
  const times = points.map((p) => new Date(p.observed_at).getTime()),
    prices = points.map((p) => Number(p.price));
  const min = Math.min(...prices),
    max = Math.max(...prices),
    start = Math.min(...times),
    end = Math.max(...times);
  const x = (time: string) =>
    60 + ((new Date(time).getTime() - start) / Math.max(1, end - start)) * 500;
  const y = (price: string | null) =>
    140 - ((Number(price) - min) / Math.max(1, max - min)) * 100;
  return (
    <figure className="price-chart">
      <figcaption>Наблюдаемая цена по объявлениям, ₽</figcaption>
      <svg
        viewBox="0 0 600 185"
        role="img"
        aria-label={`Цена от ${money(String(min))} до ${money(String(max))}`}
      >
        <path d="M60 25V150H565" stroke="#454151" fill="none" />
        <text x="0" y="40">
          {(max / 1000000).toFixed(2)} млн
        </text>
        <text x="0" y="140">
          {(min / 1000000).toFixed(2)} млн
        </text>
        {detail.listings.map((listing, i) => {
          const series = points.filter((p) => p.listing_id === listing.id);
          return (
            <g key={listing.id} fill={i % 2 ? "#688ee0" : "#ab83f2"}>
              <polyline
                stroke={i % 2 ? "#688ee0" : "#ab83f2"}
                strokeWidth="2"
                fill="none"
                points={series
                  .map((p) => `${x(p.observed_at)},${y(p.price)}`)
                  .join(" ")}
              />
              {series.map((p) => (
                <circle key={p.id} cx={x(p.observed_at)} cy={y(p.price)} r="4">
                  <title>
                    {listing.source_listing_id}: {date(p.observed_at)} ·{" "}
                    {money(p.price)}
                  </title>
                </circle>
              ))}
            </g>
          );
        })}
        <text x="60" y="175">
          {date(points[0].observed_at)}
        </text>
        <text x="560" y="175" textAnchor="end">
          {date(points[points.length - 1].observed_at)}
        </text>
      </svg>
    </figure>
  );
}
function unwrap(note: Note): Note {
  return note.note ? unwrap(note.note) : note;
}
export function VehicleDetail({
  id,
  onClose,
  onChanged,
}: {
  id: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<DetailData | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [note, setNote] = useState(""),
    [editing, setEditing] = useState<string | null>(null),
    [splitOpen, setSplitOpen] = useState(false),
    [showArchived, setShowArchived] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void api<DetailData>("/vehicles/" + id, { signal: controller.signal })
      .then(setDetail)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [id]);
  async function mutate(path: string, body: unknown) {
    setBusy(true);
    setError("");
    try {
      await api(path, { method: "PATCH", body: JSON.stringify(body) });
      setDetail(await api<DetailData>("/vehicles/" + id));
      onChanged();
      setNote("");
      setEditing(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function saveNote(e: FormEvent) {
    e.preventDefault();
    if (!detail) return;
    void mutate(
      editing ? `/vehicles/${id}/notes/${editing}` : `/vehicles/${id}/state`,
      editing
        ? { version: detail.version, text: note }
        : { version: detail.version, note },
    );
  }
  async function split() {
    if (!detail) return;
    setBusy(true);
    setError("");
    try {
      await api(`/vehicles/${id}/split`, {
        method: "POST",
        body: JSON.stringify({
          version: detail.version,
          groups: detail.listings.map((l) => [l.id]),
        }),
      });
      onChanged();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const primary =
    detail?.listings.find((l) => l.status === "ACTIVE") || detail?.listings[0];
  return (
    <div className="vehicle-detail">
      <button className="text-button" onClick={onClose}>
        ← К списку автомобилей
      </button>
      {error && (
        <p role="alert" className="error">
          {error}{" "}
          <button
            className="text-button"
            onClick={() =>
              void api<DetailData>("/vehicles/" + id)
                .then(setDetail)
                .then(() => setError(""))
                .catch((e) => setError(e.message))
            }
          >
            Обновить карточку
          </button>
        </p>
      )}
      {!detail || !primary ? (
        <p role="status">
          {error ? "Карточка недоступна." : "Загружаем автомобиль…"}
        </p>
      ) : (
        <>
          <div className="detail-title">
            <div>
              <div className="eyebrow">
                Автомобиль · {listingCount(detail.listings.length)}
              </div>
              <h1>{primary.title}</h1>
              <p className="muted">
                Впервые замечен {date(detail.first_seen_at)}
              </p>
            </div>
            <div className="decision-actions">
              <button
                className="outline"
                disabled={busy}
                aria-pressed={detail.favourite}
                onClick={() =>
                  void mutate(`/vehicles/${id}/state`, {
                    version: detail.version,
                    favourite: !detail.favourite,
                  })
                }
              >
                {detail.favourite ? "♥ В избранном" : "♡ В избранное"}
              </button>
              <button
                className="outline"
                disabled={busy}
                onClick={() =>
                  void mutate(`/vehicles/${id}/state`, {
                    version: detail.version,
                    hidden: !detail.hidden,
                  })
                }
              >
                {detail.hidden ? "Вернуть из скрытых" : "Скрыть"}
              </button>
            </div>
          </div>
          {primary.status !== "ACTIVE" && (
            <p className="warning">
              Подтверждённых активных предложений нет. Ниже сохранена история
              наблюдений.
            </p>
          )}
          <div className="detail-overview">
            <CarPreview
              title={primary.title}
              source={primary.source}
              image={
                primary.source !== "mock" && primary.images?.length
                  ? `/api/listings/${primary.id}/image/0`
                  : undefined
              }
            />
            <div>
              <p className="price">{money(primary.price, primary.currency)}</p>
              <dl className="specifications">
                {(
                  [
                    ["Марка", primary.make],
                    ["Модель", primary.model],
                    ["Поколение", primary.generation],
                    ["Год", primary.year],
                    ["Кузов", primary.body_type],
                    [
                      "Двигатель",
                      `${display(primary.engine_type)}${primary.engine_volume ? " · " + Number(primary.engine_volume) + " л" : ""}`,
                    ],
                    [
                      "Мощность",
                      primary.power_hp ? `${primary.power_hp} л. с.` : null,
                    ],
                    ["Коробка", primary.transmission],
                    ["Привод", primary.drive_type],
                    ["Руль", primary.steering_wheel],
                    [
                      "Пробег",
                      primary.mileage_km == null
                        ? null
                        : primary.mileage_km.toLocaleString("ru-RU") + " км",
                    ],
                    ["Цвет", primary.color],
                    ["Владельцы", primary.owners_count],
                  ] as [string, string | number | null][]
                ).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key}</dt>
                    <dd>{display(value)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </div>
          <section className="detail-section">
            <h2>Мои заметки</h2>
            <p className="muted small">Видны только вам.</p>
            {detail.notes.map((entry, i) => {
              const n = unwrap(entry);
              return (
                <div className="note" key={n.id ? `${n.id}-${i}` : i}>
                  <p>{n.text || "Заметка из исходного автомобиля"}</p>
                  <div className="note-meta">
                    <span>
                      {date(n.created_at)}
                      {entry.copied_from ? " · Из исходной группы" : ""}
                    </span>
                    {n.id && (
                      <>
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => {
                            setEditing(n.id!);
                            setNote(n.text || "");
                          }}
                        >
                          Изменить
                        </button>
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() =>
                            void mutate(`/vehicles/${id}/notes/${n.id}`, {
                              version: detail.version,
                              text: null,
                            })
                          }
                        >
                          Удалить заметку
                        </button>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
            <form onSubmit={saveNote}>
              <label>
                {editing ? "Редактировать заметку" : "Новая заметка"}
                <textarea
                  rows={3}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  maxLength={5000}
                  required
                  placeholder="Что уточнить у продавца, впечатления от осмотра…"
                />
              </label>
              <div className="decision-actions">
                <button disabled={busy || !note.trim()}>
                  {editing ? "Сохранить заметку" : "Добавить заметку"}
                </button>
                {editing && (
                  <button
                    type="button"
                    className="outline"
                    onClick={() => {
                      setEditing(null);
                      setNote("");
                    }}
                  >
                    Отмена
                  </button>
                )}
              </div>
            </form>
          </section>
          <section className="detail-section">
            <h2>Объявления и источники</h2>
            {detail.listings.some((l) => l.status !== "ACTIVE") && (
              <label className="checkbox small">
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(e) => setShowArchived(e.target.checked)}
                />
                Показать снятые и непроверенные объявления (
                {detail.listings.filter((l) => l.status !== "ACTIVE").length})
              </label>
            )}
            {detail.listings
              .filter((l) => showArchived || l.status === "ACTIVE")
              .map((l) => (
                <article className="source-listing" key={l.id}>
                  <div className="result-heading">
                    <span className="badge">
                      {sourceNames[l.source] || l.source}
                    </span>
                    <strong>{money(l.price, l.currency)}</strong>
                    <span
                      className={
                        "badge " + (l.status === "ACTIVE" ? "positive" : "")
                      }
                    >
                      {display(l.display_status || l.status)}
                    </span>
                  </div>
                  <p className="small muted">
                    {l.source_listing_id} ·{" "}
                    {l.mileage_km?.toLocaleString("ru-RU") ??
                      "Неизвестный пробег"}{" "}
                    км
                  </p>
                  <CheckTime car={l} />
                  <Description text={l.description} />
                  <p className="small muted">
                    Продавец:{" "}
                    {l.seller_type === "private"
                      ? "частное лицо"
                      : l.seller_type === "dealer"
                        ? "дилер"
                        : "тип не указан"}
                  </p>
                  <a
                    className="external-link"
                    href={l.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Открыть на {sourceNames[l.source] || l.source} ↗
                  </a>
                </article>
              ))}
          </section>
          <section className="detail-section">
            <h2>История цены</h2>
            <PriceChart detail={detail} />
            <div className="timeline">
              {detail.snapshots.map((s) => (
                <div className="timeline-item" key={s.id}>
                  <span>{date(s.observed_at)}</span>
                  <strong>{money(s.price, s.currency)}</strong>
                  <span>
                    {display(s.status)} ·{" "}
                    {
                      detail.listings.find((l) => l.id === s.listing_id)
                        ?.source_listing_id
                    }
                  </span>
                </div>
              ))}
            </div>
          </section>
          {detail.listings.length > 1 && (
            <section className="detail-section">
              <h2>Почему объявления объединены</h2>
              {detail.manual_decisions.length > 0 && (
                <p>Связь подтверждена вручную.</p>
              )}
              {detail.matching.map((m) => (
                <details key={m.id}>
                  <summary>
                    Сходство {Math.round(m.evidence.total_score * 100)}/100 ·
                    Совпало фото: {m.evidence.photos.matching_count}
                  </summary>
                  <p>
                    Разница пробега:{" "}
                    {m.evidence.mileage_difference ?? "неизвестна"} км.
                    Противоречия:{" "}
                    {m.evidence.conflicts.map(display).join(", ") ||
                      "не обнаружены"}
                    .
                  </p>
                  <p>
                    Сходство описаний:{" "}
                    {m.evidence.signals.description == null
                      ? "недостаточно данных"
                      : Math.round(m.evidence.signals.description * 100) + "%"}
                    .
                  </p>
                </details>
              ))}
              <button
                className="outline"
                onClick={() => setSplitOpen(!splitOpen)}
              >
                Это разные автомобили
              </button>
              {splitOpen && (
                <div className="warning">
                  <p>
                    Каждое из {detail.listings.length} объявлений станет
                    отдельным автомобилем. Повторное автоматическое объединение
                    между ними будет запрещено; заметки сохранятся у каждого.
                  </p>
                  <div className="decision-actions">
                    <button disabled={busy} onClick={() => void split()}>
                      Разделить объявления
                    </button>
                    <button
                      className="outline"
                      onClick={() => setSplitOpen(false)}
                    >
                      Отмена
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
