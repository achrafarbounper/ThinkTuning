/**
 * jobSteps.ts
 * ---------------------------------------------------------------------
 * Ordres canoniques des étapes de progression côté serveur.
 *
 * Consommés par les trackers de progression UI (TrainJobTracker,
 * PipelineJobTracker) pour rester alignés sur le vrai pipeline serveur au lieu
 * d'une barre inventée. Module minuscule et NON critique : il n'est importé
 * que par les pages/ composants lazy, donc jamais au premier rendu.
 */

/** Ordre réel des étapes traversées par _run_training() dans api.py. */
export const TRAIN_STEPS: readonly string[] = [
  "queued",
  "loading_dataset",
  "splitting_dataset",
  "augmenting_dataset",
  "building_dataloaders",
  "computing_class_weights",
  "loading_model",
  "training",
  "saving_model",
  "done",
] as const;

/** Ordre des étapes du pipeline end-to-end (core/pipeline_runner.py). */
export const PIPELINE_STEPS: readonly string[] = [
  "queued",
  "labeling",
  "filtering",
  "finetuning",
  "done",
] as const;
