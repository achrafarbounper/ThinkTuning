/**
 * hooks/useLocalStorage.ts
 * ---------------------------------------------------------------------
 * État React synchronisé avec localStorage (lecture paresseuse + écriture
 * immédiate). Généralise la logique de persistence disséminée dans les pages.
 */

import { useCallback, useState } from "react";

export type SetLocalValue<T> = T | ((previous: T) => T);

export function useLocalStorage<T>(
  key: string,
  initialValue: T,
): [T, (value: SetLocalValue<T>) => void] {
  const [stored, setStored] = useState<T>(() => {
    if (typeof window === "undefined") return initialValue;
    let raw: string | null = null;
    try {
      raw = window.localStorage.getItem(key);
    } catch {
      /* stockage indisponible */
    }
    if (raw == null) return initialValue;
    try {
      return JSON.parse(raw) as T;
    } catch {
      return initialValue;
    }
  });

  const setValue = useCallback(
    (value: SetLocalValue<T>) => {
      setStored((previous) => {
        const next =
          typeof value === "function"
            ? (value as (prev: T) => T)(previous)
            : value;
        try {
          window.localStorage.setItem(key, JSON.stringify(next));
        } catch {
          /* stockage indisponible : l'état reste valable pour la session */
        }
        return next;
      });
    },
    [key],
  );

  return [stored, setValue];
}