"use client";

import { useEffect, useState } from "react";

/**
 * True only after the first client render. Used to gate anything derived from
 * the current clock (elapsed time), so server HTML and the hydrated render
 * always agree.
 */
export function useMounted(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);
  return mounted;
}