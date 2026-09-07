import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "./Icon";
export interface Option {
  value: string;
  label: string;
}
const normalize = (s: string) =>
  s.toLocaleLowerCase("ru").replace(/[\s.,₽-]/g, "");
export function ComboBox({
  label,
  value,
  options,
  onChange,
  name,
  disabled = false,
  placeholder = "Выберите",
}: {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  name?: string;
  disabled?: boolean;
  placeholder?: string;
}) {
  const id = useId(),
    input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false),
    [query, setQuery] = useState(""),
    [active, setActive] = useState(0);
  const selected = options.find((o) => o.value === value);
  const filtered = options.filter(
    (o) =>
      normalize(o.label).includes(normalize(query)) ||
      normalize(o.value).includes(normalize(query)),
  );
  const shown = filtered.slice(0, 100);
  useEffect(() => {
    if (open)
      document
        .getElementById(id + "-" + active)
        ?.scrollIntoView({ block: "nearest" });
  }, [open, active, id]);
  function choose(option: Option) {
    onChange(option.value);
    setOpen(false);
    setQuery("");
    input.current?.focus();
  }
  return (
    <div
      className="combobox"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) {
          setOpen(false);
          setQuery("");
        }
      }}
    >
      {name && <input type="hidden" name={name} value={value} />}
      <input
        ref={input}
        id={id}
        aria-label={label}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={id + "-options"}
        aria-activedescendant={
          open && shown[active] ? id + "-" + active : undefined
        }
        autoComplete="off"
        disabled={disabled}
        placeholder={placeholder}
        value={open ? query : selected?.label || value}
        onClick={() => {
          setOpen(true);
          setQuery("");
          setActive(0);
        }}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape" && open) {
            setOpen(false);
            setQuery("");
            e.stopPropagation();
          }
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            if (!open) {
              setOpen(true);
              setQuery("");
              setActive(0);
            } else
              setActive((n) =>
                Math.max(
                  0,
                  Math.min(
                    shown.length - 1,
                    n + (e.key === "ArrowDown" ? 1 : -1),
                  ),
                ),
              );
          }
          if (e.key === "Enter" && open) {
            e.preventDefault();
            if (shown[active]) choose(shown[active]);
          }
          if (e.key === "Tab") {
            setOpen(false);
            setQuery("");
          }
        }}
      />
      <span className="combo-chevron">
        <Icon name="chevron" size={16} />
      </span>
      {open && (
        <div
          className="combo-menu"
          id={id + "-options"}
          role="listbox"
          aria-label={label}
        >
          {shown.map((option, i) => (
            <button
              type="button"
              role="option"
              id={id + "-" + i}
              key={option.value}
              aria-selected={option.value === value}
              className={i === active ? "highlighted" : ""}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(option)}
            >
              {option.label}
              {option.value === value && <span aria-hidden="true">✓</span>}
            </button>
          ))}
          {!shown.length && <p className="small muted">Ничего не найдено</p>}
          {filtered.length > 100 && (
            <p className="small muted">
              Введите название или число для уточнения
            </p>
          )}
        </div>
      )}
    </div>
  );
}
