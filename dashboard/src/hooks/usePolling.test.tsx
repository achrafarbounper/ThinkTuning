/**
 * Tests du hook usePolling : tick immédiat, intervalle, pause onglet masqué,
 * rafraîchissement au retour, arrêt au démontage, tolérance aux rejets.
 */
import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./usePolling";

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
}

describe("usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    setHidden(false);
  });

  it("exécute le tick immédiatement puis à chaque intervalle", async () => {
    const tick = vi.fn();
    renderHook(() => usePolling({ intervalMs: 1000, tick }));

    // Le tick est planifié sur une microtask : on la purge avant d'affirmer.
    await act(async () => {
      await Promise.resolve();
    });
    expect(tick).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    expect(tick).toHaveBeenCalledTimes(3); // initial + 2 intervalles
  });

  it("ne tick pas au montage si l'onglet est déjà masqué", () => {
    const tick = vi.fn();
    setHidden(true);
    renderHook(() => usePolling({ intervalMs: 1000, tick }));

    expect(tick).not.toHaveBeenCalled();
  });

  it("rafraîchit immédiatement au retour sur l'onglet", async () => {
    const tick = vi.fn();
    renderHook(() => usePolling({ intervalMs: 60_000, tick, immediate: false }));

    await act(async () => {
      setHidden(false);
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(tick).toHaveBeenCalledTimes(1);
  });

  it("ne propage pas les rejets du tick (pas d'unhandled rejection)", async () => {
    const tick = vi.fn().mockRejectedValue(new Error("boom"));
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    renderHook(() => usePolling({ intervalMs: 1000, tick }));

    await act(async () => {
      vi.advanceTimersByTime(1100);
      // Laisse les microtasks (catch du hook) se jouer.
      await Promise.resolve();
    });
    expect(tick).toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalledWith(expect.stringContaining("boom"));
    consoleError.mockRestore();
  });

  it("arrête le polling au démontage", async () => {
    const tick = vi.fn();
    const { unmount } = renderHook(() => usePolling({ intervalMs: 1000, tick }));

    await act(async () => {
      await Promise.resolve();
    });
    expect(tick).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(tick).toHaveBeenCalledTimes(1); // toujours le tick initial uniquement
  });

  it("respecte enabled=false (aucun tick)", () => {
    const tick = vi.fn();
    renderHook(() => usePolling({ intervalMs: 1000, tick, enabled: false }));

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(tick).not.toHaveBeenCalled();
  });
});
