import { useEffect, useState } from "react";
import { api, money } from "../api";
import { CarPreview, Description, display, sourceNames } from "../presentation";

interface Candidate extends Record<string, unknown> {
  id: string;
  left_cluster_id: string;
  right_cluster_id: string;
  left_version: number;
  right_version: number;
  left_title: string;
  right_title: string;
  left_source_id: string;
  right_source_id: string;
  evidence: {
    total_score: number;
    reason: string;
    mileage_difference: number | null;
    conflicts: string[];
    photos: { matching_count: number; unique_counts: number[] };
    specification_coverage: number;
    candidate_limit_hit: boolean;
    config_version: string;
  };
}

export function Duplicates({
  searchId,
  revision,
  onChanged,
  onOpen,
}: {
  searchId?: string;
  revision: unknown;
  onChanged: () => void;
  onOpen: (id: string) => void;
}) {
  const [data, setData] = useState<{
    items: Candidate[];
    has_more: boolean;
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<NonNullable<typeof data>>(
      "/duplicates" + (searchId ? "?search_id=" + searchId : ""),
      {
        signal: controller.signal,
      },
    )
      .then(setData)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [searchId, revision, retry]);
  async function decide(candidate: Candidate, action: "merge" | "reject") {
    setBusy(true);
    setError("");
    try {
      await api(action === "merge" ? "/vehicles/merge" : "/duplicates/reject", {
        method: "POST",
        body: JSON.stringify({
          versions: {
            [candidate.left_cluster_id]: candidate.left_version,
            [candidate.right_cluster_id]: candidate.right_version,
          },
        }),
      });
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="duplicates" aria-label="Возможные дубли">
      <p className="muted">
        Оценка отражает сходство объявлений. Решение об одном автомобиле можно
        принять вручную.
      </p>
      {error && (
        <p role="alert" className="error">
          {error}
          <button
            className="text-button"
            onClick={() => {
              setError("");
              setRetry((n) => n + 1);
            }}
          >
            Обновить пары
          </button>
        </p>
      )}
      {!data && !error && <p role="status">Загружаем пары для проверки…</p>}
      {data?.items.length === 0 && <p>Пар для проверки пока нет.</p>}
      {data?.items.map((c) => (
        <article className="duplicate-card" key={c.id}>
          <div className="compare-pair">
            {(["left", "right"] as const).map((side) => {
              const title = String(c[side + "_title"]);
              return (
                <section key={side}>
                  <CarPreview
                    title={title}
                    source={String(c[side + "_source"])}
                  />
                  <h3>{title}</h3>
                  <p className="price">
                    {money(
                      c[side + "_price"] == null
                        ? null
                        : String(c[side + "_price"]),
                      String(c[side + "_currency"] || "RUB"),
                    )}
                  </p>
                  <p>
                    {String(c[side + "_year"] ?? "Год не указан")} ·{" "}
                    {c[side + "_mileage_km"] == null
                      ? "Пробег неизвестен"
                      : Number(c[side + "_mileage_km"]).toLocaleString(
                          "ru-RU",
                        ) + " км"}{" "}
                    · {display(c[side + "_color"] as string | null)}
                  </p>
                  <p className="small muted">
                    {sourceNames[String(c[side + "_source"])]} ·{" "}
                    {String(c[side + "_source_id"])}
                  </p>
                  <Description
                    text={c[side + "_description"] as string | null}
                  />
                  <button
                    className="text-button"
                    onClick={() => onOpen(String(c[side + "_cluster_id"]))}
                  >
                    Открыть автомобиль →
                  </button>
                </section>
              );
            })}
          </div>
          <p>
            <strong>
              Сходство {Math.round(c.evidence.total_score * 100)}/100
            </strong>{" "}
            · Совпало фото: {c.evidence.photos.matching_count}
          </p>
          <p>
            {c.evidence.reason === "insufficient_photos"
              ? "Характеристики близки, но фотографий недостаточно для автоматического объединения."
              : c.evidence.reason === "cluster_conflict"
                ? "В составе групп есть противоречие или ручной запрет на объединение."
                : "Нужно проверить, что это один автомобиль."}
          </p>
          <details>
            <summary>Почему предложена эта пара</summary>
            <p>
              Разница пробега:{" "}
              {c.evidence.mileage_difference === null
                ? "неизвестна"
                : `${c.evidence.mileage_difference.toLocaleString("ru-RU")} км`}
              . Заполненность сравниваемых характеристик:{" "}
              {Math.round(c.evidence.specification_coverage * 100)}%.
            </p>
            <p>
              Доступно разных фото:{" "}
              {c.evidence.photos.unique_counts.join(" и ")}. Противоречия:{" "}
              {c.evidence.conflicts.join(", ") || "не обнаружены"}.
            </p>
            {c.evidence.candidate_limit_hit && (
              <p className="warning">
                Часть кандидатов осталась за пределами лимита проверки.
              </p>
            )}
          </details>
          <div className="decision-actions">
            <button disabled={busy} onClick={() => void decide(c, "merge")}>
              Это один автомобиль
            </button>
            <button
              className="outline"
              disabled={busy}
              onClick={() => void decide(c, "reject")}
            >
              Разные автомобили
            </button>
          </div>
        </article>
      ))}
      {data?.has_more && (
        <p>Есть ещё пары. После проверки показанных появятся следующие.</p>
      )}
    </section>
  );
}
