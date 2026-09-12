export function AboutSection() {
  return (
    <section className="tt-panel" aria-labelledby="settings-about-title">
      <div className="tt-panel-head"><h2 id="settings-about-title">À propos</h2></div>
      <p className="tt-hint">
        ThinkTuning — pipeline complet de recomposition de données (EDA) + fine-tuning
        DistilBERT multilingue pour la classification de sentiments (positif / neutre /
        négatif) en français et en anglais. Assistant IA configurable via Ollama,
        OpenRouter, Hugging Face ou LM Studio.
      </p>
    </section>
  );
}
