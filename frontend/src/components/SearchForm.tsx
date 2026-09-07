import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type Filters, type SavedSearch } from "../api";
import { labels, sourceNames } from "../presentation";
import { getCatalog, type CatalogItem } from "../catalog";
import { ComboBox, type Option } from "./ComboBox";
import { Icon } from "./Icon";

const yearOptions: Option[] = [
  { value: "", label: "Любой" },
  ...Array.from({ length: new Date().getFullYear() + 2 - 1886 }, (_, i) => {
    const value = String(new Date().getFullYear() + 1 - i);
    return { value, label: value };
  }),
];
const priceOptions: Option[] = [
  { value: "", label: "Любая" },
  ...Array.from({ length: 1000 }, (_, i) => {
    const value = (i + 1) * 100000;
    return {
      value: String(value),
      label: value.toLocaleString("ru-RU") + " ₽",
    };
  }),
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
const defaultFilters: Filters = {
  make: "porsche",
  model: "panamera",
  region: "москва",
  price_to: "1900000",
  year_from: 2010,
  year_to: 2015,
};

export function SearchForm({
  selected,
  busy,
  onSubmit,
  availableSources = [],
}: {
  selected: SavedSearch | null;
  busy: boolean;
  onSubmit: (
    filters: Filters,
    sources: string[],
    start: boolean,
  ) => Promise<void>;
  availableSources?: string[];
}) {
  const form = useRef<HTMLFormElement>(null),
    opener = useRef<HTMLButtonElement>(null);
  const f = selected?.filters || defaultFilters;
  const [generation, setGeneration] = useState(String(f.generation || ""));
  const [basic, setBasic] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      [
        "make",
        "model",
        "year_from",
        "year_to",
        "price_from",
        "price_to",
        "region",
      ].map((key) => [key, String(f[key] ?? "")]),
    ),
  );
  const [regions, setRegions] = useState<CatalogItem[]>([]);
  const [brands, setBrands] = useState<CatalogItem[]>([]),
    [models, setModels] = useState<CatalogItem[]>([]),
    [modelLoading, setModelLoading] = useState(true);
  const [sources, setSources] = useState<string[]>(
    selected?.enabled_sources.filter((s) => s !== "mock") || [
      "drom",
      "auto_ru",
    ],
  );
  const [error, setError] = useState(""),
    [expanded, setExpanded] = useState(false),
    [mobileOpen, setMobileOpen] = useState(false);
  useEffect(() => {
    const ctrl = new AbortController();
    void Promise.all([
      getCatalog("", ctrl.signal),
      api<{ items: CatalogItem[] }>("/regions", { signal: ctrl.signal }),
    ])
      .then(([makes, cities]) => {
        setBrands(makes);
        setRegions(cities.items);
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(e.message);
      });
    return () => ctrl.abort();
  }, []);
  useEffect(() => {
    const ctrl = new AbortController();
    if (!basic.make) {
      setModels([]);
      setModelLoading(false);
      return;
    }
    setModelLoading(true);
    void getCatalog(basic.make, ctrl.signal)
      .then((data) => {
        if (!ctrl.signal.aborted) setModels(data);
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) {
          setModels([]);
          setError(e.message);
        }
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setModelLoading(false);
      });
    return () => ctrl.abort();
  }, [basic.make]);
  useEffect(() => {
    if (!mobileOpen) return;
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    form.current
      ?.querySelector<HTMLButtonElement>(".mobile-filter-title button")
      ?.focus();
    return () => {
      document.body.style.overflow = before;
      opener.current?.focus();
    };
  }, [mobileOpen]);
  const support = basic.model
    ? models.find((m) => m.id === basic.model)?.sources || []
    : brands.find((m) => m.id === basic.make)?.sources || [];
  const allowed = availableSources.filter(
    (s) =>
      support.includes(s) &&
      (regions.find((r) => r.id === basic.region)?.sources.includes(s) ??
        false),
  );
  const chosen = sources.filter((s) => allowed.includes(s));
  function change(key: string, value: string) {
    if (key === "make" || key === "model") setGeneration("");
    setBasic((prev) => ({
      ...prev,
      [key]: value,
      ...(key === "make" ? { model: "" } : {}),
    }));
    setError("");
  }
  function options(items: CatalogItem[], any: string): Option[] {
    return [
      { value: "", label: any },
      ...items.map((item) => ({ value: item.id, label: item.label })),
    ];
  }
  function input(key: string, label: string, min = "0", step = "1") {
    return (
      <label key={key}>
        {label}
        <input
          name={key}
          type="number"
          min={min}
          step={step}
          defaultValue={String(f[key] ?? "")}
        />
      </label>
    );
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!chosen.length) {
      setError("Выберите площадку, доступную для этой модели и региона");
      return;
    }
    const data = new FormData(event.currentTarget),
      filters: Filters = { ...f };
    const numbers = new Set([
      "year_from",
      "year_to",
      "owners_max",
      "mileage_from",
      "mileage_to",
      "power_from",
      "power_to",
    ]);
    for (const [key, value] of data) {
      if (key === "body_types") continue;
      const text = String(value).trim();
      filters[key] = text ? (numbers.has(key) ? Number(text) : text) : null;
    }
    filters.body_types = data.getAll("body_types").map(String);
    if (basic.make !== f.make || basic.model !== f.model)
      filters.generation = null;
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
        setError("Значение «от» должно быть не больше значения «до»");
        return;
      }
    }
    const start =
      (event.nativeEvent as SubmitEvent).submitter?.getAttribute("value") !==
      "save";
    try {
      await onSubmit(filters, chosen, start);
      setMobileOpen(false);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <>
      <button
        ref={opener}
        type="button"
        className="mobile-filters outline"
        onClick={() => setMobileOpen(true)}
      >
        <Icon name="filter" />
        Изменить фильтры
      </button>
      <form
        ref={form}
        role={mobileOpen ? "dialog" : undefined}
        aria-modal={mobileOpen || undefined}
        aria-label={mobileOpen ? "Фильтры поиска" : "Условия поиска"}
        onKeyDown={(e) => {
          if (!mobileOpen) return;
          if (e.key === "Escape") {
            e.preventDefault();
            setMobileOpen(false);
          }
          if (e.key === "Tab") {
            const controls = Array.from(
              form.current?.querySelectorAll<HTMLElement>(
                'input:not([type="hidden"]):not(:disabled),button:not(:disabled),select:not(:disabled),textarea',
              ) || [],
            ).filter((el) => el.offsetParent !== null);
            const first = controls[0],
              last = controls[controls.length - 1];
            if (e.shiftKey && document.activeElement === first) {
              e.preventDefault();
              last?.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
              e.preventDefault();
              first?.focus();
            }
          }
        }}
        className={"search-form " + (mobileOpen ? "mobile-open" : "")}
        onSubmit={(e) => void submit(e)}
      >
        <div className="mobile-filter-title">
          <h2>Фильтры</h2>
          <button
            type="button"
            className="icon-button"
            aria-label="Закрыть фильтры"
            onClick={() => setMobileOpen(false)}
          >
            <Icon name="close" />
          </button>
        </div>
        <div className="primary-filters">
          <div className="filter-field">
            <span>Марка</span>
            <ComboBox
              label="Марка"
              name="make"
              value={basic.make}
              options={options(brands, "Выберите марку")}
              onChange={(v) => change("make", v)}
              disabled={!brands.length}
            />
          </div>
          <div className="filter-field">
            <span>Модель</span>
            <ComboBox
              label="Модель"
              name="model"
              value={basic.model}
              options={options(models, "Любая модель")}
              onChange={(v) => change("model", v)}
              disabled={modelLoading || !basic.make}
            />
          </div>
          <div className="filter-field">
            <span>Год выпуска</span>
            <div className="range-pair">
              <ComboBox
                label="Год от"
                name="year_from"
                value={basic.year_from}
                options={yearOptions}
                onChange={(v) => change("year_from", v)}
              />
              <span>–</span>
              <ComboBox
                label="Год до"
                name="year_to"
                value={basic.year_to}
                options={yearOptions}
                onChange={(v) => change("year_to", v)}
              />
            </div>
          </div>
          <div className="filter-field price-field">
            <span>Цена, ₽</span>
            <div className="range-pair">
              <ComboBox
                label="Цена от"
                name="price_from"
                value={basic.price_from}
                options={priceOptions}
                onChange={(v) => change("price_from", v)}
              />
              <span>–</span>
              <ComboBox
                label="Цена до"
                name="price_to"
                value={basic.price_to}
                options={priceOptions}
                onChange={(v) => change("price_to", v)}
              />
            </div>
          </div>
          <div className="filter-field">
            <span>Регион</span>
            <ComboBox
              label="Регион"
              name="region"
              value={basic.region}
              options={regions.map((r) => ({ value: r.id, label: r.label }))}
              onChange={(v) => change("region", v)}
            />
          </div>
        </div>
        <div className="search-actions">
          <button
            type="button"
            className="outline all-filters"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            <Icon name="filter" />
            Все фильтры
          </button>
          <fieldset className="source-choice">
            <legend>Источники</legend>
            {["drom", "auto_ru"].map((source) => (
              <label className="checkbox" key={source}>
                <input
                  type="checkbox"
                  aria-label={sourceNames[source]}
                  disabled={!allowed.includes(source)}
                  checked={chosen.includes(source)}
                  onChange={(e) =>
                    setSources((prev) =>
                      e.target.checked
                        ? [...prev, source]
                        : prev.filter((s) => s !== source),
                    )
                  }
                />
                {sourceNames[source]}
              </label>
            ))}
          </fieldset>
          <div className="submit-actions">
            <button
              type="submit"
              value="find"
              disabled={busy || modelLoading || !basic.make || !chosen.length}
            >
              {busy ? "Ищем…" : "Найти автомобили"}
            </button>
            <button
              type="submit"
              value="save"
              className="outline accent"
              disabled={busy || modelLoading || !basic.make || !chosen.length}
            >
              Сохранить поиск
            </button>
          </div>
        </div>
        <div className="advanced-fields" hidden={!expanded}>
          <div className="fields">
            {input("owners_max", "Владельцев до", "1")}
            {input("mileage_from", "Пробег от, км")}
            {input("mileage_to", "Пробег до, км")}
            {input("engine_volume_from", "Двигатель от, л", ".1", ".1")}
            {input("engine_volume_to", "Двигатель до, л", ".1", ".1")}
            {input("power_from", "Мощность от, л. с.", "1")}
            {input("power_to", "Мощность до, л. с.", "1")}
            {Object.entries(enums).map(([key, [label, values]]) => (
              <label key={key}>
                {label}
                <select name={key} defaultValue={String(f[key] || "")}>
                  <option value="">Любой</option>
                  {values.map((v) => (
                    <option key={v} value={v}>
                      {labels[v]}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <label>
              Поколение
              <input
                name="generation"
                value={generation}
                onChange={(e) => setGeneration(e.target.value)}
                placeholder="Например, 970"
              />
            </label>
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
                      Array.isArray(f.body_types) &&
                      f.body_types.includes(value)
                    }
                  />
                  {labels[value]}
                </label>
              ))}
            </div>
          </fieldset>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </form>
    </>
  );
}
