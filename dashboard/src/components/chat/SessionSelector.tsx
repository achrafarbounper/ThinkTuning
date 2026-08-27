/**
 * Sélecteur de conversations persistées.
 *
 * Affiche une liste déroulante des sessions côté serveur (GET /api/sessions) :
 * - titre de la conversation + date de dernière mise à jour ;
 * - conversation active mise en évidence ;
 * - création d'une nouvelle conversation au clic.
 *
 * Les sessions sont récupérées une fois au montage de ChatWindow et
 * transmises via la prop `sessions`. La sélection déclenche `onSelect`,
 * qui charge les messages depuis GET /api/sessions/{id}/messages.
 */

import type { ChatSessionInfo } from './types';

export interface SessionSelectorProps {
  /** Conversations disponibles (ordre serveur, pas de tri local). */
  sessions: ChatSessionInfo[];
  /** Id de la conversation active, ou '' si aucune n'est sélectionnée. */
  selectedId: string;
  /** Charge les messages de la conversation cliquee. */
  onSelect: (id: string) => Promise<void>;
  /** Désactive le sélecteur pendant qu'une réponse est en cours. */
  isLoading: boolean;
}

/**
 * Formate une date ISO 8601 en chaîne lisible (« hier 14:32 » ou « 12/04 09:10 »).
 * Repli sur l'heure ISO brute si la date est invalide ou absente.
 */
function formatSessionDate(iso?: string): string {
  if (!iso) return '';
  try {
    const date = new Date(iso);
    const now = new Date();
    const diffDays = Math.floor((now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24));
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (diffDays === 0) return `Aujourd'hui ${time}`;
    if (diffDays === 1) return `Hier ${time}`;
    if (diffDays <= 7) return `Il y a ${diffDays} jours ${time}`;
    return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${time}`;
  } catch {
    return iso;
  }
}

/**
 * Affiche le titre d'une session : surnomifie la conversation active ou
 * utilise la date de création quand le titre est vide.
 */
function sessionLabel(session: ChatSessionInfo): string {
  if (session.title && session.title.trim()) return session.title;
  return formatSessionDate(session.created_at ?? session.updated_at);
}

export function SessionSelector({ sessions, selectedId, onSelect, isLoading }: SessionSelectorProps) {
  // L'option "créer une nouvelle session" est simulée via le bouton “Nouvelle tâche”
  // du header ; ici, on propose la liste existante. Si aucune session n'existe,
  // le sélecteur reste un simple libellé informatif.
  const options = sessions.length > 0 ? sessions : [];
  const selected = options.find((session) => session.id === selectedId);
  const displayTitle = selected ? sessionLabel(selected) : selectedId ? selectedId.slice(0, 8) : 'Conversation';

  return (
    <div className="session-selector" data-loading={isLoading || undefined}>
      <select
        className="session-selector__select"
        value={selectedId}
        onChange={(event: React.ChangeEvent<HTMLSelectElement>) => {
          const value = event.target.value;
          if (value) void onSelect(value);
        }}
        disabled={isLoading}
        aria-label="Sélectionner une conversation"
        title="Cliquez pour voir l'historique des conversations"
      >
        <option value="" className="session-selector__placeholder">
          Conversation
        </option>
        {options.map((session) => (
          <option key={session.id} value={session.id}>
            {sessionLabel(session)}
          </option>
        ))}
      </select>
      <span className="session-selector__value" aria-hidden="true">
        {displayTitle}
      </span>
    </div>
  );
}
