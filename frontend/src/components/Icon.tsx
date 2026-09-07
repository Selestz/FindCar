export function Icon({
  name,
  size = 20,
  filled = false,
}: {
  name:
    "heart" | "chevron" | "filter" | "bookmark" | "close" | "image" | "more";
  size?: number;
  filled?: boolean;
}) {
  const paths = {
    heart:
      "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z",
    chevron: "m6 9 6 6 6-6",
    filter: "M3 5h18M6 12h12M10 19h4",
    bookmark: "M6 3h12v18l-6-4-6 4V3Z",
    close: "m6 6 12 12M6 18 18 6",
    image: "M3 4h18v16H3V4Zm0 12 5-5 4 4 3-3 6 6M16 8h.01",
    more: "M5 12h.01M12 12h.01M19 12h.01",
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
