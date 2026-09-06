/**
 * ThinkingBlock.test.tsx — Comportement « flux temps réel » du bloc Réflexion.
 *
 * Verrouille le correctif SCRUM-101 (affichage du raisonnement en streaming) :
 *  - le bloc est déplié pendant la diffusion et refermé à la fin ;
 *  - le contenu suit automatiquement le bas du bloc pendant la diffusion ;
 *  - un défilement manuel vers le haut coupe le suivi (l'utilisateur garde
 *    la main, même heuristique que le stick-to-bottom de la fenêtre) ;
 *  - la réouverture d'une trace terminée montre le DÉBUT (aucun forçage de
 *    scroll hors diffusion).
 *
 * jsdom n'implémente pas la mise en page : scrollHeight / clientHeight /
 * scrollTop sont simulés par des propriétés configurables sur l'élément <pre>.
 */

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ThinkingBlock } from "./ThinkingBlock";

/** Simule la géométrie de défilement d'un élément pour jsdom (pas de layout). */
function stubScrollGeometry(
  element: HTMLElement,
  scrollHeight: number,
  clientHeight: number,
): void {
  Object.defineProperty(element, "scrollHeight", {
    configurable: true,
    value: scrollHeight,
  });
  Object.defineProperty(element, "clientHeight", {
    configurable: true,
    value: clientHeight,
  });
  let value = 0;
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => value,
    set: (next: number) => {
      value = next;
    },
  });
}

describe("ThinkingBlock — cycle de diffusion", () => {
  it("déplie le bloc pendant la diffusion puis le referme à la fin", () => {
    const { container, rerender } = render(
      <ThinkingBlock thinking="" streaming />,
    );
    expect(screen.getByText("Réflexion en cours…")).toBeInTheDocument();
    expect(container.querySelector(".chat-thinking__content")).not.toBeNull();

    rerender(<ThinkingBlock thinking="Trace complète." streaming={false} />);
    expect(screen.queryByText("Réflexion en cours…")).toBeNull();
    expect(screen.getByText("Réflexion")).toBeInTheDocument();
    expect(container.querySelector(".chat-thinking__content")).toBeNull();
  });

  it("n'est pas rendu du tout sans réflexion et hors diffusion", () => {
    const { container } = render(<ThinkingBlock thinking="" streaming={false} />);
    expect(container.querySelector(".chat-thinking")).toBeNull();
  });
});

describe("ThinkingBlock — suivi automatique du bas (flux temps réel)", () => {
  it("colle le bas du contenu à chaque fragment diffusé", () => {
    const { container, rerender } = render(<ThinkingBlock thinking="" streaming />);
    const pre = container.querySelector<HTMLElement>(".chat-thinking__content")!;
    expect(pre).not.toBeNull();

    // jsdom : géométrie simulée (contenu 500px dans une boîte de 200px).
    stubScrollGeometry(pre, 500, 200);
    rerender(<ThinkingBlock thinking="Un fragment de réflexion." streaming />);
    expect(pre.scrollTop).toBe(500);

    // Le suivi continue au fragment suivant (contenu plus long).
    stubScrollGeometry(pre, 640, 200);
    rerender(
      <ThinkingBlock thinking="Un fragment de réflexion. Encore un." streaming />,
    );
    expect(pre.scrollTop).toBe(640);
  });

  it("coupe le suivi après un défilement manuel vers le haut", () => {
    const { container, rerender } = render(<ThinkingBlock thinking="" streaming />);
    const pre = container.querySelector<HTMLElement>(".chat-thinking__content")!;

    stubScrollGeometry(pre, 500, 200);
    // L'utilisateur remonte en haut du contenu (300px > seuil de 40px).
    fireEvent.scroll(pre, { target: { scrollTop: 0 } });
    // Plus aucun fragment ne force le défilement.
    rerender(
      <ThinkingBlock thinking="Un fragment. Un autre. Encore un." streaming />,
    );
    expect(pre.scrollTop).toBe(0);
  });

  it("reprend le suivi au retour manuel en bas du contenu", () => {
    const { container, rerender } = render(<ThinkingBlock thinking="" streaming />);
    const pre = container.querySelector<HTMLElement>(".chat-thinking__content")!;

    stubScrollGeometry(pre, 500, 200);
    fireEvent.scroll(pre, { target: { scrollTop: 0 } });
    rerender(<ThinkingBlock thinking="Fragment." streaming />);
    expect(pre.scrollTop).toBe(0);

    // L'utilisateur revient au bas (distance 0px <= seuil) : le suivi reprend.
    fireEvent.scroll(pre, { target: { scrollTop: 500 } });
    stubScrollGeometry(pre, 560, 200);
    rerender(<ThinkingBlock thinking="Fragment. Suite." streaming />);
    expect(pre.scrollTop).toBe(560);
  });
});

describe("ThinkingBlock — réouverture d'une trace terminée", () => {
  it("montre le début de la trace (aucun saut forcé en bas hors diffusion)", () => {
    const { container, rerender } = render(
      <ThinkingBlock thinking="Longue trace de raisonnement." streaming />,
    );
    // Fin de diffusion : le bloc se referme (le <pre> est démonté).
    rerender(<ThinkingBlock thinking="Longue trace de raisonnement." streaming={false} />);

    // Réouverture manuelle via le bouton toggle (nom accessible = aria-label).
    fireEvent.click(screen.getByRole("button", { name: /réflexion/i }));
    const pre = container.querySelector<HTMLElement>(".chat-thinking__content")!;
    expect(pre).not.toBeNull();
    stubScrollGeometry(pre, 999, 200);
    // L'effet de suivi ne s'applique PAS hors diffusion : scrollTop reste 0.
    expect(pre.scrollTop).toBe(0);
  });
});