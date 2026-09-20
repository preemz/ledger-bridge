import { formatMoney, isNegative } from "@/lib/format";

/** Renders a decimal money string, 2 decimals, negative values in red. */
export function Money({
  value,
  signed = false,
  className = "num",
}: {
  value: string | number | null | undefined;
  signed?: boolean;
  className?: string;
}) {
  const negative = isNegative(value);
  const text = formatMoney(value);
  const prefix = signed && !negative && text !== "-" ? "+" : "";
  return (
    <span className={negative ? `${className} num-negative` : className}>
      {prefix}
      {text}
    </span>
  );
}
