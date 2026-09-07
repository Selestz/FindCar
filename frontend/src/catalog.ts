import { api } from "./api";
export interface CatalogItem {
  id: string;
  label: string;
  sources: string[];
  model_count?: number;
}
const cache = new Map<string, CatalogItem[]>();
export async function getCatalog(
  make = "",
  signal?: AbortSignal,
): Promise<CatalogItem[]> {
  const existing = cache.get(make);
  if (existing) return existing;
  const data = await api<{ items: CatalogItem[] }>(
    "/catalog" + (make ? "/" + encodeURIComponent(make) : ""),
    { signal },
  );
  cache.set(make, data.items);
  return data.items;
}
