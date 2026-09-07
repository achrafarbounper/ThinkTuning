/**
 * markdown.test.tsx — Contrat de rendu Markdown des réponses de l'assistant.
 *
 * Verrouille le correctif « retour de l'assistant mal formaté » :
 *  - les titres ##, le gras **…**, les listes, le code et les tableaux sont
 *    rendus comme des éléments HTML (plus jamais affichés en littéral) ;
 *  - le contenu est préservé (aucune transformation du texte) ;
 *  - le streaming tolère un Markdown incomplet (« ** » / fence non fermés) ;
 *  - la sécurité : liens limités à http(s)/mailto, pas d'innerHTML.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatMessage } from "./ChatMessage";
import { MarkdownContent } from "./markdown";
import { MultiAgentTrace } from "./MultiAgentTrace";
import type { ChatMessageData } from "./types";

const baseMessage: ChatMessageData = {
  id: "m1",
  role: "assistant",
  content: "",
  createdAt: new Date("2026-09-05T10:00:00Z").toISOString(),
};

describe("MarkdownContent — titres & emphases", () => {
  it("rend les titres ATX (# à ######) comme des headings", () => {
    render(<MarkdownContent content={"## Analyse\n\n### Détails"} />);
    expect(screen.getByRole("heading", { level: 2, name: "Analyse" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Détails" })).toBeInTheDocument();
  });

  it("rend gras, italique et code en ligne sans altérer le texte", () => {
    const { container } = render(
      <MarkdownContent content={"Un **point important** et *une nuance* et `pip install`"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("point important");
    expect(container.querySelector("em")?.textContent).toBe("une nuance");
    expect(container.querySelector("p > code")?.textContent).toBe("pip install");
    expect(container.textContent).toBe("Un point important et une nuance et pip install");
  });

  it("laisse un « ** » non fermé en littéral (tolérance streaming)", () => {
    const { container } = render(<MarkdownContent content={"Réponse **en cours"} />);
    expect(container.querySelector("strong")).toBeNull();
    expect(container.textContent).toContain("**en cours");
  });

  it("traite snake_case et unités sans faux positifs d'italique", () => {
    const { container } = render(
      <MarkdownContent content={"agent_max_rounds et 2 * 3 restent intacts"} />,
    );
    expect(container.querySelector("em")).toBeNull();
    expect(container.textContent).toContain("agent_max_rounds et 2 * 3 restent intacts");
  });
});

describe("MarkdownContent — blocs", () => {
  it("rend listes à puces, ordonnées et imbriquées", () => {
    const { container } = render(
      <MarkdownContent content={"- un\n  - un.a\n- deux\n\n1. premier\n2. second"} />,
    );
    expect(container.querySelectorAll("ul")).toHaveLength(2); // racine + sous-liste
    expect(container.querySelectorAll("ol")).toHaveLength(1);
    expect(container.querySelectorAll("li")).toHaveLength(5);
  });

  it("rend les cases à cocher GFM", () => {
    const { container } = render(<MarkdownContent content={"- [x] fait\n- [ ] à faire"} />);
    const boxes = container.querySelectorAll("input[type=checkbox]");
    expect(boxes).toHaveLength(2);
    expect(boxes[0]).toHaveProperty("checked", true);
    expect(boxes[1]).toHaveProperty("checked", false);
  });

  it("rend les blocs de code avec label de langage", () => {
    const { container } = render(<MarkdownContent content={"```python\nprint('ok')\n```"} />);
    expect(container.querySelector(".chat-md-code pre code")?.textContent).toBe("print('ok')");
    expect(container.querySelector(".chat-md-code")?.getAttribute("data-lang")).toBe("python");
  });

  it("ferme implicitement une fence non terminée (streaming)", () => {
    const { container } = render(<MarkdownContent content={"```bash\npip install x"} />);
    expect(container.querySelector(".chat-md-code pre code")?.textContent).toBe("pip install x");
  });

  it("rend les tableaux GFM avec alignements", () => {
    const { container } = render(
      <MarkdownContent content={"| Col A | Col B |\n| :--- | ---: |\n| 1 | 2 |"} />,
    );
    expect(container.querySelectorAll("th")).toHaveLength(2);
    expect(container.querySelectorAll("td")).toHaveLength(2);
    expect(container.querySelector("th")?.getAttribute("style")).toContain("left");
    expect(container.querySelectorAll("th")[1]?.getAttribute("style")).toContain("right");
  });

  it("rend citations, séparateurs et sauts de ligne simples", () => {
    const { container } = render(
      <MarkdownContent content={"Avant\n\n> une citation\n\n---\n\nLigne une\nLigne deux"} />,
    );
    expect(container.querySelector("blockquote")?.textContent).toContain("une citation");
    expect(container.querySelector("hr")).not.toBeNull();
    expect(container.querySelector("p br")).not.toBeNull();
  });
});

describe("MarkdownContent — sécurité & curseur", () => {
  it("n'accepte que http(s)/mailto : un schème exotique reste littéral", () => {
    const { container } = render(<MarkdownContent content={"[clic](javascript:alert(1))"} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("[clic](javascript:alert(1))");
  });

  it("rend les liens sûrs avec target=_blank et rel=noopener", () => {
    const { container } = render(<MarkdownContent content={"[docs](https://example.com)"} />);
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com");
    expect(link?.getAttribute("rel")).toContain("noopener");
    expect(link?.getAttribute("target")).toBe("_blank");
  });

  it("affiche le curseur de streaming en fin de dernier bloc", () => {
    const { container } = render(<MarkdownContent content="Texte" cursor />);
    expect(container.querySelector(".chat-message__cursor")).not.toBeNull();
  });
});

describe("ChatMessage — intégration bulle assistant / utilisateur", () => {
  it("la bulle assistant rend le Markdown (pas de « ## » littéral)", () => {
    const { container } = render(
      <ChatMessage message={{ ...baseMessage, content: "## Bilan\n\n- **Succès** : oui" }} />,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Bilan" })).toBeInTheDocument();
    expect(container.querySelector(".chat-message__text--md")).not.toBeNull();
    expect(container.textContent).not.toContain("##");
  });

  it("la bulle utilisateur reste en texte brut fidèle", () => {
    render(<ChatMessage message={{ ...baseMessage, role: "user", content: "## Pas un titre" }} />);
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.getByText("## Pas un titre")).toBeInTheDocument();
  });
});

describe("MultiAgentTrace — sous-tâches en Markdown (rendu en ligne)", () => {
  const plan = [
    { task_id: "t1", role: "cleaner", subtask: "**Nettoyer** les `donnees.csv`" },
  ];

  it("rend gras et code inline des sous-tâches du plan (pas de « ** » littéral)", () => {
    const { container } = render(<MultiAgentTrace plan={plan} />);
    expect(container.querySelector(".multi-agent-trace__subtask strong")?.textContent).toBe(
      "Nettoyer",
    );
    expect(container.querySelector(".multi-agent-trace__subtask code")?.textContent).toBe(
      "donnees.csv",
    );
    expect(container.textContent).not.toContain("**");
  });

  it("rend les liens des résumés de workers (http(s) uniquement)", () => {
    const { container } = render(
      <MultiAgentTrace
        plan={plan}
        workers={[
          {
            task_id: "t1",
            role: "cleaner",
            status: "ok",
            summary: "[docs](https://example.com) consultées",
          },
        ]}
      />,
    );
    const link = container.querySelector(".multi-agent-trace__subtask a");
    expect(link?.getAttribute("href")).toBe("https://example.com");
  });

  it("laisse un schéma exotique en littéral dans les résumés de workers", () => {
    const { container } = render(
      <MultiAgentTrace
        workers={[
          {
            task_id: "t2",
            role: "etl",
            status: "error",
            message: "[x](javascript:alert(1)) bloqué",
          },
        ]}
      />,
    );
    expect(container.querySelector(".multi-agent-trace__subtask a")).toBeNull();
    expect(container.textContent).toContain("[x](javascript:alert(1)) bloqué");
  });
});