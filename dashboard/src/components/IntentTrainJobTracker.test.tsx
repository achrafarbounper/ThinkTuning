/**
 * Tests du composant IntentTrainJobTracker (SCRUM-95).
 *
 * Rendu conditionnel : étapes du pipeline d'intention, barre d'avancement
 * (job.progress.global_pct), bouton d'annulation selon le statut, erreur.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import IntentTrainJobTracker from "./IntentTrainJobTracker";
import type { IntentTrainJob } from "./types";

const RUNNING_JOB: IntentTrainJob = {
  job_id: "12345678-abcd-ef01",
  status: "running",
  step: "training",
  started_at: 1000,
  progress: { step: "training", global_pct: 42.5, epoch: 2, epochs_total: 3 },
};

describe("IntentTrainJobTracker", () => {
  it("ne rend rien sans job (null)", () => {
    // Arrange / Act
    const { container } = render(
      <IntentTrainJobTracker job={null} onCancel={() => {}} />
    );
    // Assert
    expect(container).toBeEmptyDOMElement();
  });

  it("affiche le statut, l'identifiant et les étapes du pipeline d'intention", () => {
    // Act
    render(<IntentTrainJobTracker job={RUNNING_JOB} onCancel={() => {}} />);
    // Assert
    expect(screen.getByText(/Job intention/)).toBeInTheDocument();
    expect(screen.getByText("12345678")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    // Étapes canoniques (uniques dans la liste) :
    expect(screen.getByText("Chargement du dataset")).toBeInTheDocument();
    expect(screen.getByText("Split train/val")).toBeInTheDocument();
    expect(screen.getByText("Chargement du modèle")).toBeInTheDocument();
    expect(screen.getByText("Sauvegarde du modèle")).toBeInTheDocument();
    expect(screen.getByText("Terminé")).toBeInTheDocument();
    // Étape courante répétée (titre + étape active) :
    expect(screen.getAllByText("Entraînement").length).toBeGreaterThan(0);
  });

  it("affiche le pourcentage global de job.progress (texte + barre)", () => {
    // Act
    render(<IntentTrainJobTracker job={RUNNING_JOB} onCancel={() => {}} />);
    // Assert
    expect(screen.getByText(/Avancement : 43 %/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "43");
  });

  it("propose l'annulation pendant l'exécution et déclenche onCancel", () => {
    // Arrange
    const onCancel = vi.fn();
    // Act
    render(<IntentTrainJobTracker job={RUNNING_JOB} onCancel={onCancel} />);
    fireEvent.click(screen.getByRole("button", { name: "Annuler ce job" }));
    // Assert
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("n'affiche pas de bouton d'annulation une fois le job terminé", () => {
    // Arrange
    const job: IntentTrainJob = { ...RUNNING_JOB, status: "completed", step: "done" };
    // Act
    render(<IntentTrainJobTracker job={job} onCancel={() => {}} />);
    // Assert
    expect(
      screen.queryByRole("button", { name: "Annuler ce job" })
    ).not.toBeInTheDocument();
  });

  it("affiche l'erreur du job en échec", () => {
    // Arrange
    const job: IntentTrainJob = {
      ...RUNNING_JOB,
      status: "failed",
      error: "Dataset vide.",
    };
    // Act
    render(<IntentTrainJobTracker job={job} onCancel={() => {}} />);
    // Assert
    expect(screen.getByText("Dataset vide.")).toBeInTheDocument();
  });

  it("affiche une entrée « Annulé » pour un job annulé", () => {
    // Arrange
    const job: IntentTrainJob = {
      ...RUNNING_JOB,
      status: "cancelled",
      step: "cancelled",
    };
    // Act
    render(<IntentTrainJobTracker job={job} onCancel={() => {}} />);
    // Assert
    // "Annulé" apparaît au moins dans l'étape courante (hint) et l'entrée
    // additionnelle de la liste.
    expect(screen.getAllByText("Annulé").length).toBeGreaterThanOrEqual(1);
  });
});