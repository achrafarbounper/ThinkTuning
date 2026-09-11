/**
 * Tests du formulaire d'inscription (RegisterForm).
 *
 * Couvre : rendu des champs, validation (email/mot de passe/confirmation/CGU),
 * appel du callback `register` avec email normalisé, et `onSuccess` au succès.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RegisterForm, type RegisterFormProps } from "./RegisterForm";

/** jsdom n'implémente pas requestAnimationFrame — stub minimal (shake). */
beforeEach(() => {
  window.requestAnimationFrame = (callback: FrameRequestCallback): number => {
    callback(0);
    return 0;
  };
  window.cancelAnimationFrame = () => {};
});

function fillEmail(value: string): void {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value } });
}

function fillPassword(value: string): void {
  fireEvent.change(screen.getByLabelText("Mot de passe"), { target: { value } });
}

function fillConfirm(value: string): void {
  fireEvent.change(screen.getByLabelText("Confirmez le mot de passe"), {
    target: { value },
  });
}

function acceptTerms(): void {
  fireEvent.click(screen.getByRole("checkbox"));
}

function submit(): void {
  fireEvent.click(screen.getByRole("button", { name: "Créer mon compte" }));
}

function renderRegister(overrides: Partial<RegisterFormProps> = {}) {
  const register = vi.fn().mockResolvedValue({ email: "nouvel@exemple.com" });
  const onSuccess = vi.fn();
  render(<RegisterForm register={register} onSuccess={onSuccess} {...overrides} />);
  return { register, onSuccess };
}

describe("RegisterForm", () => {
  it("rend tous les champs et la case CGU", () => {
    renderRegister();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Mot de passe")).toBeInTheDocument();
    expect(screen.getByLabelText("Confirmez le mot de passe")).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Créer mon compte" })).toBeInTheDocument();
  });

  it("bloque la soumission vide et signale les champs requis", () => {
    const { register } = renderRegister();
    submit();

    expect(screen.getByText("Email requis")).toBeInTheDocument();
    expect(screen.getByText("Mot de passe requis")).toBeInTheDocument();
    // Le libellé ET le message d'erreur portent le même texte (au moins l'erreur).
    expect(screen.getAllByText("Confirmez le mot de passe").length).toBeGreaterThan(0);
    expect(screen.getByText("Acceptez les CGU pour continuer")).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
  });

  it("refuse un mot de passe trop court", () => {
    const { register } = renderRegister();
    fillEmail("nouvel@exemple.com");
    fillPassword("court");
    fillConfirm("court");
    acceptTerms();
    submit();

    expect(screen.getByText("Au moins 8 caractères")).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
  });

  it("signale une confirmation différente du mot de passe", () => {
    const { register } = renderRegister();
    fillEmail("nouvel@exemple.com");
    fillPassword("mot-de-passe-long");
    fillConfirm("autre-mot-de-passe");
    acceptTerms();
    submit();

    expect(
      screen.getByText("Les mots de passe ne correspondent pas")
    ).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
  });

  it("exige la case CGU même avec des champs valides", () => {
    const { register } = renderRegister();
    fillEmail("nouvel@exemple.com");
    fillPassword("mot-de-passe-long");
    fillConfirm("mot-de-passe-long");
    submit();

    expect(screen.getByText("Acceptez les CGU pour continuer")).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
  });

  it("appelle register avec l'email normalisé puis onSuccess au succès", async () => {
    const { register, onSuccess } = renderRegister();
    fillEmail("  Nouvel@Exemple.com ");
    fillPassword("mot-de-passe-long");
    fillConfirm("mot-de-passe-long");
    acceptTerms();
    submit();

    await waitFor(() =>
      expect(register).toHaveBeenCalledWith("nouvel@exemple.com", "mot-de-passe-long")
    );
    await waitFor(() =>
      expect(onSuccess).toHaveBeenCalledWith({ email: "nouvel@exemple.com" })
    );
  });

  it("affiche l'erreur remontée par le callback register (ex. 409)", async () => {
    const register = vi
      .fn()
      .mockRejectedValue(new Error("Un compte existe déjà avec cet email"));
    renderRegister({ register });

    fillEmail("nouvel@exemple.com");
    fillPassword("mot-de-passe-long");
    fillConfirm("mot-de-passe-long");
    acceptTerms();
    submit();

    await waitFor(() =>
      expect(screen.getByText("Un compte existe déjà avec cet email")).toBeInTheDocument()
    );
  });
});