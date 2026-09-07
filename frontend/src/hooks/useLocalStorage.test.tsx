/**
 * Tests du hook useLocalStorage : lecture initiale, écriture persistée,
 * updater fonctionnel, JSON corrompu -> valeur par défaut.
 */
import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, beforeEach } from "vitest";
import { useLocalStorage } from "./useLocalStorage";

describe("useLocalStorage", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("retourne la valeur initiale quand la clé est absente", () => {
    const { result } = renderHook(() => useLocalStorage("k", 42));
    expect(result.current[0]).toBe(42);
  });

  it("lit la valeur stockée (JSON)", () => {
    window.localStorage.setItem("k", JSON.stringify({ a: 1 }));
    const { result } = renderHook(() => useLocalStorage<{ a: number }>("k", { a: 0 }));
    expect(result.current[0]).toEqual({ a: 1 });
  });

  it("retombe sur la valeur initiale si le JSON est corrompu", () => {
    window.localStorage.setItem("k", "{not json");
    const { result } = renderHook(() => useLocalStorage("k", "default"));
    expect(result.current[0]).toBe("default");
  });

  it("écrit la valeur dans le stockage", () => {
    const { result } = renderHook(() => useLocalStorage("k", 1));
    act(() => result.current[1](7));
    expect(result.current[0]).toBe(7);
    expect(window.localStorage.getItem("k")).toBe("7");
  });

  it("accepte un updater fonctionnel (comme setState)", () => {
    const { result } = renderHook(() => useLocalStorage<number>("k", 10));
    act(() => result.current[1]((prev) => prev + 5));
    expect(result.current[0]).toBe(15);
    expect(window.localStorage.getItem("k")).toBe("15");
  });
});
