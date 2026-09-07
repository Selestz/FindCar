import { useState, type FormEvent } from "react";
import type { Filters, SavedSearch } from "../api";
import { labels, sourceNames } from "../presentation";
const ranges = [
  ["price_from", "Цена от, ₽", "0"],
  ["mileage_from", "Пробег от, км", "0"],
  ["mileage_to", "Пробег до, км", "0"],
  ["engine_volume_from", "Двигатель от, л", ".1"],
  ["engine_volume_to", "Двигатель до, л", ".1"],
  ["power_from", "Мощность от, л. с.", "1"],
  ["power_to", "Мощность до, л. с.", "1"],
];
const enums: Record<string, [string, string[]]> = {
  transmission: ["Коробка передач", ["automatic", "manual", "robot", "cvt"]],
  engine_type: [
    "Тип двигателя",
    ["petrol", "diesel", "hybrid", "electric", "gas", "other"],
  ],
  drive_type: ["Привод", ["all", "front", "rear"]],
  steering_wheel: ["Руль", ["left", "right"]],
};
export function SearchForm({
  selected,
  busy,
  onSubmit,
  availableSources = ["mock"],
}: {
  selected: SavedSearch | null;
  busy: boolean;
  onSubmit: (
    name: string,
    filters: Filters,
    sources: string[],
  ) => Promise<void>;
  availableSources?: string[];
}) {
  const f: Filters = selected?.filters || {
    make: "porsche",
    model: "panamera",
    region: "москва",
    price_to: "1900000",
    year_from: 2010,
    year_to: 2015,
    owners_max: 5,
  };
  const [error, setError] = useState("");
  const [make, setMake] = useState(String(f.make || ""));
  const [sources, setSources] = useState<string[] | null>(
    selected?.enabled_sources || null,
  );
  const chosen =
    sources ?? (availableSources.includes("drom") ? ["drom"] : ["mock"]);
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError("");
    const form = new FormData(e.currentTarget);
    const filters: Filters = { ...f };
    const numbers = new Set([
      "year_from",
      "year_to",
      "owners_max",
      "mileage_from",
      "mileage_to",
      "power_from",
      "power_to",
    ]);
    for (const [key, value] of form) {
      if (key === "name" || key === "body_types" || key === "enabled_sources")
        continue;
      const text = String(value).trim();
      filters[key] = text ? (numbers.has(key) ? Number(text) : text) : null;
    }
    if (!make) filters.model = null;
    filters.body_types = form.getAll("body_types").map(String);
    if (!filters.model) filters.generation = null;
    for (const prefix of [
      "year",
      "price",
      "mileage",
      "engine_volume",
      "power",
    ]) {
      const a = filters[prefix + "_from"],
        b = filters[prefix + "_to"];
      if (a != null && b != null && Number(a) > Number(b)) {
        setError("Значение «от» должно быть не больше значения «до».");
        return;
      }
    }
    if (!chosen.length) {
      setError("Выберите хотя бы один источник.");
      return;
    }
    void onSubmit(String(form.get("name")).trim(), filters, chosen);
  }
  const input = (name: string, label: string, min = "0") => (
    <label key={name}>
      {label}
      <input
        name={name}
        type="number"
        min={min}
        step={name.includes("volume") ? ".1" : "1"}
        defaultValue={String(f[name] ?? "")}
      />
    </label>
  );
  return (
    <form className="search-form" onSubmit={submit}>
      <div className="fields">
        <label>
          Название поиска
          <input
            name="name"
            required
            maxLength={150}
            defaultValue={selected?.name || "Panamera до 1,9 млн"}
          />
        </label>
        <label>
          Марка
          <select
            name="make"
            value={make}
            onChange={(e) => setMake(e.target.value)}
          >
            <option value="">Любая</option>
            {!["porsche", "bmw", ""].includes(make) && (
              <option value={make}>{make}</option>
            )}
            <option value="porsche">Porsche</option>
            <option value="bmw">BMW</option>
          </select>
        </label>
        <label>
          Модель
          <select
            key={make}
            name="model"
            defaultValue={make === f.make ? String(f.model || "") : ""}
            disabled={!make}
          >
            <option value="">Любая</option>
            {make === "porsche" && <option value="panamera">Panamera</option>}
            {make === "bmw" && <option value="3-series">3 series</option>}
            {f.model && !["panamera", "3-series"].includes(String(f.model)) && (
              <option value={String(f.model)}>{String(f.model)}</option>
            )}
          </select>
        </label>
        <label>
          Регион
          <input
            name="region"
            list="regions"
            defaultValue={String(f.region || "")}
            placeholder="Любой"
          />
          <datalist id="regions">
            <option value="москва" />
          </datalist>
        </label>
        {input("price_to", "Цена до, ₽")}
        {input("year_from", "Год от", "1886")}
        {input("year_to", "Год до", "1886")}
        {input("owners_max", "Владельцев до", "1")}
      </div>
      <details className="advanced-filters">
        <summary>Все характеристики</summary>
        <div className="fields">
          <label>
            Поколение
            <input
              name="generation"
              defaultValue={String(f.generation || "")}
              placeholder="Например, 970"
            />
          </label>
          {ranges.map(([name, label, min]) => input(name, label, min))}
          {Object.entries(enums).map(([name, [title, values]]) => (
            <label key={name}>
              {title}
              <select name={name} defaultValue={String(f[name] || "")}>
                <option value="">Любой</option>
                {values.map((value) => (
                  <option value={value} key={value}>
                    {labels[value]}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
        <fieldset>
          <legend>Кузов</legend>
          <div className="body-options">
            {[
              "sedan",
              "hatchback",
              "liftback",
              "wagon",
              "suv",
              "coupe",
              "convertible",
              "pickup",
              "minivan",
              "van",
              "other",
            ].map((value) => (
              <label className="checkbox" key={value}>
                <input
                  type="checkbox"
                  name="body_types"
                  value={value}
                  defaultChecked={
                    Array.isArray(f.body_types) && f.body_types.includes(value)
                  }
                />
                {labels[value]}
              </label>
            ))}
          </div>
        </fieldset>
        <p className="small muted">
          Drom: Москва, Санкт-Петербург, Новосибирск. Auto.ru: Москва. Оставьте
          регион пустым для поиска по России. Радиус пока не поддерживается.
          Неизвестные характеристики можно показать отдельно.
        </p>
      </details>
      <fieldset className="source-choice">
        <legend>Где искать</legend>
        {Object.entries(sourceNames).map(([source, name]) => (
          <label className="checkbox" key={source}>
            <input
              type="checkbox"
              name="enabled_sources"
              value={source}
              disabled={!availableSources.includes(source)}
              checked={chosen.includes(source)}
              onChange={(e) =>
                setSources(
                  e.target.checked
                    ? [...chosen, source]
                    : chosen.filter((x) => x !== source),
                )
              }
            />
            {name}
            {!availableSources.includes(source) ? " · не подключён" : ""}
          </label>
        ))}
      </fieldset>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <button disabled={busy}>
        {busy
          ? "Поиск выполняется…"
          : selected
            ? "Сохранить изменения и найти"
            : "Сохранить и найти"}
      </button>
    </form>
  );
}
