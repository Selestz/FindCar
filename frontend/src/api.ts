export interface User {
  id: string;
  username: string;
  role: string;
  csrf_token: string;
}
export interface Filters {
  [key: string]: string | number | string[] | null | undefined;
  make?: string | null;
  model?: string | null;
  region?: string | null;
  price_to?: string | null;
  year_from?: number | null;
  year_to?: number | null;
  owners_max?: number | null;
}
export interface SavedSearch {
  enabled: boolean;
  refresh_interval_seconds: number;
  next_refresh_at: string | null;
  id: string;
  name: string;
  filters: Filters;
  enabled_sources: string[];
  latest_run?: string | null;
  last_checked_at?: string | null;
}
export interface Listing {
  images: string[];
  main_image_url: string | null;
  id: string;
  cluster_id: string;
  cluster_version: number;
  listing_count: number;
  source_listing_id: string;
  display_status?: string;
  title: string;
  source: string;
  source_url: string;
  seller_type: "private" | "dealer" | "unknown";
  year: number | null;
  engine_volume: string | null;
  drive_type: string | null;
  mileage_km: number | null;
  city: string | null;
  price: string | null;
  currency: string;
  description: string | null;
  status: string;
  match_state: string;
  unknown_filters: string[];
  owners_count: number | null;
  make: string | null;
  model: string | null;
  generation: string | null;
  engine_type: string | null;
  power_hp: number | null;
  transmission: string | null;
  steering_wheel: string | null;
  body_type: string | null;
  color: string | null;
  last_seen_at: string;
  detail_checked_at: string | null;
  detail_attempted_at: string | null;
  favourite: boolean;
  hidden: boolean;
  cluster_first_seen_at: string;
  price_min: string | null;
  price_max: string | null;
  price_drop_amount: string | null;
  price_drop_at: string | null;
  sources: string[];
  new_at: string | null;
  changed_at: string | null;
}
export interface Note {
  id?: string;
  text?: string;
  created_at?: string;
  copied_from?: string;
  note?: Note;
}
export interface Evidence {
  total_score: number;
  reason: string;
  mileage_difference: number | null;
  signals: Record<string, number | null>;
  conflicts: string[];
  photos: { matching_count: number; unique_counts: number[] };
  specification_coverage: number;
  candidate_limit_hit: boolean;
  config_version: string;
}
export interface VehicleDetail {
  id: string;
  version: number;
  favourite: boolean;
  hidden: boolean;
  notes: Note[];
  first_seen_at: string;
  listings: Listing[];
  snapshots: {
    id: string;
    listing_id: string;
    price: string | null;
    currency: string;
    observed_at: string;
    status: string;
  }[];
  matching: { id: string; evidence: Evidence }[];
  manual_decisions: { id: string; decision: string }[];
}
export interface ChangeEvent {
  id: string;
  type: string;
  title: string | null;
  source: string | null;
  cluster_id: string | null;
  occurred_at: string;
  read_at: string | null;
  payload: { old_price?: string | null; new_price?: string | null };
}
export interface EventPage {
  items: ChangeEvent[];
  unread_count: number;
  next_cursor: string | null;
}
export interface Results {
  items: Listing[];
  total: number;
  unverified_count: number;
  next_cursor: string | null;
}
export interface Run {
  id: string;
  outcome: string;
  sources: {
    source: string;
    state: string;
    error_code: string | null;
    result_count: number | null;
    warnings: string[];
    pages_checked: number[];
    catalog_complete: boolean;
    not_before: string;
    attempt: number;
  }[];
}
export interface Health {
  items: {
    source: string;
    enabled: boolean;
    status: string;
    last_success_at?: string;
    last_attempt_at?: string;
    last_error?: string;
    cooldown_until?: string;
  }[];
  worker_alive: boolean;
  scheduler_enabled: boolean;
}
let csrf = "";
export function setCsrf(token: string) {
  csrf = token;
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api" + path, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...options.headers,
    },
  });
  if (!response.ok) {
    const data = await response
      .json()
      .catch(() => ({ detail: "Не удалось выполнить запрос" }));
    throw new ApiError(
      typeof data.detail === "string"
        ? data.detail
        : "Проверьте значения полей",
      response.status,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}
export const money = (price: string | null, currency = "RUB") =>
  price === null
    ? "Цена не указана"
    : new Intl.NumberFormat("ru-RU", {
        style: "currency",
        currency,
        maximumFractionDigits: 0,
      }).format(Number(price));
