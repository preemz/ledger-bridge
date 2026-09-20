"use client";

import { useEffect, useRef } from "react";

/**
 * Runs `fn` on an interval using a recursive timer, so a slow request never
 * stacks up behind the next tick. Pass enabled=false to stop polling.
 */
export function usePoll(
  fn: () => Promise<void> | void,
  intervalMs: number,
  enabled = true,
): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (cancelled) return;
      try {
        await fnRef.current();
      } catch {
        // Polling never throws; guards against unexpected client errors.
      }
      if (!cancelled) {
        timer = setTimeout(() => {
          void tick();
        }, intervalMs);
      }
    };

    timer = setTimeout(() => {
      void tick();
    }, intervalMs);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [intervalMs, enabled]);
}