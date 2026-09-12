import { useCallback, useState } from "react";
import type { AgentSettings } from "../../api/agentSettings";
import { DEFAULT_BASE_URL } from "../../api/sentimentApiClient";
import { useApp } from "../../context/useApp";
import { createDraft, type DraftShape } from "./types";

export function useSettingsDraft() {
  const { config, agentSettings } = useApp();
  const [draft, setDraft] = useState<DraftShape>(() =>
    createDraft(
      { baseUrl: config.baseUrl || DEFAULT_BASE_URL, apiKey: config.apiKey || "" },
      agentSettings
    )
  );
  const [previousSettings, setPreviousSettings] = useState(agentSettings);

  if (agentSettings !== previousSettings) {
    setPreviousSettings(agentSettings);
    setDraft((previous) => ({ ...previous, ...agentSettings }));
  }

  const updateDraft = useCallback(
    <K extends keyof DraftShape>(field: K, value: DraftShape[K]) => {
      setDraft((previous) => ({ ...previous, [field]: value }));
    },
    []
  );

  return { draft, updateDraft };
}

export type DraftUpdater = <K extends keyof DraftShape>(field: K, value: DraftShape[K]) => void;
export type AgentDraft = Partial<AgentSettings> & DraftShape;
