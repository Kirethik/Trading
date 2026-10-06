"use client";
import { useEffect, useState } from "react";

/**
 * Returns a debounced copy of `value` that only updates after `delay` ms with
 * no further changes. Pass a *primitive* (e.g. a JSON string), not an object —
 * objects get a fresh identity every render and would reset the timer forever.
 *
 * Used to feed rapidly-changing controls (slider drags) into a TanStack Query
 * key so the backtest fires once the user pauses, not once per pixel.
 */
export function useDebounce<T>(value: T, delay = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return debounced;
}
