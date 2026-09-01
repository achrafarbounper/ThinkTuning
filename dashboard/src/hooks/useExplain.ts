/**
 * hooks/useExplain.ts
 * ---------------------------------------------------------------------
 * Unifie la logique « expliquer une prédiction via le LLM » (POST /explain),
 * auparavant dupliquée 3× dans SentimentPage (unitaire, batch, historique) avec
 * trois états identiques. Un seul hook = un seul point de maintenance.
 *
 * Exemple : const explain = useExplain(client); explain.run("texte")…
 */

import { useCallback, useState } from "react";
import type { SentimentApiClient } from "../api/sentimentApiClient";

export interface ExplainState {
  /** Texte en cours d'explication (identifiant logique de la requête). */
  text: string;
  loading: boolean;
  result: string | null;
  error: string | null;
}

const IDLE: ExplainState = { text: "", loading: false, result: null, error: null };

export function useExplain(client: SentimentApiClient) {
  const [state, setState] = useState<ExplainState>(IDLE);

  const run = useCallback(
    async (text: string) => {
      setState({ text, loading: true, result: null, error: null });
      try {
        const result = await client.explain({ text });
        setState({
          text,
          loading: false,
          result: result?.explanation ?? null,
          error: null,
        });
      } catch (err) {
        setState({
          text,
          loading: false,
          result: null,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    },
    [client]
  );

  const reset = useCallback(() => setState(IDLE), []);

  return { state, run, reset };
}
